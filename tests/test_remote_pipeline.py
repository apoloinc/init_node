import json
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.remote_pipeline import RUN_ID, knowledge_cli_path, read_jsonl_ids, run, validate_results


class RemotePipelineValidationTests(unittest.TestCase):
    def test_run_id_is_limited_to_single_safe_component(self):
        for value in ("pilot-1", "2026.11.04_a"):
            self.assertRegex(value, RUN_ID)
        for value in ("../escape", "", ".", "a/b", "a b"):
            self.assertIsNone(RUN_ID.fullmatch(value))

    def test_results_must_match_ids_and_all_be_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.jsonl"
            good = [{"id": "a", "ok": True}, {"id": "b", "ok": True}]
            path.write_text("".join(json.dumps(x) + "\n" for x in good), encoding="utf-8")
            self.assertEqual(validate_results(path, ["a", "b"]), 2)
            with self.assertRaisesRegex(ValueError, "exactly match"):
                validate_results(path, ["a", "c"])
            path.write_text(json.dumps({"id": "a", "ok": False}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "failed results"):
                validate_results(path, ["a"])

    def test_input_jsonl_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.jsonl"
            path.write_text('{"id":"a"}\n{"id":"a"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_jsonl_ids(path)

    def test_knowledge_cli_is_sibling_of_interpreter_on_both_platforms(self):
        py = Path("C:/node/repos/knowledge/.venv/Scripts/python.exe")
        self.assertEqual(knowledge_cli_path(py, windows=True), py.parent / "apolo-knowledge.exe")
        self.assertEqual(knowledge_cli_path(Path("/node/.venv/bin/python"), windows=False), Path("/node/.venv/bin/apolo-knowledge"))

    def test_runs_symlink_is_rejected_before_any_subprocess_or_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "outside"
            target.mkdir()
            (root / "runs").symlink_to(target, target_is_directory=True)
            with patch("scripts.remote_pipeline.subprocess.run", side_effect=AssertionError("must reject before transport")), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run(["--root", str(root), "--feed", "/feed.jsonl", "--run-id", "guard"])
            self.assertEqual(list(target.iterdir()), [])

    def test_mocked_run_uses_workstation_modules_without_local_korax_and_real_rag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = root / "repos"
            knowledge = repos / "apolo-knowledge-data"
            rag = repos / "apolo-rag"
            bindir = "Scripts" if os.name == "nt" else "bin"
            interpreter = "python.exe" if os.name == "nt" else "python"
            cli = "apolo-knowledge.exe" if os.name == "nt" else "apolo-knowledge"
            for path in (knowledge / ".venv" / bindir / interpreter, knowledge / ".venv" / bindir / cli, rag / ".venv" / bindir / interpreter):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            feed = {"job_id": "j1", "title": "Engineer", "description_text": "Python required"}

            def fake_run(command, **kwargs):
                result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                stage = command[0]
                if stage == "scp" and command[-1].endswith("feed.jsonl"):
                    Path(command[-1]).write_text(json.dumps(feed) + "\n", encoding="utf-8")
                elif "apolo-knowledge" in stage and "ingest-vectorjobs" in command:
                    out = Path(command[command.index("--output") + 1])
                    (out).mkdir(parents=True)
                    (out / "jobs.parquet").touch()
                    result.stdout = json.dumps({"input": 1, "accepted": 1, "rejected": 0, "duplicates": 0})
                elif "apolo-knowledge" in stage and "export-korax" in command:
                    out = Path(command[command.index("--output") + 1])
                    out.mkdir(parents=True)
                    (out / "jobs.jsonl").write_text('{"id":"canonical-1","text":"Python required"}\n', encoding="utf-8")
                    (out / "mapping.parquet").touch()
                elif command[0] == "ssh" and "json.loads" in command[-1]:
                    self.assertEqual(kwargs['input'], json.dumps("~/apolo-node"))
                    result.stdout = "/home/fabian/apolo-node\n"
                elif command[0] == "scp" and command[-1].endswith("results.jsonl"):
                    Path(command[-1]).write_text('{"id":"canonical-1","ok":true,"attempts":1,"elapsed_s":1,"requirements":[]}\n', encoding="utf-8")
                elif "apolo-knowledge" in stage and "import-korax" in command:
                    out = Path(command[command.index("--output") + 1])
                    out.mkdir(parents=True)
                    (out / "requirement_mentions.parquet").touch()
                    result.stdout = json.dumps({"successful_result_rows": 1, "failed_result_rows": 0, "evidence_failed_result_rows": 0})
                elif "-c" in command and "model_dump_json" in " ".join(command):
                    env = kwargs["env"]
                    self.assertNotIn("APOLO_SETUP_MODE", env)
                    self.assertEqual(env["APOLO_HADRON_DIR"], str(root / "runs/test/enriched"))
                    result.stdout = json.dumps({"sample_size": 1, "job_count": 1})
                elif "-c" in command and "requirement_type" in " ".join(command):
                    result.stdout = "1\n"
                return result

            output = io.StringIO()
            with patch("scripts.remote_pipeline.subprocess.run", side_effect=fake_run), contextlib.redirect_stdout(output):
                code = run(["--root", str(root), "--collector", "deb", "--feed", "/feed/jobs.jsonl", "--run-id", "test"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "complete")
            self.assertFalse((repos / "korax").exists())


if __name__ == "__main__":
    unittest.main()
