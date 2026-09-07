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
        if not launcher.exists():
            launcher = self.source / ".repo/repo/repo"
        # Repo 2.59 marks object stores precious, then uses repack -a -d to
        # dissociate. Git 2.34 rejects that combination. Override the setting
        # only for this process operating in our independently owned checkout;
        # no reference-tree configuration is edited.
        env = dict(os.environ)
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        env[f"GIT_CONFIG_KEY_{count}"] = "extensions.preciousObjects"
        env[f"GIT_CONFIG_VALUE_{count}"] = "false"
        env["GIT_CONFIG_COUNT"] = str(count + 1)
        return run([sys.executable, launcher, *args], cwd=self.tree, capture=capture, env=env)

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
        phase_file = self.state / "setup.json"
        phase = json.loads(phase_file.read_text()) if phase_file.exists() else {}
        if not phase.get("source_synced"):
            if not (self.tree / ".repo/manifest.xml").exists():
                repo_rev = git(self.source / ".repo/repo", "rev-parse", "HEAD")
                self.repo("init", "-u", str(manifest_dir), "-b", "main",
                          "--reference", str(self.source), "--dissociate",
                          "--repo-url", str(self.source / ".repo/repo"),
                          "--repo-rev", repo_rev, "--no-repo-verify", "--no-clone-bundle")
            print("Synchronizing the independent pinned source tree...", flush=True)
            self.repo("sync", "-c", "-j", "6", "--no-clone-bundle", "--no-tags", "--no-manifest-update")
            phase["source_synced"] = True
            write_json(phase_file, phase)
        self.update_product(revision)
        print("Research workspace ready.")

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
        self.require_owned()
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
        with (runtime / "emulator.log").open("w") as f:
            process = subprocess.Popen(command, cwd=self.tree, env=env, stdout=f, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        meta = {"name": name, "pid": process.pid, "process_start": self.process_start(process.pid),
                "serial": f"emulator-{port}", "directory": str(runtime), "command": command,
                "build": build, "time": now()}
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
                  "checks": checks, "passed": all(checks.values()), "build": meta["build"]}
        write_json(directory / "verification.json", result)
        (directory / "logcat.txt").write_text(self.adb("logcat", "-d").stdout)
        (directory / "crashes.txt").write_text(self.adb("logcat", "-b", "crash", "-d").stdout)
        binary = self.out / "host/linux-x86/bin/adb"
        with (directory / "screen.png").open("wb") as f:
            subprocess.run([str(binary), "-s", meta["serial"], "exec-out", "screencap", "-p"], stdout=f, check=True)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result["passed"]:
            raise LabError("Device checks failed; inspect verification.json.")

    def snapshot(self, name):
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
            head = git(path, "rev-parse", "HEAD")
            run(["git", "-C", path, "push", str(bare), f"{head}:refs/heads/snapshots/{name}"], capture=True)
            if git(bare, "rev-parse", f"refs/heads/snapshots/{name}") != head:
                raise LabError("Saved Git reference does not match source.")
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
        write_json(directory / "snapshot.json", {"time": now(), "name": name, "commits": commits,
                                                 "target": TARGET, "tree": str(self.tree)})
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
                lab.snapshot(args.name)
            elif args.command == "protect":
                lab.protect(args.compare)
    except (LabError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
