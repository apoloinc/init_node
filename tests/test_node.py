"""Comprehensive stdlib unittest suite for init_node."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import CLI module under test
import node


class TestManifestAndConfig(unittest.TestCase):
    """Test manifest integrity, profile contracts, and schema invariants."""

    def test_default_manifest_structure(self):
        manifest = node.load_manifest()
        self.assertIn("profiles", manifest)
        self.assertIn("modules", manifest)

        # Invariant: Required profiles
        for required_profile in ("coordinator", "rag", "inference"):
            self.assertIn(required_profile, manifest["profiles"])

        # Check coordinator modules
        coord_modules = manifest["profiles"]["coordinator"]
        self.assertIn("apolo-knowledge-data", coord_modules)
        self.assertIn("korax", coord_modules)
        self.assertIn("apolo-skill-standard", coord_modules)

        # Check rag modules
        rag_modules = manifest["profiles"]["rag"]
        self.assertIn("apolo-rag", rag_modules)
        self.assertIn("apolo-skill-standard", rag_modules)

        # Check inference modules
        inf_modules = manifest["profiles"]["inference"]
        self.assertIn("korax", inf_modules)

        # Validate module specifications
        sha_regex = re.compile(r"^[0-9a-f]{40}$")
        for mod_name, spec in manifest["modules"].items():
            self.assertIn("url", spec)
            self.assertIn("ref", spec)
            self.assertTrue(spec["url"].startswith("https://github.com/"), f"Bad URL in {mod_name}")
            self.assertTrue(sha_regex.match(spec["ref"]), f"SHA for {mod_name} must be 40-hex: {spec['ref']}")
            self.assertIn(spec.get("runtime"), ("uv-project", "knowledge-lock", "stdlib", "build-standard"))

    def test_disk_manifest_matches_default(self):
        disk_manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
        self.assertTrue(disk_manifest_path.is_file(), "manifest.json must exist in repo root")
        with open(disk_manifest_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)

        for profile in ("coordinator", "rag", "inference"):
            self.assertEqual(
                disk_data["profiles"][profile],
                node.load_manifest()["profiles"][profile],
            )
        for mod_name in disk_data["modules"]:
            self.assertEqual(
                disk_data["modules"][mod_name]["ref"],
                node.load_manifest()["modules"][mod_name]["ref"],
            )

    def test_normalize_git_url(self):
        self.assertEqual(
            node.normalize_git_url("https://github.com/apoloinc/apolo-korax.git"),
            "https://github.com/apoloinc/apolo-korax",
        )
        self.assertEqual(
            node.normalize_git_url("HTTPS://GITHUB.COM/apoloinc/apolo-korax/"),
            "https://github.com/apoloinc/apolo-korax",
        )

    def test_resolve_safe_root(self):
        with self.assertRaises(ValueError):
            node.resolve_safe_root("")
        with self.assertRaises(ValueError):
            node.resolve_safe_root("   ")

        # Root filesystem protection
        with self.assertRaises(ValueError):
            node.resolve_safe_root("/")
        if sys.platform == "win32":
            with self.assertRaises(ValueError):
                node.resolve_safe_root("C:\\")

    def test_load_manifest_invalid_or_missing(self):
        with self.assertRaises(FileNotFoundError):
            node.load_manifest("non_existent_manifest.json")

    def test_module_directory_traversal_refusal(self):
        unsafe_spec = {
            "url": "https://github.com/apoloinc/apolo-korax.git",
            "ref": "f3773579699f86430a303659664fe7dfa032b39c",
            "dir_name": "../escaped_dir",
            "runtime": "stdlib",
        }
        with self.assertRaises(ValueError) as ctx:
            node.check_and_sync_module("unsafe", unsafe_spec, Path("/tmp/repos"))
        self.assertIn("Invalid directory name", str(ctx.exception))

    def test_install_stdlib_runtime_infers_without_uv(self):
        spec = {
            "url": "https://github.com/apoloinc/apolo-korax.git",
            "ref": "f3773579699f86430a303659664fe7dfa032b39c",
            "dir_name": "korax",
            "runtime": "stdlib",
        }
        with patch("shutil.which", return_value=None):
            res = node.install_module_dependencies("korax", spec, Path("/tmp/repos"))
            self.assertEqual(res["status"], "inferred_stdlib")
            self.assertEqual(res["runtime"], "stdlib")


class TestInitAndDoctorWithGitFixtures(unittest.TestCase):
    """Test git cloning, idempotence, refusal to overwrite dirty/unexpected revisions, and doctor checks."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_init_node_")
        self.temp_path = Path(self.temp_dir)

        # Create upstream fixture git repo
        self.upstream_dir = self.temp_path / "upstream_fixture"
        self.upstream_dir.mkdir()
        subprocess.run(["git", "init"], cwd=self.upstream_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test Runner"],
            cwd=self.upstream_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@apolo.test"],
            cwd=self.upstream_dir,
            capture_output=True,
            check=True,
        )

        test_file = self.upstream_dir / "sample.txt"
        test_file.write_text("initial commit content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.upstream_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=self.upstream_dir,
            capture_output=True,
            check=True,
        )

        # Get initial commit SHA
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.upstream_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        self.initial_sha = res.stdout.strip()
        self.upstream_url = self.upstream_dir.as_uri()

        # Build custom test manifest referencing upstream fixture
        self.custom_manifest = {
            "manifest_version": "1.0",
            "profiles": {
                "test_profile": ["fixture-module"],
            },
            "modules": {
                "fixture-module": {
                    "url": self.upstream_url,
                    "ref": self.initial_sha,
                    "dir_name": "fixture-module",
                    "runtime": "stdlib",
                    "description": "Test fixture module",
                }
            },
        }

        self.manifest_file = self.temp_path / "test_manifest.json"
        with open(self.manifest_file, "w", encoding="utf-8") as f:
            json.dump(self.custom_manifest, f)

        self.workspace_root = self.temp_path / "workspace"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_fresh_clone_and_state(self):
        args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )

        code = node.cmd_init(args)
        self.assertEqual(code, 0)

        # Check cloned repo
        cloned_repo = self.workspace_root / "repos" / "fixture-module"
        self.assertTrue(cloned_repo.is_dir())
        self.assertTrue((cloned_repo / "sample.txt").is_file())

        # Check detached checkout at expected SHA
        head_sha = node.get_git_commit(cloned_repo)
        self.assertEqual(head_sha.lower(), self.initial_sha.lower())

        # Check persisted state.json
        state_path = self.workspace_root / "state.json"
        self.assertTrue(state_path.is_file())
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["profile"], "test_profile")
        self.assertEqual(state["modules"]["fixture-module"]["commit"].lower(), self.initial_sha.lower())

    def test_init_idempotence(self):
        args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )

        # First run: clones
        code1 = node.cmd_init(args)
        self.assertEqual(code1, 0)

        # Second run: clean, matching origin & SHA -> succeeds idempotently
        code2 = node.cmd_init(args)
        self.assertEqual(code2, 0)

    def test_init_refuses_dirty_worktree(self):
        args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )
        self.assertEqual(node.cmd_init(args), 0)

        cloned_repo = self.workspace_root / "repos" / "fixture-module"
        # Make working tree dirty by modifying tracked file
        dirty_file = cloned_repo / "sample.txt"
        dirty_file.write_text("dirty uncommitted change", encoding="utf-8")

        # Refusal check: calling check_and_sync_module directly raises RuntimeError
        repos_dir = self.workspace_root / "repos"
        with self.assertRaises(RuntimeError) as ctx:
            node.check_and_sync_module(
                "fixture-module",
                self.custom_manifest["modules"]["fixture-module"],
                repos_dir,
            )
        self.assertIn("Refusing to overwrite dirty repository", str(ctx.exception))

        # CLI should exit non-zero
        self.assertEqual(node.cmd_init(args), 1)

    def test_init_refuses_wrong_origin(self):
        args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )
        self.assertEqual(node.cmd_init(args), 0)

        cloned_repo = self.workspace_root / "repos" / "fixture-module"
        # Change origin URL
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/fake/wrong-repo.git"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        repos_dir = self.workspace_root / "repos"
        with self.assertRaises(RuntimeError) as ctx:
            node.check_and_sync_module(
                "fixture-module",
                self.custom_manifest["modules"]["fixture-module"],
                repos_dir,
            )
        self.assertIn("does not match expected", str(ctx.exception))
        self.assertIn("Refusing to overwrite", str(ctx.exception))

    def test_init_refuses_unexpected_revision(self):
        args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )
        self.assertEqual(node.cmd_init(args), 0)

        # Add a new commit in cloned repo and checkout
        cloned_repo = self.workspace_root / "repos" / "fixture-module"
        new_file = cloned_repo / "extra.txt"
        new_file.write_text("second commit", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=cloned_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Unexpected commit"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        repos_dir = self.workspace_root / "repos"
        with self.assertRaises(RuntimeError) as ctx:
            node.check_and_sync_module(
                "fixture-module",
                self.custom_manifest["modules"]["fixture-module"],
                repos_dir,
            )
        self.assertIn("Refusing to overwrite unexpected revision", str(ctx.exception))

    def test_init_install_missing_uv_raises_actionable_error(self):
        # Create module spec configured with runtime uv
        uv_manifest = {
            "manifest_version": "1.0",
            "profiles": {"uv_profile": ["uv-module"]},
            "modules": {
                "uv-module": {
                    "url": self.upstream_url,
                    "ref": self.initial_sha,
                    "dir_name": "uv-module",
                    "runtime": "uv-project",
                }
            },
        }
        uv_manifest_file = self.temp_path / "uv_manifest.json"
        with open(uv_manifest_file, "w", encoding="utf-8") as f:
            json.dump(uv_manifest, f)

        args = argparse.Namespace(
            profile="uv_profile",
            root=str(self.workspace_root),
            manifest=str(uv_manifest_file),
            install=True,
        )

        with patch("shutil.which", return_value=None):
            # When uv is absent, install must fail with actionable guidance and no curl pipe
            repos_dir = self.workspace_root / "repos"
            repos_dir.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(RuntimeError) as ctx:
                node.install_module_dependencies("uv-module", uv_manifest["modules"]["uv-module"], repos_dir)
            err_msg = str(ctx.exception)
            self.assertIn("requires 'uv' for dependency management", err_msg)
            self.assertIn("never uses curl pipe", err_msg)

    def test_doctor_success_and_failures(self):
        init_args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )
        self.assertEqual(node.cmd_init(init_args), 0)

        # 1. Doctor on clean workspace -> success (0)
        doc_args = argparse.Namespace(
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            url=[],
            json=True,
        )
        code = node.cmd_doctor(doc_args)
        self.assertEqual(code, 0)

        # 2. Make repository dirty -> doctor detects dirty and returns 1
        cloned_repo = self.workspace_root / "repos" / "fixture-module"
        dirty_file = cloned_repo / "untracked.txt"
        dirty_file.write_text("untracked file", encoding="utf-8")

        code_dirty = node.cmd_doctor(doc_args)
        self.assertEqual(code_dirty, 1)

        # Clean up untracked file
        dirty_file.unlink()
        self.assertEqual(node.cmd_doctor(doc_args), 0)

        # 3. Doctor on missing state.json -> returns 1
        state_file = self.workspace_root / "state.json"
        state_file.unlink()
        self.assertEqual(node.cmd_doctor(doc_args), 1)

    def test_doctor_http_health_check(self):
        init_args = argparse.Namespace(
            profile="test_profile",
            root=str(self.workspace_root),
            manifest=str(self.manifest_file),
            install=False,
        )
        self.assertEqual(node.cmd_init(init_args), 0)

        # Successful HTTP 200
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            doc_args = argparse.Namespace(
                root=str(self.workspace_root),
                manifest=str(self.manifest_file),
                url=["http://localhost:8000/health"],
                json=True,
            )
            self.assertEqual(node.cmd_doctor(doc_args), 0)

        # Failed HTTP 500
        http_err = urllib.error.HTTPError("http://localhost:8000/health", 500, "Internal Error", {}, None)
        with patch("urllib.request.urlopen", side_effect=http_err):
            doc_args = argparse.Namespace(
                root=str(self.workspace_root),
                manifest=str(self.manifest_file),
                url=["http://localhost:8000/health"],
                json=True,
            )
            self.assertEqual(node.cmd_doctor(doc_args), 1)


