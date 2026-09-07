"""Small personal AOSP workflow; Git and Repo remain the source of truth."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from . import protection
from .dissociate import copy_objects

ROOT = Path(__file__).resolve().parents[1]
TARGET = json.loads((ROOT / "targets/aosp13.json").read_text())
MANAGED = "aosp-research-prototype-v1"


class LabError(RuntimeError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def run(argv, cwd=None, capture=False, check=True, env=None, timeout=None):
    result = subprocess.run([str(x) for x in argv], cwd=cwd, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            env=env, timeout=timeout)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise LabError(f"Command failed ({result.returncode}): {shlex.join([str(x) for x in argv])}\n{detail}")
    return result


def git(path, *args, check=True):
    return run(["git", "-C", path, *args], capture=True, check=check,
               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout.strip()


def require_clean(path):
    if git(path, "status", "--porcelain=v1", "-uall"):
        raise LabError(f"Unsaved changes in {path}; commit or preserve them yourself before continuing.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        p = Path(git(path, "rev-parse", "--git-path", marker))
        if not p.is_absolute():
            p = Path(path) / p
        if p.exists():
            raise LabError(f"Git operation in progress: {p}")


def validate_paths(source, tree, state, root=ROOT):
    paths = [Path(p).resolve() for p in (source, tree, state, root)]
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise LabError(f"Directories must be separate, not nested: {a} / {b}")
    return paths[:3]


class Lab:
    def __init__(self):
        p = ROOT / ".lab.local.json"
        if not p.exists():
            raise LabError("Run lab configure --source PATH --tree PATH --state PATH first.")
        c = json.loads(p.read_text())
        self.source, self.tree, self.state = validate_paths(c["source"], c["tree"], c["state"])
        self.product = self.tree / TARGET["research_path"]
        self.core = self.tree / "frameworks/base"
        self.out = self.tree / "out"
        self.product_out = self.out / "target/product" / TARGET["device"]
        self.state.mkdir(parents=True, exist_ok=True)

    def require_owned(self):
        p = self.tree / ".research-workspace.json"
        if not p.exists():
            raise LabError(f"Not an initialized research workspace: {self.tree}")
        meta = json.loads(p.read_text())
        if meta.get("owner") != MANAGED or meta.get("source") != str(self.source):
            raise LabError("Workspace identity mismatch; refusing to change it.")

    @contextlib.contextmanager
    def lock(self):
        with (self.state / "operation.lock").open("a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LabError("Another lab operation is running.")
            yield

    def repo(self, *args, capture=False):
        self.require_owned()
        launcher = self.tree / ".repo/repo/repo"
        return run([sys.executable, launcher, *args], cwd=self.tree, capture=capture)

    def prepare_repo_tool(self):
        self.require_owned()
        tool = self.tree / ".repo/repo"
        revision = git(self.source / ".repo/repo", "rev-parse", "HEAD")
        if not tool.exists():
            tool.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--no-hardlinks", self.source / ".repo/repo", tool], capture=True)
            git(tool, "switch", "--detach", revision)
        if git(tool, "rev-parse", "HEAD") != revision:
            raise LabError("Experimental Repo tool revision differs from the reference tool.")
        path = tool / "project.py"
        original = git(tool, "show", "HEAD:project.py") + "\n"
        start = original.index('                cmd = ["repack", "-a", "-d"]')
        end = original.index('                platform_utils.remove(alternates_file)', start)
        replacement = f'''                # research: copy available objects, including partial-clone packs.
                subprocess.run([sys.executable, {str(ROOT / "scripts/dissociate.py")!r},
                                os.path.join(self.objdir, "objects")], check=True)
                if glob.glob(os.path.join(self.objdir, "objects/pack/*.promisor")):
                    self.EnableRepositoryExtension("partialclone", self.remote.name)
                    self.config.SetBoolean("remote.%s.promisor" % self.remote.name, True)
                    self.config.SetString("remote.%s.partialclonefilter" % self.remote.name, "blob:none")
'''
        desired = original[:start] + replacement + original[end:]
        previous = original.replace('cmd = ["repack", "-a", "-d"]',
                   'cmd = ["repack", "-a"]  # research: copy borrowed objects without deleting precious packs')
        if path.read_text() not in (original, previous, desired):
            raise LabError("Unrecognized private Repo changes; preserve them before setup.")
        path.write_text(desired)
        write_json(self.state / "repo-compatibility.json", {"revision": revision,
                   "change": "Copy existing objects and promisor packs before removing reference alternates",
                   "path": str(path), "sha256": protection.sha256(path)})

    def protect(self, compare=False):
        baseline = self.state / "reference-before.json"
        if not compare and baseline.exists():
            print(f"Protection inventory already exists: {baseline}")
            return
        print("Reading original Git states and artifact hashes...", flush=True)
        result = protection.inventory(self.source)
        if not compare:
            write_json(baseline, result)
            print(f"Saved {len(result['projects'])} project records and {result['out']['files']} output file records.")
        else:
            if not baseline.exists():
                raise LabError("No initial protection inventory.")
            before = json.loads(baseline.read_text())
            write_json(self.state / "reference-after.json", result)
            equal = result == before
            write_json(self.state / "reference-comparison.json", {"time": now(), "equal": equal})
            if not equal:
                old = {p["path"]: p for p in before["projects"]}
                changed = [p["path"] for p in result["projects"] if old.get(p["path"]) != p]
                raise LabError(f"Original inventory changed. Projects: {changed}; output_equal={before['out'] == result['out']}")
            print("PASS: original Git state, output metadata and selected image hashes are unchanged.")

    def create_manifest(self):
        records = json.loads((self.state / "reference-before.json").read_text())["projects"]
        by_path = {r["path"]: r for r in records}
        origin = git(self.source / ".repo/manifests", "remote", "get-url", "origin")
        manifest = ET.parse(self.source / ".repo/manifests/default.xml").getroot()
        if manifest.findall("include") or manifest.findall("submanifest"):
            raise LabError("This prototype requires the inspected flat AOSP 13 manifest.")
        default = manifest.find("default")
        for remote in manifest.findall("remote"):
            remote.set("fetch", urllib.parse.urljoin(origin, remote.get("fetch")))
        for project in list(manifest.findall("project")):
            path = project.get("path", project.get("name"))
            if path not in by_path:
                manifest.remove(project)
                continue
            upstream = project.get("revision", default.get("revision"))
            project.set("revision", by_path[path]["head"])
            project.set("upstream", upstream)
        mdir = self.state / "manifests"
        if mdir.exists():
            require_clean(mdir)
            if ET.tostring(ET.parse(mdir / "default.xml").getroot()) != ET.tostring(manifest):
                # Normalize formatting before comparing the pinned content.
                existing = ET.parse(mdir / "default.xml").getroot()
                ET.indent(existing)
                ET.indent(manifest)
                if ET.tostring(existing) != ET.tostring(manifest):
                    raise LabError("Existing pinned manifest differs from the recorded reference.")
            return mdir
        mdir.mkdir()
        ET.indent(manifest)
        ET.ElementTree(manifest).write(mdir / "default.xml", encoding="utf-8", xml_declaration=True)
        run(["git", "init", "-b", "main", mdir], capture=True)
        run(["git", "-C", mdir, "add", "default.xml"])
        run(["git", "-C", mdir, "commit", "-m", "Pin existing AOSP 13 committed baseline"], capture=True)
        return mdir

    def setup(self, revision):
        require_clean(ROOT)
        if git(self.source / ".repo/manifests", "rev-parse", "HEAD") != TARGET["manifest_commit"]:
            raise LabError("Reference manifest does not match the inspected Android 13 baseline.")
        self.protect()
        manifest_dir = self.create_manifest()
        if self.tree.exists():
            self.require_owned()
        else:
            self.tree.mkdir()
            write_json(self.tree / ".research-workspace.json", {"owner": MANAGED, "source": str(self.source)})
        self.prepare_repo_tool()
        phase_file = self.state / "setup.json"
        phase = json.loads(phase_file.read_text()) if phase_file.exists() else {}
        if phase.get("source_synced") and self.product.exists():
            self.statuses(clean=True)
        if not phase.get("source_synced"):
            # Interrupted initial synchronization can already contain checkouts.
            manifest = ET.parse(manifest_dir / "default.xml").getroot()
            for project in manifest.findall("project"):
                checkout = self.tree / project.get("path", project.get("name"))
                if (checkout / ".git").exists() and git(checkout, "rev-parse", "--verify", "HEAD", check=False):
                    require_clean(checkout)
            if not (self.tree / ".repo/manifest.xml").exists():
                self.repo("init", "-u", str(manifest_dir), "-b", "main",
                          "--dissociate", "--partial-clone", "--clone-filter=blob:none",
                          "--no-repo-verify", "--no-clone-bundle")
            # The private manifest has no history in the reference AOSP manifest.
            # Use reference objects only for the source projects that share history.
            git(self.tree / ".repo/manifests", "config", "repo.reference", str(self.source))
            print("Synchronizing the independent pinned source tree...", flush=True)
            self.repo("sync", "-c", "-j", "6", "--no-clone-bundle", "--no-tags", "--no-manifest-update")
            phase["source_synced"] = True
            write_json(phase_file, phase)
        self.update_product(revision)
        self.install_agent_instructions()
        print("Research workspace ready.")

    def install_agent_instructions(self):
        self.require_owned()
        text = f"""# AOSP research workspace

