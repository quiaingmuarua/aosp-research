import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import lab
from scripts.dissociate import copy_objects


def init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "file").write_text("original\n")
    subprocess.run(["git", "-C", str(path), "add", "file"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "baseline"], check=True, capture_output=True)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_nested_source_and_workspace_rejected(self):
        for tree in (self.base / "source", self.base / "source/work", self.base):
            with self.assertRaises(lab.LabError):
                lab.validate_paths(self.base / "source", tree, self.base / "state", self.base / "code")

    def test_symlink_cannot_bypass_path_isolation(self):
        (self.base / "source").mkdir()
        (self.base / "alias").symlink_to(self.base / "source", target_is_directory=True)
        with self.assertRaises(lab.LabError):
            lab.validate_paths(self.base / "source", self.base / "alias/work", self.base / "state", self.base / "code")

    def test_dirty_and_untracked_files_are_preserved(self):
        repo = self.base / "repo"
        init_repo(repo)
        lab.require_clean(repo)
        (repo / "file").write_text("my work\n")
        (repo / "untracked").write_text("keep this")
        with self.assertRaises(lab.LabError):
            lab.require_clean(repo)
        self.assertEqual((repo / "file").read_text(), "my work\n")
        self.assertEqual((repo / "untracked").read_text(), "keep this")

    def test_unfinished_git_operation_is_rejected(self):
        repo = self.base / "repo"
        init_repo(repo)
        (repo / ".git/CHERRY_PICK_HEAD").write_text(lab.git(repo, "rev-parse", "HEAD") + "\n")
        with self.assertRaises(lab.LabError):
            lab.require_clean(repo)

    def test_core_branch_survives_return_to_baseline_and_independent_clone(self):
        repo = self.base / "repo"
        init_repo(repo)
        base = lab.git(repo, "rev-parse", "HEAD")
        lab.git(repo, "switch", "-c", "research/a13/prototype")
        (repo / "file").write_text("research change\n")
        lab.git(repo, "add", "file")
        lab.git(repo, "commit", "-m", "research feature")
        feature = lab.git(repo, "rev-parse", "HEAD")
        saved = self.base / "saved.git"
        lab.run(["git", "init", "--bare", saved], capture=True)
        lab.git(repo, "push", str(saved), "HEAD:refs/heads/snapshots/demo")
        lab.git(repo, "switch", "--detach", base)
        self.assertEqual((repo / "file").read_text(), "original\n")
        restored = self.base / "restored"
        lab.run(["git", "clone", "--no-local", "--branch", "snapshots/demo", saved, restored], capture=True)
        self.assertEqual(lab.git(restored, "rev-parse", "HEAD"), feature)
        self.assertEqual((restored / "file").read_text(), "research change\n")
        lab.git(repo, "switch", "research/a13/prototype")
        self.assertEqual(lab.git(repo, "rev-parse", "HEAD"), feature)

    def test_dead_process_is_not_owned_emulator(self):
        obj = object.__new__(lab.Lab)
        obj.state = self.base
        lab.write_json(obj.runtime_file, {"pid": 123, "process_start": None})
        with patch.object(lab.Lab, "process_start", return_value=None):
            self.assertFalse(obj.runtime_alive())

    def test_dissociate_precious_repository_keeps_reference_untouched(self):
        source = self.base / "source"
        init_repo(source)
        baseline = lab.git(source, "rev-parse", "HEAD")
        clone = self.base / "clone"
        lab.run(["git", "clone", "--shared", source, clone], capture=True)
        lab.git(clone, "config", "core.repositoryformatversion", "1")
        lab.git(clone, "config", "extensions.preciousObjects", "true")
        result = lab.run(["git", "-C", clone, "repack", "-a", "-d"], capture=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        copy_objects(clone / ".git/objects")
        copy_objects(clone / ".git/objects")
        (clone / ".git/objects/info/alternates").unlink()
        source.rename(self.base / "source-preserved")
        self.assertEqual(lab.git(clone, "rev-parse", "HEAD"), baseline)
        self.assertEqual(lab.git(clone, "show", "HEAD:file"), "original")
        self.assertEqual(lab.git(self.base / "source-preserved", "status", "--porcelain"), "")

    def test_product_update_retry_is_idempotent_and_preserves_private_work(self):
        personal = self.base / "personal"
        init_repo(personal)
        obj = object.__new__(lab.Lab)
        obj.tree = self.base / "tree"
        obj.source = self.base / "source"
        obj.tree.mkdir()
        obj.product = obj.tree / lab.TARGET["research_path"]
        lab.write_json(obj.tree / ".research-workspace.json",
                       {"owner": lab.MANAGED, "source": str(obj.source)})

        def sync(*args, **kwargs):
            if not obj.product.exists():
                obj.product.parent.mkdir(parents=True)
                lab.run(["git", "clone", personal, obj.product], capture=True)
            lab.git(obj.product, "fetch", str(personal), "main")
            lab.git(obj.product, "switch", "--detach", lab.git(personal, "rev-parse", "HEAD"))

        with patch.object(lab, "ROOT", personal), patch.object(obj, "repo", side_effect=sync):
            obj.update_product("main")
            baseline = lab.git(obj.product, "rev-parse", "HEAD")
            obj.update_product("main")
            self.assertEqual(lab.git(obj.product, "rev-parse", "HEAD"), baseline)
            (personal / "file").write_text("updated product")
            lab.git(personal, "commit", "-am", "update")
            with patch.object(obj, "repo", side_effect=lab.LabError("interrupted sync")):
                with self.assertRaises(lab.LabError):
                    obj.update_product("main")
            self.assertEqual(lab.git(obj.product, "rev-parse", "HEAD"), baseline)
            obj.update_product("main")
            self.assertEqual(lab.git(obj.product, "rev-parse", "HEAD"), lab.git(personal, "rev-parse", "HEAD"))
            (obj.product / "file").write_text("private unsaved")
            with self.assertRaises(lab.LabError):
                obj.update_product("main")
            self.assertEqual((obj.product / "file").read_text(), "private unsaved")
            lab.git(obj.product, "commit", "-am", "private feature")
            private = lab.git(obj.product, "rev-parse", "HEAD")
            with self.assertRaisesRegex(lab.LabError, "absent from the personal repository"):
                obj.update_product("main")
            self.assertEqual(lab.git(obj.product, "rev-parse", "HEAD"), private)

    def test_promised_history_backup_keeps_private_feature_offline(self):
        upstream = self.base / "upstream"
        init_repo(upstream)
        lab.git(upstream, "config", "uploadpack.allowFilter", "true")
        (upstream / "file").write_text("current baseline")
        lab.git(upstream, "commit", "-am", "baseline two")
        core = self.base / "core"
        lab.run(["git", "clone", "--filter=blob:none", upstream.as_uri(), core], capture=True)
        baseline = lab.git(core, "rev-parse", "HEAD")
        obj = object.__new__(lab.Lab)
        obj.core = core
        bare = self.base / "core-backup.git"
        lab.run(["git", "init", "--bare", bare], capture=True)
        with patch.dict(lab.TARGET, {"framework_baseline": baseline}):
            obj.seed_core_backup(bare)
            obj.seed_core_backup(bare)
        (core / "file").write_text("private feature")
        lab.git(core, "commit", "-am", "experiment")
        feature = lab.git(core, "rev-parse", "HEAD")
        lab.git(core, "push", str(bare), "HEAD:refs/heads/research/a13/prototype")
        upstream.rename(self.base / "upstream-unavailable")
        core.rename(self.base / "core-unavailable")
        lab.git(bare, "config", "remote.upstream.url", str(self.base / "offline"))
        restored = self.base / "restored"
        lab.run(["git", "clone", "--filter=blob:none", "--branch", "research/a13/prototype", bare.as_uri(), restored], capture=True)
        self.assertEqual(lab.git(restored, "rev-parse", "HEAD"), feature)
        self.assertEqual((restored / "file").read_text(), "private feature")

    def test_workspace_identity_required(self):
        obj = object.__new__(lab.Lab)
        obj.tree = self.base / "unrelated"
        obj.source = self.base / "source"
        obj.tree.mkdir()
        (obj.tree / "keep").write_text("existing data")
        with self.assertRaises(lab.LabError):
            obj.require_owned()
        self.assertEqual((obj.tree / "keep").read_text(), "existing data")


class NativeToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temp.name) / "research-info"
        src = lab.ROOT / "modules/research-info/main.cpp"
        subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(src), "-o", str(cls.binary)], check=True)
        cls.json_test = Path(cls.temp.name) / "json-test.cpp"
        cls.json_test.write_text('#define main tool_main\n#include "' + str(src) + '"\n#undef main\n'
                                 'int main() { std::puts(json_string("quote\\\" slash\\\\ line\\n tab\\t").c_str()); }\n')
        cls.json_binary = Path(cls.temp.name) / "json-test"
        subprocess.run(["c++", "-std=c++17", str(cls.json_test), "-o", str(cls.json_binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_machine_readable_output_and_invalid_arguments(self):
        result = subprocess.run([str(self.binary), "--json"], capture_output=True, text=True, check=True)
        obj = json.loads(result.stdout)
        self.assertEqual(obj["tool_version"], "0.1.0")
        self.assertEqual(set(obj), {"tool_version", "android_release", "sdk", "build_id", "fingerprint", "abi", "build_flavor"})
        result = subprocess.run([str(self.binary), "--json", "unexpected"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_json_control_characters_round_trip(self):
        result = subprocess.run([str(self.json_binary)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), 'quote" slash\\ line\n tab\t')


if __name__ == "__main__":
    unittest.main()
