from pathlib import Path
import subprocess
import tempfile
import unittest
import os
from unittest.mock import patch

import node


class InstallBehaviorTests(unittest.TestCase):
    def test_locked_knowledge_dependencies_are_checked_in(self):
        manifest = node.load_manifest()
        spec = manifest["modules"]["apolo-knowledge-data"]
        lock = Path(__file__).resolve().parents[1] / spec["lock_file"]
        self.assertEqual(spec["runtime"], "knowledge-lock")
        self.assertTrue(lock.is_file())
        self.assertIn("pandas==", lock.read_text(encoding="utf-8"))

    def test_collector_and_workstation_profiles_are_pinned(self):
        manifest = node.load_manifest()
        modules = manifest["modules"]
        self.assertEqual(manifest["profiles"]["collector"], ["apolo-scraper"])
        self.assertEqual(modules["apolo-scraper"]["ref"], "73b8e4c9fb0b836746ae989595eda21cda0ee833")
        self.assertEqual(modules["apolo-scraper"]["import_name"], "populares_scraper")
        self.assertNotIn("korax", manifest["profiles"]["workstation"])
        args = node.build_parser().parse_args(["init", "--profile", "collector", "--root", "x"])
        self.assertEqual(args.profile, "collector")

    def test_stdlib_does_not_try_uv_even_with_an_accidental_lockfile(self):
        spec = {"runtime": "stdlib", "dir_name": "korax"}
        with patch("shutil.which", return_value="uv"), patch(
            "subprocess.run", side_effect=AssertionError("must not invoke uv")
        ):
            result = node.install_module_dependencies("korax", spec, Path("/tmp/repos"))
        self.assertEqual(result["status"], "inferred_stdlib")

    def test_broken_repository_manifest_fails_closed(self):
        with patch.object(node, "__file__", "/nonexistent/node.py"):
            with self.assertRaises(FileNotFoundError):
                node.load_manifest()

    def test_clone_from_bundle_sets_canonical_origin_and_pinned_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            for args in (["init"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.com"]):
                subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
            (source / "file.txt").write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            bundles = root / "bundles"
            bundles.mkdir()
            subprocess.run(["git", "bundle", "create", str(bundles / "fixture.bundle"), "HEAD"], cwd=source, check=True, capture_output=True)

            result = node.check_and_sync_module(
                "fixture",
                {"url": "https://github.com/example/fixture.git", "ref": sha, "dir_name": "fixture"},
                root / "repos",
                bundles,
            )
            repo = root / "repos" / "fixture"
            self.assertEqual(result["commit"], sha)
            self.assertEqual(node.get_git_origin(repo), "https://github.com/example/fixture.git")

    def test_skill_build_rejects_linked_artifact_directory_before_running_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = root / "repos"
            repos.mkdir()
            artifacts = root / "artifacts"
            artifacts.mkdir()
            output = artifacts / "skill-standard"
            with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == output), patch(
                "shutil.which", return_value="uv"
            ), patch("subprocess.run", side_effect=AssertionError("must reject before subprocess")):
                with self.assertRaisesRegex(ValueError, "linked skill-standard output"):
                    node.install_module_dependencies("apolo-skill-standard", {"runtime": "build-standard", "dir_name": "standard"}, repos)

    def test_uv_project_is_forced_into_module_venv(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "repos" / "rag"
            target.mkdir(parents=True)
            result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("shutil.which", return_value="uv"), patch("subprocess.run", return_value=result) as run:
                node.install_module_dependencies("rag", {"runtime": "uv-project", "dir_name": "rag"}, target.parent)
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(target / ".venv"))
            self.assertNotIn("UV_PROJECT", env)
            self.assertNotIn("UV_WORKING_DIRECTORY", env)


if __name__ == "__main__":
    unittest.main()