Generated by aosp-research. This is the experimental tree: {self.tree}.

- Never modify the reference tree {self.source}, including its out and existing changes.
- Personal product and tool source of truth: {ROOT}. Read its AGENTS.md and README.md.
- Product checkout: {self.product}. It is pinned by commit; do not share files with symlinks.
- Core experiment repository: {self.core}; branch {TARGET['core_branch']}.
- Framework baseline: {TARGET['framework_baseline']}.
- Build output must be {self.out}. State, backups and emulator data: {self.state}.
- Inspect the precise child repository before editing. Stop on unsaved changes or unfinished Git operations.
- Never use reset --hard, clean -fd, force checkout, or force sync to resolve state.
- Run {ROOT}/lab from the personal repository for build, run, verify and snapshot.
- After initial setup, synchronize only device/kyler/research. Core switching uses ordinary Git.
- Use the lab-recorded emulator serial. Full image boot is required; module builds alone are insufficient.
- Keep each core feature in a small Git commit and snapshot it before returning to baseline.
- Android 14 and 15 are unverified. Do not download them as part of this prototype.
- Do not create additional agents or team infrastructure.
"""
        path = self.tree / "AGENTS.md"
        if path.exists() and path.read_text() != text:
            raise LabError("Existing workspace AGENTS.md differs; preserve and reconcile it manually.")
        if not path.exists():
            path.write_text(text)

    def update_product(self, revision):
        self.require_owned()
        commit = git(ROOT, "rev-parse", "--verify", f"{revision}^{{commit}}")
        manifest = self.tree / ".repo/local_manifests/research.xml"
        if self.product.exists():
            require_clean(self.product)
            if not manifest.exists():
                raise LabError("Product path exists without this tool's manifest.")
        if manifest.exists():
            old = ET.parse(manifest).getroot()
            if old.get("owner") != MANAGED:
                raise LabError("Existing research.xml is not owned by this project.")
            # Reject unpublished commits before changing a pinned checkout.
            if self.product.exists():
                head = git(self.product, "rev-parse", "HEAD")
                available = run(["git", "-C", ROOT, "for-each-ref", "--contains", head,
                                 "--format=%(refname)", "refs/heads", "refs/tags"], capture=True, check=False)
                if available.returncode or not available.stdout.strip():
                    raise LabError("Product contains commits absent from the personal repository; preserve them first.")
        root = ET.Element("manifest", {"owner": MANAGED})
        ET.SubElement(root, "remote", {"name": "research", "fetch": ROOT.parent.as_uri()})
        ET.SubElement(root, "project", {"name": ROOT.name, "path": TARGET["research_path"],
                                        "remote": "research", "revision": commit})
        manifest.parent.mkdir(parents=True, exist_ok=True)
        ET.indent(root)
        ET.ElementTree(root).write(manifest, encoding="utf-8", xml_declaration=True)
        self.repo("sync", "-c", "-j", "1", "--no-clone-bundle", "--no-tags", "--no-manifest-update", TARGET["research_path"])
        if git(self.product, "rev-parse", "HEAD") != commit:
            raise LabError("Product checkout did not reach requested commit.")

    def statuses(self, clean=False):
        self.require_owned()
        records = protection.source_inventory(self.tree)
        if clean:
            dirty = [r["path"] for r in records if r["status"]]
            if dirty:
                raise LabError(f"Unsaved source changes: {dirty}")
            for record in records:
                gitdir = self.tree / record["path"] / ".git"
                for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
                    if (gitdir / marker).exists():
                        raise LabError(f"Git operation in progress: {record['path']} / {marker}")
            require_clean(ROOT)
            require_clean(self.core)
            require_clean(self.product)
        return records

    def status(self):
        records = self.statuses()
        print(json.dumps({"tree": str(self.tree), "upstream_tag": TARGET["upstream_tag"],
                          "product": git(self.product, "rev-parse", "HEAD"),
                          "core": git(self.core, "rev-parse", "HEAD"),
                          "core_branch": git(self.core, "branch", "--show-current"),
                          "dirty": [r for r in records if r["status"]]}, indent=2, ensure_ascii=False))

    def build(self, jobs, modules):
        if self.runtime_alive():
            raise LabError("Stop the lab emulator before updating its image files.")
        records = self.statuses(clean=True)
        self.out.mkdir(exist_ok=True)
        build_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        log = self.state / "builds" / (build_id + ".log")
        log.parent.mkdir(exist_ok=True)
        metadata = {"time": now(), "jobs": jobs, "modules": modules,
                    "product_commit": git(self.product, "rev-parse", "HEAD"),
                    "core_commit": git(self.core, "rev-parse", "HEAD"),
                    "product": TARGET["product"], "variant": TARGET["variant"],
                    "out": str(self.out), "log": str(log), "projects": records}
        write_json(log.with_suffix(".json"), metadata)
        env = {**os.environ, "OUT_DIR": str(self.out), "USE_CCACHE": "0"}
        env.pop("OUT_DIR_COMMON_BASE", None)
        combo = TARGET["product"] + "-" + TARGET["variant"]
        script = 'source build/envsetup.sh && lunch "$1" && shift && m "$@"'
        print(f"Build log: {log}", flush=True)
        with log.open("w") as f:
            result = subprocess.run(["bash", "-c", script, "lab-build", combo, f"-j{jobs}", *modules],
                                    cwd=self.tree, env=env, stdout=f, stderr=subprocess.STDOUT)
        metadata["returncode"] = result.returncode
        metadata["finished"] = now()
        write_json(log.with_suffix(".json"), metadata)
        if result.returncode:
            print("\n".join(log.read_text(errors="replace").splitlines()[-65:]))
            raise LabError(f"Build failed; see {log}")
        if not modules:
            metadata.pop("projects")
            write_json(self.state / "last-full-build.json", metadata)
        print(f"Build passed: {log}", flush=True)

    @property
    def runtime_file(self):
        return self.state / "runtime/current.json"

    @staticmethod
    def process_start(pid):
        try:
            s = Path(f"/proc/{pid}/stat").read_text()
            parts = s[s.rfind(")") + 2:].split()
            return parts[19] if parts[0] != "Z" else None
        except (OSError, IndexError):
            return None

    def runtime_alive(self):
        if not self.runtime_file.exists():
            return False
        meta = json.loads(self.runtime_file.read_text())
        current = self.process_start(meta["pid"])
        return current is not None and current == meta["process_start"]

    def adb(self, *args, check=True, timeout=30):
        if not self.runtime_alive():
            raise LabError("No live lab-owned emulator.")
        meta = json.loads(self.runtime_file.read_text())
        binary = self.out / "host/linux-x86/bin/adb"
        if not binary.exists():
            binary = self.source / "out/host/linux-x86/bin/adb"
        return run([binary, "-s", meta["serial"], *args], capture=True, check=check, timeout=timeout)

    def start(self, name, port):
        self.statuses(clean=True)
        if self.runtime_alive():
            raise LabError("A lab emulator is already running; use lab stop first.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise LabError("Invalid runtime name.")
        if not 5554 <= port <= 5682 or port % 2:
            raise LabError("Emulator port must be even and between 5554 and 5682.")
        for n in (port, port + 1):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", n))
                except OSError:
                    raise LabError(f"Port {n} is already in use.")
        full_build = self.state / "last-full-build.json"
        if not full_build.exists():
            raise LabError("No successful full product build has been recorded.")
        build = json.loads(full_build.read_text())
        if build["core_commit"] != git(self.core, "rev-parse", "HEAD") or build["product_commit"] != git(self.product, "rev-parse", "HEAD"):
            raise LabError("Source commits differ from the last full build; rebuild first.")
        runtime = self.state / "runtime" / name
        if runtime.exists():
            raise LabError("Runtime name already exists; select a new name to preserve its data.")
        runtime.mkdir(parents=True)
        emulator = self.tree / "prebuilts/android-emulator/linux-x86_64/emulator"
        for path in ("system.img", "ramdisk.img", "kernel-ranchu", "userdata.img"):
            if not (self.product_out / path).exists():
                raise LabError(f"Missing image: {path}")
        env = {**os.environ, "ANDROID_BUILD_TOP": str(self.tree),
               "ANDROID_PRODUCT_OUT": str(self.product_out),
               "ANDROID_HOST_OUT": str(self.out / "host/linux-x86"),
               "ANDROID_EMULATOR_HOME": str(runtime / "emulator-home"),
               "ANDROID_AVD_HOME": str(runtime / "avd-home")}
        command = [str(emulator), "-no-window", "-gpu", "swiftshader_indirect", "-no-audio",
                   "-no-snapshot", "-no-boot-anim", "-memory", "4096", "-cores", "4", "-port", str(port),
                   "-data", str(runtime / "userdata.img"), "-cache", str(runtime / "cache.img"),
                   "-initdata", str(self.product_out / "userdata.img")]
        version_output = run([emulator, "-version"], capture=True)
        version = next(line for line in (version_output.stdout + version_output.stderr).splitlines()
                       if "Android emulator version" in line)
        with (runtime / "emulator.log").open("w") as f:
            process = subprocess.Popen(command, cwd=self.tree, env=env, stdout=f, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        meta = {"name": name, "pid": process.pid, "process_start": self.process_start(process.pid),
                "serial": f"emulator-{port}", "directory": str(runtime), "command": command,
                "build": build, "emulator_version": version, "time": now()}
        write_json(self.runtime_file, meta)
        write_json(runtime / "run.json", meta)
        print(f"Started {meta['serial']} ({name}); log: {runtime / 'emulator.log'}")

    def stop(self):
        if not self.runtime_alive():
            print("No live lab-owned emulator; nothing stopped.")
            return
        self.adb("emu", "kill")
        end = time.monotonic() + 30
        while self.runtime_alive() and time.monotonic() < end:
            time.sleep(0.5)
        if self.runtime_alive():
            raise LabError("Emulator has not exited; inspect its process before retrying.")
        print("Lab emulator stopped.")

    def verify(self, expect, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = self.adb("shell", "getprop", "sys.boot_completed", check=False)
            if result.returncode == 0 and result.stdout.strip() == "1":
                break
            print("Waiting for Android boot...", flush=True)
            time.sleep(5)
        else:
            raise LabError("Android did not finish booting before timeout.")
        info = json.loads(self.adb("shell", "/product/bin/research-info", "--json").stdout)
        core = self.adb("shell", "cmd", "activity", "research-status", check=False)
        enabled = core.returncode == 0 and "research.framework=enabled" in core.stdout
        help_text = self.adb("shell", "cmd", "activity", "help").stdout
        checks = {"android_sdk": info.get("sdk") == TARGET["sdk"],
                  "product_flavor": info.get("build_flavor") == TARGET["product"] + "-" + TARGET["variant"],
                  "tool_version": info.get("tool_version") == "0.1.0",
                  "abi": info.get("abi") == "x86_64", "fingerprint": bool(info.get("fingerprint")),
                  "core_presence": enabled == (expect == "experiment"),
                  "help_presence": ("research-status" in help_text) == (expect == "experiment")}
        if expect == "experiment":
            checks["core_sdk"] = f"sdk={TARGET['sdk']}" in core.stdout
        else:
            checks["unknown_command"] = core.returncode != 0 and "Unknown command" in (core.stdout + core.stderr)
        meta = json.loads(self.runtime_file.read_text())
        directory = Path(meta["directory"])
        result = {"time": now(), "expect": expect, "serial": meta["serial"], "info": info,
                  "core_output": core.stdout + core.stderr, "core_returncode": core.returncode,
                  "checks": checks, "passed": all(checks.values()), "build": meta["build"],
                  "emulator_version": meta.get("emulator_version", "unrecorded")}
        write_json(directory / "verification.json", result)
        (directory / "logcat.txt").write_text(self.adb("logcat", "-d").stdout)
        (directory / "crashes.txt").write_text(self.adb("logcat", "-b", "crash", "-d").stdout)
        binary = self.out / "host/linux-x86/bin/adb"
        with (directory / "screen.png").open("wb") as f:
            subprocess.run([str(binary), "-s", meta["serial"], "exec-out", "screencap", "-p"], stdout=f, check=True)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result["passed"]:
            raise LabError("Device checks failed; inspect verification.json.")

    def seed_core_backup(self, bare):
        """Save available upstream objects without downloading promised history."""
        baseline_ref = "refs/heads/upstream/a13-baseline"
        if git(bare, "rev-parse", "--verify", baseline_ref, check=False):
            if git(bare, "rev-parse", baseline_ref) != TARGET["framework_baseline"]:
                raise LabError("Saved framework baseline identity differs.")
            return
        objects = Path(git(self.core, "rev-parse", "--git-path", "objects"))
        if not objects.is_absolute():
            objects = self.core / objects
        alternates = bare / "objects/info/alternates"
        alternates.parent.mkdir(exist_ok=True)
        expected = str(objects.resolve()) + "\n"
        if alternates.exists() and alternates.read_text() != expected:
            raise LabError("Unrecognized backup object reference.")
        alternates.write_text(expected)
        copy_objects(bare / "objects")
        alternates.unlink()
        git(bare, "config", "core.repositoryformatversion", "1")
        git(bare, "config", "extensions.partialclone", "upstream")
        git(bare, "config", "remote.upstream.url", "https://android.googlesource.com/platform/frameworks/base")
        git(bare, "config", "remote.upstream.promisor", "true")
        git(bare, "config", "remote.upstream.partialclonefilter", "blob:none")
        git(bare, "config", "uploadpack.allowFilter", "true")
        git(bare, "update-ref", baseline_ref, TARGET["framework_baseline"])

    def snapshot(self, name, images=False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise LabError("Invalid snapshot name.")
        self.statuses(clean=True)
        directory = self.state / "snapshots" / name
        if directory.exists():
            raise LabError("Snapshot already exists; use a new name.")
        commits = {}
        for key, path in (("product", self.product), ("frameworks-base", self.core)):
            bare = self.state / "git" / (key + ".git")
            if not bare.exists():
                bare.parent.mkdir(parents=True, exist_ok=True)
                run(["git", "init", "--bare", bare], capture=True)
            if key == "frameworks-base":
                self.seed_core_backup(bare)
            head = git(path, "rev-parse", "HEAD")
            run(["git", "-C", path, "push", str(bare), f"{head}:refs/heads/snapshots/{name}"], capture=True)
            if git(bare, "rev-parse", f"refs/heads/snapshots/{name}") != head:
                raise LabError("Saved Git reference does not match source.")
            if key == "frameworks-base" and git(path, "branch", "--show-current") == TARGET["core_branch"]:
                run(["git", "-C", path, "push", str(bare), f"HEAD:refs/heads/{TARGET['core_branch']}"], capture=True)
            commits[key] = {"commit": head, "repository": str(bare), "ref": f"refs/heads/snapshots/{name}"}
        manifest_text = self.repo("manifest", "-r", capture=True).stdout
        root = ET.fromstring(manifest_text)
        ET.SubElement(root, "remote", {"name": "research-snapshot", "fetch": (self.state / "git").as_uri()})
        for element in root.findall("project"):
            path = element.get("path", element.get("name"))
            key = "product" if path == TARGET["research_path"] else "frameworks-base" if path == "frameworks/base" else None
            if key:
                element.set("name", key + ".git")
                element.set("path", path)
                element.set("remote", "research-snapshot")
                element.set("upstream", commits[key]["ref"])
        directory.mkdir(parents=True)
        ET.indent(root)
        ET.ElementTree(root).write(directory / "manifest.xml", encoding="utf-8", xml_declaration=True)
        runtime = json.loads(self.runtime_file.read_text()) if self.runtime_file.exists() else None
        verification = None
        if runtime:
            verification_file = Path(runtime["directory"]) / "verification.json"
            if verification_file.exists():
                verification = json.loads(verification_file.read_text())
        image_records = {}
        if images:
            build_file = self.state / "last-full-build.json"
            build = json.loads(build_file.read_text()) if build_file.exists() else {}
            if build.get("core_commit") != commits["frameworks-base"]["commit"] or build.get("product_commit") != commits["product"]["commit"]:
                raise LabError("Images do not match the selected source commits.")
            image_dir = directory / "images"
            image_dir.mkdir()
            paths = list(self.product_out.glob("*.img")) + list(self.product_out.glob("*.ini"))
            paths += [self.product_out / "kernel-ranchu"]
            for source in paths:
                if source.is_file():
                    target = image_dir / source.name
                    run(["cp", "--reflink=auto", "--sparse=always", str(source), str(target)])
                    image_records[source.name] = {"size": target.stat().st_size, "sha256": protection.sha256(target)}
        write_json(directory / "snapshot.json", {"images": image_records, "time": now(), "name": name, "commits": commits,
                   "target": TARGET, "tree": str(self.tree), "verification": verification,
                   "history": "Core commit history and locally available blobs saved; absent upstream historical blobs remain promised by googlesource."})
        print(f"Saved Git-backed snapshot: {directory}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("configure")
    for name in ("source", "tree", "state"):
        p.add_argument("--" + name, required=True)
    p = sub.add_parser("setup")
    p.add_argument("target", choices=["aosp13"])
    p.add_argument("--revision", default="HEAD")
    sub.add_parser("status")
    p = sub.add_parser("build")
    p.add_argument("--jobs", type=int, default=TARGET["default_jobs"])
    p.add_argument("modules", nargs="*")
    p = sub.add_parser("run")
    p.add_argument("--name", required=True)
    p.add_argument("--port", type=int, default=5580)
    sub.add_parser("stop")
    p = sub.add_parser("verify")
    p.add_argument("--expect", choices=["baseline", "experiment"], required=True)
    p.add_argument("--timeout", type=int, default=300)
    p = sub.add_parser("snapshot")
    p.add_argument("name")
    p.add_argument("--images", action="store_true", help="Also preserve the matching complete build images")
    p = sub.add_parser("protect")
    p.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            paths = validate_paths(args.source, args.tree, args.state)
            config = dict(zip(("source", "tree", "state"), map(str, paths)))
            p = ROOT / ".lab.local.json"
            if p.exists() and json.loads(p.read_text()) != config:
                raise LabError("Configuration already exists with different paths.")
            write_json(p, config)
            print("Local paths configured (not tracked by Git).")
            return
        lab = Lab()
        with lab.lock():
            if args.command == "setup":
                lab.setup(args.revision)
            elif args.command == "status":
                lab.status()
            elif args.command == "build":
                if args.jobs < 1:
                    raise LabError("jobs must be positive")
                lab.build(args.jobs, args.modules)
            elif args.command == "run":
                lab.start(args.name, args.port)
            elif args.command == "stop":
                lab.stop()
            elif args.command == "verify":
                lab.verify(args.expect, args.timeout)
            elif args.command == "snapshot":
                lab.snapshot(args.name, args.images)
            elif args.command == "protect":
                lab.protect(args.compare)
    except (LabError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
