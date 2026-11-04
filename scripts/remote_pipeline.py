"""Run one isolated collector -> Hadron -> local RAG processing cycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def read_jsonl_ids(path: Path) -> list[str]:
    ids: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {number}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError(f"line {number} has no valid id")
        ids.append(record["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("input contains duplicate ids")
    return ids


def validate_results(path: Path, expected: list[str]) -> int:
    actual = read_jsonl_ids(path)
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise ValueError("Korax result IDs do not exactly match exported input IDs")
    failed = []
    for line, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        result = json.loads(raw)
        if result.get("ok") is not True:
            failed.append(result.get("id", f"line {line}"))
    if failed:
        raise ValueError(f"Korax reported failed results: {failed[:10]}")
    return len(actual)


def knowledge_cli_path(knowledge_python: Path, *, windows: bool | None = None) -> Path:
    """Return the console script beside the interpreter on either OS."""
    if windows is None:
        windows = os.name == "nt"
    return knowledge_python.parent / ("apolo-knowledge.exe" if windows else "apolo-knowledge")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Installed workstation node root")
    parser.add_argument("--collector", default="deb", help="SSH alias reachable from this workstation")
    parser.add_argument("--feed", required=True, help="JSONL feed path on collector")
    parser.add_argument("--inference", default="hadron", help="SSH alias reachable from this workstation")
    parser.add_argument("--inference-root", default="~/apolo-node", help="Absolute remote node root or ~/path")
    parser.add_argument("--run-id", required=True, help="New isolated run name (letters, numbers, dot, dash, underscore)")
    parser.add_argument("--synthetic", action="store_true", help="Mark feed as synthetic in the run report")
    args = parser.parse_args(argv)
    if not RUN_ID.fullmatch(args.run_id) or args.run_id in {".", ".."}:
        parser.error("--run-id must be a simple safe name")
    for host in (args.collector, args.inference):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
            parser.error(f"unsafe SSH alias: {host}")
    if not (args.inference_root.startswith("~/") or PurePosixPath(args.inference_root).is_absolute()):
        parser.error("--inference-root must be absolute or start with ~/ ")
    root_parts = args.inference_root.removeprefix("~/").split("/") if args.inference_root.startswith("~/") else PurePosixPath(args.inference_root).parts[1:]
    if any(part in {"", ".", ".."} for part in root_parts) or not re.fullmatch(r"[A-Za-z0-9_./~-]+", args.inference_root):
        parser.error("--inference-root must use simple path components without shell metacharacters")

    root = args.root.resolve()
    repos = root / "repos"
    knowledge = repos / "apolo-knowledge-data"
    rag_python = repos / "apolo-rag" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    knowledge_python = knowledge / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for required in (knowledge_python, rag_python):
        if not required.exists():
            parser.error(f"required installed module missing: {required}")

    runs = root / "runs"
    if runs.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(runs)):
        parser.error(f"runs directory must not be a symlink or junction: {runs}")
    if runs.resolve() != runs.absolute() or not runs.resolve().is_relative_to(root):
        parser.error(f"runs directory escapes node root: {runs}")
    runs.mkdir(exist_ok=True)
    run_dir = runs / args.run_id
    if run_dir.exists():
        parser.error(f"run already exists; choose a new --run-id: {run_dir}")
    run_dir.mkdir()
    report_path = run_dir / "run.json"
    report = {"run_id": args.run_id, "status": "running", "synthetic": args.synthetic, "stages": {}}

    def save() -> None:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def command(stage: str, cmd: list[str], *, env: dict[str, str] | None = None, input_text: str | None = None) -> str:
        timeout = 900 if stage == "infer" else 30
        child_env = os.environ.copy() if env is None else env.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=child_env, input=input_text, timeout=timeout)
        report["stages"][stage] = {"returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}
        save()
        if result.returncode:
            raise RuntimeError(f"{stage} failed ({result.returncode})")
        return result.stdout

    try:
        raw = run_dir / "feed.jsonl"
        command("fetch_feed", ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"{args.collector}:{shlex.quote(args.feed)}", str(raw)])
        report["feed_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        records = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
        if not records or any(not isinstance(x, dict) or not x.get("job_id") or not x.get("title") or not x.get("description_text") for x in records):
            raise ValueError("feed must contain JSON objects with job_id, title and description_text")
        frame_script = "import json,pandas as pd,sys; from pathlib import Path; rows=[json.loads(x) for x in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()]; pd.DataFrame(rows).to_parquet(sys.argv[2],index=False)"
        command("convert_feed", [str(knowledge_python), "-c", frame_script, str(raw), str(run_dir / "feed.parquet")])
        knowledge_cli = str(knowledge_cli_path(knowledge_python))
        counts = json.loads(command("ingest", [knowledge_cli, "ingest-vectorjobs", "--input", str(run_dir / "feed.parquet"), "--output", str(run_dir / "snapshot")]))
        report["ingest_counts"] = counts
        if counts.get("accepted", 0) != counts.get("input") or counts.get("rejected", 0) or counts.get("duplicates", 0):
            raise ValueError("feed did not ingest completely; see ingest_counts")
        exchange = run_dir / "exchange"
        command("export", [knowledge_cli, "export-korax", "--jobs", str(run_dir / "snapshot/jobs.parquet"), "--output", str(exchange)])
        expected_ids = read_jsonl_ids(exchange / "jobs.jsonl")
        root_probe = "python3 -c 'import json,pathlib,sys; print(pathlib.Path(json.loads(sys.stdin.read())).expanduser().resolve())'"
        resolved_root = command("resolve_inference_root", ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", args.inference, root_probe], input_text=json.dumps(args.inference_root)).strip()
        if not resolved_root.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]+", resolved_root) or any(part in {".", ".."} for part in PurePosixPath(resolved_root).parts):
            raise ValueError("remote inference root did not resolve to a safe absolute path")
        remote_root = resolved_root.rstrip("/")
        remote_run = f"{remote_root}/runs/{args.run_id}"
        command("remote_prepare", ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", args.inference, f"mkdir -p {shlex.quote(remote_root + '/runs')} && mkdir {shlex.quote(remote_run)}"])
        command("send_jobs", ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", str(exchange / "jobs.jsonl"), f"{args.inference}:{remote_run}/jobs.jsonl"])
        remote_cmd = "python3 " + " ".join(map(shlex.quote, [f"{remote_root}/repos/korax/apolo.py", "--base-url", "http://127.0.0.1:8080/v1", "--model", "apolo-extractor", "--no-pet", "batch", "--input", f"{remote_run}/jobs.jsonl", "--output", f"{remote_run}/results.jsonl"]))
        command("infer", ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", args.inference, remote_cmd])
        received = exchange / "results.jsonl"
        command("fetch_results", ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"{args.inference}:{remote_run}/results.jsonl", str(received)])
        result_count = validate_results(received, expected_ids)
        report["result_rows"] = result_count
        enriched = run_dir / "enriched"
        import_cmd = [knowledge_cli, "import-korax", "--jobs", str(run_dir / "snapshot/jobs.parquet"), "--mapping", str(exchange / "mapping.parquet"), "--results", str(received), "--output", str(enriched)]
        report["import"] = json.loads(command("import", import_cmd))
        imported = report["import"]
        if imported.get("successful_result_rows") != result_count or imported.get("failed_result_rows") != 0 or imported.get("evidence_failed_result_rows") != 0:
            raise ValueError("Korax import contains failed or evidence-failed rows")
        env = os.environ.copy()
        env.pop("APOLO_SETUP_MODE", None)
        env.update({"APOLO_KNOWLEDGE_DATA_ROOT": str(knowledge), "APOLO_SKILL_STANDARD_DIR": str(root / "artifacts/skill-standard"), "APOLO_HADRON_DIR": str(enriched)})
        rag_code = "import json; from apolo_rag.tools.labor_market import get_labor_market; print(get_labor_market(backend='real').model_dump_json())"
        rag_result = json.loads(command("rag_check", [str(rag_python), "-c", rag_code], env=env))
        sample_size = rag_result.get("sample_size")
        job_count = rag_result.get("job_count")
        expected_sample = int(command("rag_expected_sample", [str(knowledge_python), "-c", "import pandas as p,sys; f=p.read_parquet(sys.argv[1]); print(f[f.requirement_type.isin(['skill','tool','technical_knowledge','methodology','language'])].job_id.nunique())", str(enriched / "requirement_mentions.parquet")]).strip())
        if not isinstance(sample_size, int) or sample_size != expected_sample:
            raise ValueError("RAG returned an invalid sample_size for this run")
        if job_count is not None and (not isinstance(job_count, int) or not 0 <= job_count <= sample_size):
            raise ValueError("RAG returned an invalid job_count for this run")
        report["rag"] = rag_result
        report["status"] = "complete"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        save()
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1
    save()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