class TestPeerInterconnection(unittest.TestCase):
    """Test SSH peer connectivity, regex safety, and token roundtrip verification."""

    def test_peer_regex_validation(self):
        # Valid aliases/hostnames
        for valid in ("deb", "hadron", "zephyrus", "node-1.lan", "gpu_worker_01"):
            self.assertTrue(node.PEER_REGEX.match(valid), f"Should accept valid peer: {valid}")

        # Invalid/malicious hostnames (injection attempts)
        for invalid in (
            "-oProxyCommand=calc.exe",
            "; rm -rf /",
            "host with spaces",
            "deb & echo pwned",
            ">output",
            "|sh",
        ):
            res = node.check_single_ssh_peer(invalid)
            self.assertFalse(res["ok"])
            self.assertIn("Invalid peer hostname", res["error"])

    def test_peer_successful_roundtrip(self):
        # Mock SSH subprocess returning expected JSON echo
        def fake_run(cmd, input, **kwargs):
            req = json.loads(input)
            nonce = req["token"]
            stdout_data = json.dumps({"status": "ok", "echo": nonce, "platform": "linux", "py": "3.11.2"})
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = stdout_data
            mock_proc.stderr = ""
            return mock_proc

        with patch("subprocess.run", side_effect=fake_run) as mock_sub:
            res = node.check_single_ssh_peer("deb", timeout=5)
            self.assertTrue(res["ok"])
            self.assertTrue(res["token_verified"])
            self.assertEqual(res["remote_platform"], "linux")
            self.assertEqual(res["remote_python"], "3.11.2")

            # Check that ssh argument list was used without shell=True
            called_cmd = mock_sub.call_args[0][0]
            self.assertEqual(called_cmd[0], "ssh")
            self.assertEqual(called_cmd[1:5], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
            self.assertEqual(called_cmd[5], "deb")
            self.assertFalse(mock_sub.call_args[1].get("shell", False))

    def test_peer_token_mismatch(self):
        def fake_run(cmd, input, **kwargs):
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = json.dumps({"status": "ok", "echo": "WRONG_TOKEN", "platform": "linux"})
            mock_proc.stderr = ""
            return mock_proc

        with patch("subprocess.run", side_effect=fake_run):
            res = node.check_single_ssh_peer("hadron", timeout=5)
            self.assertFalse(res["ok"])
            self.assertIn("Token mismatch", res["error"])

    def test_peer_timeout_and_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=5)):
            res = node.check_single_ssh_peer("hadron", timeout=5)
            self.assertFalse(res["ok"])
            self.assertIn("timed out", res["error"])

        mock_proc_fail = MagicMock(returncode=255, stdout="", stderr="Host unreachable")
        with patch("subprocess.run", return_value=mock_proc_fail):
            res = node.check_single_ssh_peer("hadron", timeout=5)
            self.assertFalse(res["ok"])
            self.assertIn("Host unreachable", res["error"])

    def test_cmd_check_peers_topology_loading(self):
        topology_path = Path(__file__).resolve().parent.parent / "examples" / "topology.json"
        self.assertTrue(topology_path.is_file())

        with patch("node.check_single_ssh_peer") as mock_chk:
            mock_chk.return_value = {"peer": "mock", "ok": True, "token_verified": True}
            args = argparse.Namespace(
                peers=[],
                topology=str(topology_path),
                timeout=5,
                json=True,
            )
            code = node.cmd_check_peers(args)
            self.assertEqual(code, 0)
            # Checked remote peers from topology (deb and hadron, skipping local zephyrus)
            called_peers = [call[0][0] for call in mock_chk.call_args_list]
            self.assertIn("deb", called_peers)
            self.assertIn("hadron", called_peers)
            self.assertNotIn("zephyrus", called_peers)


if __name__ == "__main__":
    unittest.main()
