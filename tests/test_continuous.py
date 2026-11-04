from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.simulate_continuous import (
    SIM_ID_RE,
    SSH_ALIAS_RE,
    advance_active_pointer,
    assert_safe_path,
    compute_lifecycle_delta,
    create_publication_manifest,
    default_processor,
    get_scenario_cycles,
    get_scenario_jobs,
    main,
    probe_rag_consumer,
    read_active_publication,
    run_simulation,
    stage_feed_on_collector,
)


class ContinuousSimulationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.root.joinpath("state.json").write_text(json.dumps({"modules": {
            "apolo-knowledge-data": {"commit": "1" * 40},
            "apolo-rag": {"commit": "2" * 40},
            "apolo-skill-standard": {"commit": "3" * 40},
        }}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_mock_run(self, run_id: str, job_count: int = 3, status: str = "complete") -> Path:
        """Create real temporary run directory and artifacts mimicking remote_pipeline output."""
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        enriched = run_dir / "enriched"
        enriched.mkdir(parents=True, exist_ok=True)

        req_file = enriched / "requirement_mentions.parquet"
        req_file.write_bytes(b"PARQUET_MOCK_REQUIREMENT_MENTIONS")
        job_ext_file = enriched / "job_extractions.parquet"
        job_ext_file.write_bytes(b"PARQUET_MOCK_JOB_EXTRACTIONS")
        (enriched / "failed_extractions.parquet").write_bytes(b"PARQUET_MOCK_FAILED")
        (enriched / "manifest.json").write_text("{}\n", encoding="utf-8")

        run_json = run_dir / "run.json"
        report = {
            "run_id": run_id,
            "status": status,
            "synthetic": True,
            "feed_sha256": "dummyfeedhash",
            "ingest_counts": {"input": job_count, "accepted": job_count, "rejected": 0, "duplicates": 0},
            "result_rows": job_count,
            "import": {
                "successful_result_rows": job_count,
                "failed_result_rows": 0,
                "evidence_failed_result_rows": 0,
                "requirement_mentions": job_count * 2,
            },
            "rag": {
                "sample_size": job_count,
                "job_count": job_count,
            },
        }
        run_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return run_dir

    def _set_run_feed_hash(self, run_id: str, jobs: dict[str, dict[str, Any]]) -> None:
        content = "".join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs.values())
        path = self.root / "runs" / run_id / "run.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["feed_sha256"] = __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def _mock_transport(self, cmd: list[str], input_str: str | None = None):
        stdout = "/home/fabian/apolo-collector/simulations/sim/cycle/feed.jsonl\n" if "python3 -c" in cmd[-1] else ""
        return type("Proc", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    def test_simulation_id_and_ssh_alias_safety_regex(self):
        for valid_sim in ("sim-01", "sim_pilot.2026", "a-b-c"):
            self.assertRegex(valid_sim, SIM_ID_RE)
        for invalid_sim in ("../escape", "", ".", "a/b", "a b", "sim$id"):
            self.assertIsNone(SIM_ID_RE.fullmatch(invalid_sim))

        for valid_ssh in ("deb", "hadron", "node-1.lan"):
            self.assertRegex(valid_ssh, SSH_ALIAS_RE)
        for invalid_ssh in ("deb;rm", "host name", "bad/alias"):
            self.assertIsNone(SSH_ALIAS_RE.fullmatch(invalid_ssh))

    def test_lifecycle_delta_calculation(self):
        catalog = get_scenario_jobs()
        # Initial snapshot
        snap1 = {
            "sim-job-101": catalog["sim-job-101"],
            "sim-job-102": catalog["sim-job-102-v1"],
            "sim-job-103": catalog["sim-job-103"],
        }
        delta1 = compute_lifecycle_delta(None, snap1)
        self.assertEqual(delta1["added"], ["sim-job-101", "sim-job-102", "sim-job-103"])
        self.assertEqual(delta1["modified"], [])
        self.assertEqual(delta1["removed"], [])
        self.assertEqual(delta1["unchanged"], [])
        self.assertFalse(delta1["is_repeat"])

        # Second snapshot with delta (1 added, 1 modified, 1 removed, 1 unchanged)
        snap2 = {
            "sim-job-101": catalog["sim-job-101"],
            "sim-job-102": catalog["sim-job-102-v2"],  # modified text
            "sim-job-104": catalog["sim-job-104"],     # new job
        }
        delta2 = compute_lifecycle_delta(snap1, snap2)
        self.assertEqual(delta2["added"], ["sim-job-104"])
        self.assertEqual(delta2["modified"], ["sim-job-102"])
        self.assertEqual(delta2["removed"], ["sim-job-103"])
        self.assertEqual(delta2["unchanged"], ["sim-job-101"])
        self.assertFalse(delta2["is_repeat"])

        # Exact repeat
        delta_repeat = compute_lifecycle_delta(snap2, snap2)
        self.assertEqual(delta_repeat["added"], [])
        self.assertEqual(delta_repeat["modified"], [])
        self.assertEqual(delta_repeat["removed"], [])
        self.assertEqual(delta_repeat["unchanged"], ["sim-job-101", "sim-job-102", "sim-job-104"])
        self.assertTrue(delta_repeat["is_repeat"])

    def test_symlink_and_junction_containment_rejection(self):
        outside = Path(tempfile.gettempdir()) / "outside_apolo_sim"
        outside.mkdir(exist_ok=True)
        try:
            symlink_dir = self.root / "escaped_link"
            symlink_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "must not be a symlink or junction"):
                assert_safe_path(symlink_dir, self.root)
        finally:
            if outside.exists():
                outside.rmdir()

    def test_atomic_pointer_advancement_and_reading(self):
        sim_id = "test-sim-01"
        run_id = f"{sim_id}-c1"
        self._setup_mock_run(run_id, job_count=3)

        manifest_path, manifest_sha, manifest_data = create_publication_manifest(
            self.root,
            sim_id,
            run_id,
            1,
            "/remote/feed.jsonl",
            "dummyfeedhash",
            {"added": ["sim-job-101"], "modified": [], "removed": [], "unchanged": [], "total_active": 1, "is_repeat": False},
            "deb",
            "hadron",
        )
        self.assertTrue(manifest_path.exists())

        # Immutability: creating the same manifest again must fail
        with self.assertRaises(FileExistsError):
            create_publication_manifest(
                self.root,
                sim_id,
                run_id,
                1,
                "/remote/feed.jsonl",
                "dummyfeedhash",
                {"added": ["sim-job-101"], "modified": [], "removed": [], "unchanged": [], "total_active": 1, "is_repeat": False},
                "deb",
                "hadron",
            )

        # Advance pointer atomically
        rel_manifest = str(manifest_path.relative_to(self.root)).replace("\\", "/")
        rel_enriched = f"runs/{run_id}/enriched"
        advance_active_pointer(
            self.root,
            sim_id,
            rel_manifest,
            manifest_sha,
            run_id,
            rel_enriched,
            "dummyfeedhash",
        )

        active = read_active_publication(self.root, sim_id)
        self.assertEqual(active["pointer"]["run_id"], run_id)
        self.assertEqual(active["pointer"]["manifest_sha256"], manifest_sha)
        self.assertEqual(active["manifest"]["publication_id"], f"pub-{run_id}")

    def test_hash_tamper_detection(self):
        sim_id = "tamper-sim"
        run_id = f"{sim_id}-c1"
        self._setup_mock_run(run_id, job_count=3)

        manifest_path, manifest_sha, _ = create_publication_manifest(
            self.root,
            sim_id,
            run_id,
            1,
            "/remote/feed.jsonl",
            "dummyfeedhash",
            {"added": ["sim-job-101"], "modified": [], "removed": [], "unchanged": [], "total_active": 1, "is_repeat": False},
            "deb",
            "hadron",
        )
        rel_manifest = str(manifest_path.relative_to(self.root)).replace("\\", "/")
        rel_enriched = f"runs/{run_id}/enriched"
        advance_active_pointer(
            self.root,
            sim_id,
            rel_manifest,
            manifest_sha,
            run_id,
            rel_enriched,
            "dummyfeedhash",
        )

        # Verify active publication is valid initially
        self.assertIsNotNone(read_active_publication(self.root, sim_id))

        # Tamper with manifest file content
        content = json.loads(manifest_path.read_text(encoding="utf-8"))
        content["roles"]["collector"] = "tampered_host"
        manifest_path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
            read_active_publication(self.root, sim_id)

    def test_active_artifact_hash_tamper_detection(self):
        sim_id = "artifact-tamper"
        run_id = f"{sim_id}-c1"
        self._setup_mock_run(run_id)
        manifest_path, manifest_sha, _ = create_publication_manifest(
            self.root, sim_id, run_id, 1, "/feed.jsonl", "dummyfeedhash",
            {"added": ["job"], "modified": [], "removed": [], "unchanged": [], "total_active": 3, "is_repeat": False},
            "deb", "hadron",
        )
        advance_active_pointer(self.root, sim_id, str(manifest_path.relative_to(self.root)), manifest_sha,
                               run_id, f"runs/{run_id}/enriched", "dummyfeedhash")
        (self.root / "runs" / run_id / "enriched" / "job_extractions.parquet").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "artifact hash/size mismatch"):
            read_active_publication(self.root, sim_id)

    def test_feed_hash_mismatch_cannot_publish_or_change_pointer(self):
        sim_id = "feed-hash-guard"
        run_id = f"{sim_id}-c1"
        self._setup_mock_run(run_id)
        manifest_path, manifest_sha, _ = create_publication_manifest(
            self.root, sim_id, run_id, 1, "/feed.jsonl", "dummyfeedhash",
            {"added": ["job"], "modified": [], "removed": [], "unchanged": [], "total_active": 3, "is_repeat": False},
            "deb", "hadron",
        )
        advance_active_pointer(self.root, sim_id, str(manifest_path.relative_to(self.root)), manifest_sha,
                               run_id, f"runs/{run_id}/enriched", "dummyfeedhash")
        pointer_path = self.root / "simulations" / sim_id / "current.json"
        previous_pointer = pointer_path.read_bytes()

        next_run = f"{sim_id}-c2"
        self._setup_mock_run(next_run)
        with self.assertRaisesRegex(ValueError, "Run feed hash does not match"):
            create_publication_manifest(
                self.root, sim_id, next_run, 2, "/feed2.jsonl", "actual-feed-hash",
                {"added": ["job2"], "modified": [], "removed": [], "unchanged": [], "total_active": 3, "is_repeat": False},
                "deb", "hadron",
            )
        self.assertFalse((self.root / "simulations" / sim_id / "publications" / f"pub-{next_run}.json").exists())
        self.assertEqual(pointer_path.read_bytes(), previous_pointer)

    def test_missing_enriched_artifact_detection(self):
        sim_id = "missing-art-sim"
        run_id = f"{sim_id}-c1"
        self._setup_mock_run(run_id, job_count=3)

        manifest_path, manifest_sha, _ = create_publication_manifest(
            self.root,
            sim_id,
            run_id,
            1,
            "/remote/feed.jsonl",
            "dummyfeedhash",
            {"added": ["sim-job-101"], "modified": [], "removed": [], "unchanged": [], "total_active": 1, "is_repeat": False},
            "deb",
            "hadron",
        )
        rel_manifest = str(manifest_path.relative_to(self.root)).replace("\\", "/")
        rel_enriched = f"runs/{run_id}/enriched"
        advance_active_pointer(
            self.root,
            sim_id,
            rel_manifest,
            manifest_sha,
            run_id,
            rel_enriched,
            "dummyfeedhash",
        )

        # Delete requirement_mentions.parquet
        req_file = self.root / rel_enriched / "requirement_mentions.parquet"
        req_file.unlink()

        with self.assertRaises((ValueError, FileNotFoundError)):
            read_active_publication(self.root, sim_id)

    def test_repeat_skip_does_not_call_processor(self):
        sim_id = "repeat-skip-sim"
        catalog = get_scenario_jobs()
        cycle1 = {
            "cycle_index": 1,
            "name": "snap1",
            "jobs": {"sim-job-101": catalog["sim-job-101"]},
            "expect_failure": False,
        }
        cycle2_repeat = {
            "cycle_index": 2,
            "name": "snap1_repeat",
            "jobs": {"sim-job-101": catalog["sim-job-101"]},
            "expect_failure": False,
        }

        processor_calls = []

        def mock_processor(args: list[str]) -> int:
            run_id = args[args.index("--run-id") + 1]
            processor_calls.append(run_id)
            self._setup_mock_run(run_id, job_count=1)
            self._set_run_feed_hash(run_id, cycle1["jobs"])
            return 0

        transport_calls = []

        def mock_transport(cmd: list[str], input_str: str | None = None):
            transport_calls.append((cmd, input_str))
            return self._mock_transport(cmd, input_str)

        probe_calls = []

        def mock_probe(py: Path, env: dict[str, str]) -> dict[str, Any]:
            probe_calls.append(env)
            return {"sample_size": 1, "job_count": 1}

        report = run_simulation(
            self.root,
            sim_id,
            processor=mock_processor,
            transport_hook=mock_transport,
            probe_runner=mock_probe,
            cycles_to_run=[cycle1, cycle2_repeat],
            interval_seconds=0.0,
        )

        # Processor should only have been called once (for cycle 1)
        self.assertEqual(processor_calls, [f"{sim_id}-c1"])
        self.assertEqual(len(report["cycles"]), 2)
        self.assertEqual(report["cycles"][0]["status"], "complete")
        self.assertEqual(report["cycles"][1]["status"], "skipped_repeat")
        self.assertEqual(report["total_gpu_jobs"], 1)

        # Active publication must point to cycle 1
        active = read_active_publication(self.root, sim_id)
        self.assertEqual(active["pointer"]["run_id"], f"{sim_id}-c1")

    def test_failed_cycle_preserves_pointer_and_recovery_advances_it(self):
        sim_id = "failure-isolation-sim"
        catalog = get_scenario_jobs()
        cycle1 = {
            "cycle_index": 1,
            "name": "initial",
            "jobs": {"sim-job-101": catalog["sim-job-101"]},
            "expect_failure": False,
        }
        cycle2_fail = {
            "cycle_index": 2,
            "name": "bad_feed",
            "jobs": {"corrupt": {"corrupt": True}},
            "expect_failure": True,
        }
        cycle3_recover = {
            "cycle_index": 3,
            "name": "recovery",
            "jobs": {
                "sim-job-101": catalog["sim-job-101"],
                "sim-job-104": catalog["sim-job-104"],
            },
            "expect_failure": False,
        }

        def mock_processor(args: list[str]) -> int:
            run_id = args[args.index("--run-id") + 1]
            if "c2" in run_id:
                # Controlled failure
                return 1
            jobs = cycle1["jobs"] if "c1" in run_id else cycle3_recover["jobs"]
            self._setup_mock_run(run_id, job_count=len(jobs))
            self._set_run_feed_hash(run_id, jobs)
            return 0

        def mock_transport(cmd: list[str], input_str: str | None = None):
            return self._mock_transport(cmd, input_str)

        def mock_probe(py: Path, env: dict[str, str]) -> dict[str, Any]:
            count = 1 if Path(env["APOLO_HADRON_DIR"]).parent.name.endswith("c1") else 2
            return {"sample_size": count, "job_count": count}

        report = run_simulation(
            self.root,
            sim_id,
            processor=mock_processor,
            transport_hook=mock_transport,
            probe_runner=mock_probe,
            cycles_to_run=[cycle1, cycle2_fail, cycle3_recover],
            interval_seconds=0.0,
        )

        self.assertEqual(len(report["cycles"]), 3)
        self.assertEqual(report["cycles"][0]["status"], "complete")
        self.assertEqual(report["cycles"][1]["status"], "expected_failure")
        self.assertTrue(report["cycles"][1]["pointer_preserved"])
        self.assertEqual(report["cycles"][2]["status"], "complete")

        # After recovery, pointer is at cycle 3
        active = read_active_publication(self.root, sim_id)
        self.assertEqual(active["pointer"]["run_id"], f"{sim_id}-c3")

    def test_full_scenario_runs_bounded_under_12_gpu_jobs(self):
        sim_id = "bounded-scenario-sim"
        processor_calls = []

        def mock_processor(args: list[str]) -> int:
            run_id = args[args.index("--run-id") + 1]
            processor_calls.append(run_id)
            if "c4" in run_id:
                # Controlled failure cycle
                return 1
            jobs_by_cycle = {1: get_scenario_cycles()[0]["jobs"], 2: get_scenario_cycles()[1]["jobs"], 5: get_scenario_cycles()[4]["jobs"]}
            cycle_number = int(run_id.rsplit("c", 1)[1])
            jobs = jobs_by_cycle[cycle_number]
            self._setup_mock_run(run_id, job_count=len(jobs))
            self._set_run_feed_hash(run_id, jobs)
            return 0

        def mock_transport(cmd: list[str], input_str: str | None = None):
            return self._mock_transport(cmd, input_str)

        def mock_probe(py: Path, env: dict[str, str]) -> dict[str, Any]:
            count = 4 if Path(env["APOLO_HADRON_DIR"]).parent.name.endswith("c5") else 3
            return {"sample_size": count, "job_count": count}

        report = run_simulation(
            self.root,
            sim_id,
            processor=mock_processor,
            transport_hook=mock_transport,
            probe_runner=mock_probe,
            interval_seconds=0.0,
        )

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["total_cycles"], 5)
        # Cycle 1: 3 jobs
        # Cycle 2: 3 jobs
        # Cycle 3: 0 jobs (repeat skipped)
        # Cycle 4: 0 jobs (failed)
        # Cycle 5: 4 jobs (recovered)
        # Total = 10 <= 12
        self.assertLessEqual(report["total_gpu_jobs"], 12)
        self.assertEqual(report["total_gpu_jobs"], 10)

        # Verify statuses
        statuses = [c["status"] for c in report["cycles"]]
        self.assertEqual(statuses, ["complete", "complete", "skipped_repeat", "expected_failure", "complete"])

        # Check processor calls: c1, c2, c4 (fails), c5
        # c3 was skipped
        self.assertEqual(processor_calls, [f"{sim_id}-c1", f"{sim_id}-c2", f"{sim_id}-c4", f"{sim_id}-c5"])

        # Check active pointer is at recovery cycle c5
        active = read_active_publication(self.root, sim_id)
        self.assertEqual(active["pointer"]["run_id"], f"{sim_id}-c5")

    def test_check_only_cli_mode(self):
        sim_id = "cli-check-sim"
        run_id = f"{sim_id}-c1"
        self._setup_mock_run(run_id, job_count=3)

        manifest_path, manifest_sha, _ = create_publication_manifest(
            self.root,
            sim_id,
            run_id,
            1,
            "/remote/feed.jsonl",
            "dummyfeedhash",
            {"added": ["sim-job-101"], "modified": [], "removed": [], "unchanged": [], "total_active": 1, "is_repeat": False},
            "deb",
            "hadron",
        )
        rel_manifest = str(manifest_path.relative_to(self.root)).replace("\\", "/")
        rel_enriched = f"runs/{run_id}/enriched"
        advance_active_pointer(
            self.root,
            sim_id,
            rel_manifest,
            manifest_sha,
            run_id,
            rel_enriched,
            "dummyfeedhash",
        )

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            code = main(["--root", str(self.root), "--simulation-id", sim_id, "--check-only"])
        self.assertEqual(code, 0)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(output["pointer"]["run_id"], run_id)

        # Test non-existent simulation
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            bad_code = main(["--root", str(self.root), "--simulation-id", "non-existent", "--check-only"])
        self.assertEqual(bad_code, 1)

    def test_probe_failure_leaves_previous_pointer_byte_identical(self):
        sim_id = "probe-isolation"
        catalog = get_scenario_jobs()
        cycles = [
            {"cycle_index": 1, "name": "first", "jobs": {"sim-job-101": catalog["sim-job-101"]}},
            {"cycle_index": 2, "name": "second", "jobs": {"sim-job-104": catalog["sim-job-104"]}},
        ]

        def processor(args: list[str]) -> int:
            run_id = args[args.index("--run-id") + 1]
            idx = int(run_id.rsplit("c", 1)[1]) - 1
            self._setup_mock_run(run_id, job_count=1)
            self._set_run_feed_hash(run_id, cycles[idx]["jobs"])
            return 0

        def probe(_py: Path, env: dict[str, str]) -> dict[str, Any]:
            if Path(env["APOLO_HADRON_DIR"]).parent.name.endswith("c2"):
                raise RuntimeError("candidate RAG unavailable")
            return {"sample_size": 1, "job_count": 1}

        with self.assertRaisesRegex(RuntimeError, "candidate RAG unavailable"):
            run_simulation(self.root, sim_id, processor=processor, transport_hook=self._mock_transport,
                           probe_runner=probe, cycles_to_run=cycles, interval_seconds=0)
        pointer = self.root / "simulations" / sim_id / "current.json"
        before = pointer.read_bytes()
        self.assertEqual(json.loads(before)["run_id"], f"{sim_id}-c1")
        self.assertEqual(pointer.read_bytes(), before)

    def test_simulation_root_symlink_rejected_before_writes(self):
        outside = Path(tempfile.mkdtemp())
        try:
            (self.root / "simulations").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink or junction"):
                run_simulation(self.root, "link-guard", processor=lambda _args: 1, interval_seconds=0)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            outside.rmdir()

    def test_reused_simulation_id_refuses_overwrite(self):
        sim_dir = self.root / "simulations" / "already-used"
        sim_dir.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            run_simulation(self.root, "already-used", processor=lambda _args: 1, interval_seconds=0)

    def test_direct_entry_default_processor_imports_runner(self):
        # Import path is added from __file__, so invocation works outside repository cwd.
        current_dir = os.getcwd()
        try:
            os.chdir(self.root)
            self.assertEqual(default_processor(["--help"]), 0)
        finally:
            os.chdir(current_dir)

    def test_collector_tilde_is_resolved_before_quoting_remote_path(self):
        calls = []

        def transport(cmd: list[str], input_str: str | None = None):
            calls.append((cmd, input_str))
            stdout = "/home/fabian/apolo-collector/run/feed.jsonl\n" if "python3 -c" in cmd[-1] else ""
            return type("Proc", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        resolved = stage_feed_on_collector("deb", "~/apolo-collector/run/feed.jsonl", "{}\n", transport_hook=transport)
        self.assertEqual(resolved, "/home/fabian/apolo-collector/run/feed.jsonl")
        self.assertEqual(calls[0][1], '"~/apolo-collector/run/feed.jsonl"')
        self.assertTrue(any("/home/fabian/apolo-collector/run/feed.jsonl" in str(cmd) for cmd, _ in calls[1:]))

    def test_real_collector_transport_sends_exact_utf8_bytes_without_text_mode(self):
        payload = '{"text":"línea uno"}\n{"text":"línea dos"}\n'
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append((cmd, kwargs))
            output = b"/home/fabian/feed.jsonl\n" if "python3 -c" in cmd[-1] else b""
            return subprocess.CompletedProcess(cmd, 0, output, b"")

        with patch("scripts.simulate_continuous.subprocess.run", side_effect=fake_run):
            resolved = stage_feed_on_collector("deb", "~/feed.jsonl", payload)
        self.assertEqual(resolved, "/home/fabian/feed.jsonl")
        write_call = next(kwargs for cmd, kwargs in captured if cmd[-1].startswith("cat >"))
        self.assertEqual(write_call["input"], payload.encode("utf-8"))
        self.assertIs(write_call["text"], False)


if __name__ == "__main__":
    unittest.main()
