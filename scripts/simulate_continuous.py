"""Bounded realistic continuous acquisition and publication simulation for Apolo nodes.

Orchestrates sequential cycles of feed staging, remote inference via remote_pipeline,
immutable publication manifest creation, atomic pointer advancement, skip-on-repeat,
lifecycle delta tracking, and failure isolation / recovery verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

SIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,57}$")
SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./~-]+$")


def assert_safe_path(path: Path, root: Path, *, must_exist: bool = False) -> Path:
    """Ensure path is within root, has no symlinks or junctions, and does not escape."""
    resolved_root = root.resolve()

    # Traverse existing segments to verify none is a symlink or NTFS junction
    curr = path
    while True:
        if curr.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(curr)):
            raise ValueError(f"Path component must not be a symlink or junction: {curr}")
        if curr == curr.parent or curr == resolved_root:
            break
        curr = curr.parent

    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"Path escapes root directory ({resolved_root}): {path}")

    if must_exist and not path.exists():
        raise FileNotFoundError(f"Required path does not exist: {path}")

    return path


def compute_lifecycle_delta(
    previous_jobs: dict[str, dict[str, Any]] | None,
    current_jobs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compute added, modified, removed, and unchanged job IDs between snapshots."""
    if previous_jobs is None:
        return {
            "added": sorted(current_jobs.keys()),
            "modified": [],
            "removed": [],
            "unchanged": [],
            "total_active": len(current_jobs),
            "is_repeat": False,
        }

    prev_keys = set(previous_jobs.keys())
    curr_keys = set(current_jobs.keys())

    added = sorted(curr_keys - prev_keys)
    removed = sorted(prev_keys - curr_keys)
    modified: list[str] = []
    unchanged: list[str] = []

    for key in sorted(curr_keys & prev_keys):
        if previous_jobs[key] != current_jobs[key]:
            modified.append(key)
        else:
            unchanged.append(key)

    is_repeat = (not added and not modified and not removed)
    return {
        "added": added,
        "modified": modified,
        "removed": removed,
        "unchanged": unchanged,
        "total_active": len(current_jobs),
        "is_repeat": is_repeat,
    }


def get_scenario_jobs() -> dict[str, dict[str, Any]]:
    """Return catalog of distinct Spanish jobs with Python/SQL requirements and synthetic URLs."""
    return {
        "sim-job-101": {
            "job_id": "sim-job-101",
            "title": "Ingeniero de Datos Python y SQL",
            "company_name": "Tecnologias Cono Sur SpA",
            "source_url": "https://example.invalid/empleos/sim-job-101",
            "published_at": "2026-11-04T08:00:00Z",
            "description_text": "Buscamos Ingeniero de Datos con experiencia comprobable en Python y diseno de consultas complejas en SQL para procesamiento analitico distribuido.",
        },
        "sim-job-102-v1": {
            "job_id": "sim-job-102",
            "title": "Desarrollador Backend Senior",
            "company_name": "Fintech Cordillera",
            "source_url": "https://example.invalid/empleos/sim-job-102",
            "published_at": "2026-11-04T08:15:00Z",
            "description_text": "Se requiere desarrollador backend con dominio solido de Python y bases de datos relacionales con SQL para microservicios financieros.",
        },
        "sim-job-102-v2": {
            "job_id": "sim-job-102",
            "title": "Desarrollador Backend y Datos Senior",
            "company_name": "Fintech Cordillera",
            "source_url": "https://example.invalid/empleos/sim-job-102",
            "published_at": "2026-11-04T09:15:00Z",
            "description_text": "Se requiere desarrollador senior experto en arquitectura backend con Python y modelado analitico en SQL para APIs transaccionales de alto rendimiento.",
        },
        "sim-job-103": {
            "job_id": "sim-job-103",
            "title": "Analista de Inteligencia de Negocios",
            "company_name": "Logistica Austral Limitada",
            "source_url": "https://example.invalid/empleos/sim-job-103",
            "published_at": "2026-11-04T08:30:00Z",
            "description_text": "Empresa de logistica busca analista de BI con conocimientos en modelado SQL y automatizacion de pipelines con scripts en Python.",
        },
        "sim-job-104": {
            "job_id": "sim-job-104",
            "title": "Especialista en Automatizacion de Datos",
            "company_name": "Servicios Integrados Valparaiso",
            "source_url": "https://example.invalid/empleos/sim-job-104",
            "published_at": "2026-11-04T09:00:00Z",
            "description_text": "Buscamos especialista para construir flujos de ingesta utilizando Python y optimizacion de bases de datos con SQL relacional.",
        },
        "sim-job-105": {
            "job_id": "sim-job-105",
            "title": "Ingeniero de Pipelines de Datos",
            "company_name": "Innovacion Andina S.A.",
            "source_url": "https://example.invalid/empleos/sim-job-105",
            "published_at": "2026-11-04T10:00:00Z",
            "description_text": "Se busca ingeniero para extraccion y transformacion de datos aplicando scripts en Python y gestion de almacenes de datos SQL.",
        },
    }


def get_scenario_cycles() -> list[dict[str, Any]]:
    """Return bounded realistic 5-cycle scenario."""
    catalog = get_scenario_jobs()
    return [
        {
            "cycle_index": 1,
            "name": "initial_snapshot",
            "description": "Initial 3 jobs snapshot",
            "jobs": {
                "sim-job-101": catalog["sim-job-101"],
                "sim-job-102": catalog["sim-job-102-v1"],
                "sim-job-103": catalog["sim-job-103"],
            },
            "expect_failure": False,
        },
        {
            "cycle_index": 2,
            "name": "delta_snapshot",
            "description": "Second snapshot with 1 new (104), 1 modified (102-v2), 1 removed (103), 1 unchanged (101)",
            "jobs": {
                "sim-job-101": catalog["sim-job-101"],
                "sim-job-102": catalog["sim-job-102-v2"],
                "sim-job-104": catalog["sim-job-104"],
            },
            "expect_failure": False,
        },
        {
            "cycle_index": 3,
            "name": "exact_repeat",
            "description": "Exact repeat of cycle 2 snapshot to verify compute and publication are skipped",
            "jobs": {
                "sim-job-101": catalog["sim-job-101"],
                "sim-job-102": catalog["sim-job-102-v2"],
                "sim-job-104": catalog["sim-job-104"],
            },
            "expect_failure": False,
        },
        {
            "cycle_index": 4,
            "name": "controlled_failure",
            "description": "Controlled failure cycle with invalid feed schema to verify previous pointer is preserved",
            "jobs": {
                "sim-job-invalid": {
                    "corrupt_field": "no_title_or_desc",
                    "notes": "intentionally invalid feed to test failure isolation",
                }
            },
            "expect_failure": True,
        },
        {
            "cycle_index": 5,
            "name": "recovery",
            "description": "Recovery cycle with valid snapshot adding job 105, advancing pointer to recovered state",
            "jobs": {
                "sim-job-101": catalog["sim-job-101"],
                "sim-job-102": catalog["sim-job-102-v2"],
                "sim-job-104": catalog["sim-job-104"],
                "sim-job-105": catalog["sim-job-105"],
            },
            "expect_failure": False,
        },
    ]


def stage_feed_on_collector(
    collector: str,
    remote_feed_path: str,
    feed_content: str,
    *,
    transport_hook: Callable[[list[str], Optional[str]], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Stage feed on collector node via atomic write (temp write + mv)."""
    if not SAFE_PATH_RE.fullmatch(remote_feed_path) or not (remote_feed_path.startswith("~/") or PurePosixPath(remote_feed_path).is_absolute()):
        raise ValueError(f"Unsafe remote feed path: {remote_feed_path}")

    def run_cmd(cmd: list[str], input_str: str | None = None) -> subprocess.CompletedProcess[str]:
        if transport_hook is not None:
            return transport_hook(cmd, input_str)
        proc = subprocess.run(
            cmd,
            input=input_str.encode("utf-8") if input_str is not None else None,
            capture_output=True,
            text=False,
            timeout=30,
            check=True,
        )
        return subprocess.CompletedProcess(
            proc.args,
            proc.returncode,
            proc.stdout.decode("utf-8"),
            proc.stderr.decode("utf-8"),
        )

    # Resolve ~ over SSH first; quotes around a tilde suppress shell expansion.
    probe = "python3 -c 'import json,pathlib,sys; print(pathlib.Path(json.loads(sys.stdin.read())).expanduser().resolve())'"
    resolved = run_cmd(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", collector, probe], json.dumps(remote_feed_path)).stdout.strip()
    if not resolved.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]+", resolved) or any(part in {".", ".."} for part in PurePosixPath(resolved).parts):
        raise ValueError("Collector did not resolve feed path to a safe absolute path")
    remote_feed_path = resolved
    remote_dir = str(PurePosixPath(remote_feed_path).parent)
    remote_tmp = f"{remote_feed_path}.tmp"

    # 1. Ensure remote directory exists
    run_cmd(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", collector, f"mkdir -p {shlex.quote(remote_dir)}"])
    # 2. Write to remote temp file
    run_cmd(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", collector, f"cat > {shlex.quote(remote_tmp)}"],
        input_str=feed_content,
    )
    # 3. Rename atomically on remote
    run_cmd(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", collector, f"mv {shlex.quote(remote_tmp)} {shlex.quote(remote_feed_path)}"]
    )
    return remote_feed_path


def create_publication_manifest(
    root: Path,
    simulation_id: str,
    run_id: str,
    cycle_index: int,
    remote_feed_path: str,
    feed_sha256: str,
    lifecycle_delta: dict[str, Any],
    collector: str,
    inference: str,
) -> tuple[Path, str, dict[str, Any]]:
    """Create versioned immutable simulation publication manifest referencing completed run artifacts."""
    sim_dir = root / "simulations" / simulation_id
    pub_dir = sim_dir / "publications"
    assert_safe_path(root / "simulations", root)
    assert_safe_path(sim_dir, root)
    assert_safe_path(pub_dir, root)
    pub_dir.mkdir(parents=True, exist_ok=True)

    run_dir = root / "runs" / run_id
    assert_safe_path(run_dir, root, must_exist=True)

    run_json_path = run_dir / "run.json"
    assert_safe_path(run_json_path, root, must_exist=True)
    run_data = json.loads(run_json_path.read_text(encoding="utf-8"))

    if run_data.get("status") != "complete":
        raise ValueError(f"Cannot publish run '{run_id}' with status '{run_data.get('status')}'")
    if run_data.get("feed_sha256") != feed_sha256:
        raise ValueError(
            f"Run feed hash does not match staged feed: {run_data.get('feed_sha256')} != {feed_sha256}"
        )

    enriched_dir = run_dir / "enriched"
    assert_safe_path(enriched_dir, root, must_exist=True)

    required_artifacts = {"requirement_mentions.parquet", "job_extractions.parquet", "failed_extractions.parquet", "manifest.json"}
    artifacts = []
    for art_file in sorted(enriched_dir.iterdir()):
        if art_file.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(art_file)):
            raise ValueError(f"Publication artifact must not be a symlink/junction: {art_file}")
        if not art_file.is_file():
            continue
        art_hash = hashlib.sha256(art_file.read_bytes()).hexdigest()
        artifacts.append({
            "name": art_file.name,
            "relpath": str(art_file.relative_to(root)).replace("\\", "/"),
            "sha256": art_hash,
            "size_bytes": art_file.stat().st_size,
        })
    artifact_names = {item["name"] for item in artifacts}
    if not required_artifacts.issubset(artifact_names):
        raise ValueError(f"Enriched run is missing publication artifacts: {sorted(required_artifacts - artifact_names)}")

    pub_id = f"pub-{run_id}"
    manifest_path = pub_dir / f"{pub_id}.json"
    if manifest_path.exists():
        raise FileExistsError(f"Immutable publication manifest already exists: {manifest_path}")

    state_file = root / "state.json"
    assert_safe_path(state_file, root, must_exist=True)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state_modules = state.get("modules", {})
    required_modules = {"apolo-knowledge-data", "apolo-rag", "apolo-skill-standard"}
    if not required_modules.issubset(state_modules):
        raise ValueError(f"Installed state is missing module provenance: {sorted(required_modules - set(state_modules))}")
    module_refs = {}
    for name in sorted(required_modules):
        commit = state_modules[name].get("commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise ValueError(f"Installed state has no valid commit for {name}")
        module_refs[name] = commit

    manifest_data = {
        "contract": "simulation-publication-v1",
        "simulation_id": simulation_id,
        "publication_id": pub_id,
        "run_id": run_id,
        "cycle_index": cycle_index,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "synthetic": True,
        "roles": {
            "collector": collector,
            "inference": inference,
            "workstation": "zephyrus",
        },
        "versions": {
            "manifest_version": "1.0",
            "knowledge_data_ref": module_refs["apolo-knowledge-data"],
            "rag_ref": module_refs["apolo-rag"],
            "skill_standard_ref": module_refs["apolo-skill-standard"],
        },
        "feed": {
            "remote_path": remote_feed_path,
            "sha256": feed_sha256,
            "job_count": lifecycle_delta["total_active"],
        },
        "lifecycle_delta": lifecycle_delta,
        "run_artifacts": {
            "run_json_relpath": str(run_json_path.relative_to(root)).replace("\\", "/"),
            "run_json_sha256": hashlib.sha256(run_json_path.read_bytes()).hexdigest(),
            "enriched_dir_relpath": str(enriched_dir.relative_to(root)).replace("\\", "/"),
            "artifacts": artifacts,
        },
        "counts": {
            "ingest": run_data.get("ingest_counts", {}),
            "result_rows": run_data.get("result_rows", 0),
            "import": {
                "successful_result_rows": run_data.get("import", {}).get("successful_result_rows", 0),
                "requirement_mentions": run_data.get("import", {}).get("requirement_mentions", 0),
            },
            "rag": {
                "job_count": run_data.get("rag", {}).get("job_count"),
                "sample_size": run_data.get("rag", {}).get("sample_size"),
            },
        },
    }

    content = json.dumps(manifest_data, indent=2) + "\n"
    # Write immutably (x mode fails if file exists) with strict LF newlines across OS
    with open(manifest_path, "x", encoding="utf-8", newline="\n") as f:
        f.write(content)

    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return manifest_path, manifest_sha256, manifest_data


def advance_active_pointer(
    root: Path,
    simulation_id: str,
    manifest_relpath: str,
    manifest_sha256: str,
    run_id: str,
    enriched_dir_relpath: str,
    feed_sha256: str,
) -> Path:
    """Atomically advance the current publication pointer within isolated simulation area."""
    sim_dir = root / "simulations" / simulation_id
    assert_safe_path(sim_dir, root, must_exist=True)

    manifest_full = root / manifest_relpath
    assert_safe_path(manifest_full, root, must_exist=True)
    actual_hash = hashlib.sha256(manifest_full.read_bytes()).hexdigest()
    if actual_hash != manifest_sha256:
        raise ValueError(f"Manifest sha256 mismatch before activation: {actual_hash} != {manifest_sha256}")

    enriched_full = root / enriched_dir_relpath
    assert_safe_path(enriched_full, root, must_exist=True)
    if not (enriched_full / "requirement_mentions.parquet").exists():
        raise ValueError(f"Enriched directory missing requirement_mentions.parquet: {enriched_full}")

    pointer_data = {
        "simulation_id": simulation_id,
        "run_id": run_id,
        "manifest_relpath": manifest_relpath.replace("\\", "/"),
        "manifest_sha256": manifest_sha256,
        "enriched_dir_relpath": enriched_dir_relpath.replace("\\", "/"),
        "feed_sha256": feed_sha256,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic": True,
    }

    pointer_target = sim_dir / "current.json"
    temp_pointer = sim_dir / f"current.json.tmp.{os.getpid()}_{secrets.token_hex(4)}"
    assert_safe_path(pointer_target, root)
    assert_safe_path(temp_pointer, root)
    temp_pointer.write_text(json.dumps(pointer_data, indent=2) + "\n", encoding="utf-8")

    # Atomic rename in same directory
    os.replace(temp_pointer, pointer_target)
    return pointer_target


def read_active_publication(root: Path, simulation_id: str) -> dict[str, Any]:
    """Read and validate the active publication pointed to by current.json."""
    root_resolved = root.resolve()
    sim_dir = root_resolved / "simulations" / simulation_id
    assert_safe_path(sim_dir, root_resolved, must_exist=True)

    pointer_file = sim_dir / "current.json"
    assert_safe_path(pointer_file, root_resolved, must_exist=True)

    try:
        pointer = json.loads(pointer_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"current.json contains invalid JSON: {exc}") from exc

    manifest_rel = pointer.get("manifest_relpath")
    expected_manifest_hash = pointer.get("manifest_sha256")
    if not manifest_rel or not expected_manifest_hash:
        raise ValueError("current.json is missing manifest_relpath or manifest_sha256")

    manifest_file = root_resolved / manifest_rel
    assert_safe_path(manifest_file, root_resolved, must_exist=True)

    actual_manifest_hash = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    if actual_manifest_hash != expected_manifest_hash:
        raise ValueError(
            f"Active publication manifest hash mismatch: expected {expected_manifest_hash}, got {actual_manifest_hash}"
        )

    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))

    run_id = pointer.get("run_id")
    expected_run_id = f"{simulation_id}-c"
    if pointer.get("simulation_id") != simulation_id or not isinstance(run_id, str) or not run_id.startswith(expected_run_id):
        raise ValueError("Active pointer simulation/run identity does not match")
    expected_manifest_rel = f"simulations/{simulation_id}/publications/pub-{run_id}.json"
    if manifest_rel.replace("\\", "/") != expected_manifest_rel:
        raise ValueError("Active pointer manifest path does not match its run")
    if manifest_data.get("simulation_id") != simulation_id or manifest_data.get("run_id") != run_id or manifest_data.get("publication_id") != f"pub-{run_id}":
        raise ValueError("Active manifest identity does not match pointer")
    if manifest_data.get("feed", {}).get("sha256") != pointer.get("feed_sha256"):
        raise ValueError("Active pointer feed hash does not match manifest")

    enriched_rel = pointer.get("enriched_dir_relpath")
    if not enriched_rel:
        raise ValueError("current.json is missing enriched_dir_relpath")
    enriched_dir = root_resolved / enriched_rel
    assert_safe_path(enriched_dir, root_resolved, must_exist=True)
    expected_enriched_rel = f"runs/{run_id}/enriched"
    if enriched_rel.replace("\\", "/") != expected_enriched_rel or manifest_data.get("run_artifacts", {}).get("enriched_dir_relpath") != expected_enriched_rel:
        raise ValueError("Active enriched path does not match its run")

    run_json_rel = f"runs/{run_id}/run.json"
    run_info = manifest_data.get("run_artifacts", {})
    run_json = root_resolved / run_json_rel
    if run_info.get("run_json_relpath") != run_json_rel:
        raise ValueError("Active run report path does not match its run")
    assert_safe_path(run_json, root_resolved, must_exist=True)
    if hashlib.sha256(run_json.read_bytes()).hexdigest() != run_info.get("run_json_sha256"):
        raise ValueError("Active run report hash mismatch")
    run_data = json.loads(run_json.read_text(encoding="utf-8"))
    if run_data.get("status") != "complete" or run_data.get("run_id") != run_id or run_data.get("feed_sha256") != pointer.get("feed_sha256"):
        raise ValueError("Active run report does not match pointer")

    artifacts = run_info.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Active manifest has no artifact inventory")
    required_artifacts = {"requirement_mentions.parquet", "job_extractions.parquet", "failed_extractions.parquet", "manifest.json"}
    found_names = set()
    for artifact in artifacts:
        name = artifact.get("name")
        expected_rel = f"{expected_enriched_rel}/{name}"
        if not isinstance(name, str) or artifact.get("relpath", "").replace("\\", "/") != expected_rel:
            raise ValueError("Active artifact path does not match enriched run")
        artifact_path = root_resolved / expected_rel
        assert_safe_path(artifact_path, root_resolved, must_exist=True)
        if not artifact_path.is_file() or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact.get("sha256") or artifact_path.stat().st_size != artifact.get("size_bytes"):
            raise ValueError(f"Active artifact hash/size mismatch: {name}")
        found_names.add(name)
    if not required_artifacts.issubset(found_names):
        raise ValueError(f"Active manifest missing artifacts: {sorted(required_artifacts - found_names)}")

    return {
        "pointer": pointer,
        "manifest": manifest_data,
        "enriched_dir": str(enriched_dir),
    }


def probe_rag_consumer(
    root: Path,
    active_pub: dict[str, Any],
    *,
    probe_runner: Callable[[Path, dict[str, str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Query real RAG tool pointing to active publication enriched directory."""
    root_resolved = root.resolve()
    enriched_dir = Path(active_pub["enriched_dir"])
    assert_safe_path(enriched_dir, root_resolved, must_exist=True)

    repos = root_resolved / "repos"
    knowledge = repos / "apolo-knowledge-data"
    standard = root_resolved / "artifacts" / "skill-standard"
    rag_dir = repos / "apolo-rag"
    bindir = "Scripts" if os.name == "nt" else "bin"
    interpreter = "python.exe" if os.name == "nt" else "python"
    rag_python = rag_dir / ".venv" / bindir / interpreter

    env = {
        "APOLO_KNOWLEDGE_DATA_ROOT": str(knowledge),
        "APOLO_SKILL_STANDARD_DIR": str(standard),
        "APOLO_HADRON_DIR": str(enriched_dir),
    }

    if probe_runner is not None:
        return probe_runner(rag_python, env)

    if not rag_python.exists():
        raise FileNotFoundError(f"apolo-rag python interpreter not found: {rag_python}")

    child_env = os.environ.copy()
    child_env.pop("APOLO_SETUP_MODE", None)
    child_env.update(env)
    child_env["PYTHONIOENCODING"] = "utf-8"

    rag_code = (
        "import json; from apolo_rag.tools.labor_market import get_labor_market; "
        "print(get_labor_market(backend='real').model_dump_json())"
    )
    proc = subprocess.run(
        [str(rag_python), "-c", rag_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"RAG consumer probe failed (returncode {proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout)


def default_processor(args: list[str]) -> int:
    """Default processor invoking remote_pipeline in-process."""
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from scripts.remote_pipeline import run as remote_run

    try:
        return remote_run(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def run_simulation(
    root: Path,
    simulation_id: str,
    *,
    collector: str = "deb",
    inference: str = "hadron",
    inference_root: str = "~/apolo-node",
    interval_seconds: float = 1.0,
    processor: Callable[[list[str]], int] | None = None,
    transport_hook: Callable[[list[str], Optional[str]], subprocess.CompletedProcess[str]] | None = None,
    probe_runner: Callable[[Path, dict[str, str]], dict[str, Any]] | None = None,
    cycles_to_run: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute bounded continuous acquisition simulation."""
    if not SIM_ID_RE.fullmatch(simulation_id) or simulation_id in {".", ".."}:
        raise ValueError(f"Invalid simulation_id: {simulation_id}")
    for host in (collector, inference):
        if not SSH_ALIAS_RE.fullmatch(host):
            raise ValueError(f"Unsafe SSH alias: {host}")
    if not SAFE_PATH_RE.fullmatch(inference_root):
        raise ValueError(f"Unsafe inference_root: {inference_root}")

    root_resolved = root.resolve()
    assert_safe_path(root_resolved, root_resolved, must_exist=True)

    if len(simulation_id) > 58:
        raise ValueError("simulation_id must leave room for the per-cycle run-id suffix")
    simulations_dir = root_resolved / "simulations"
    assert_safe_path(simulations_dir, root_resolved)
    sim_dir = root_resolved / "simulations" / simulation_id
    assert_safe_path(sim_dir, root_resolved)
    if sim_dir.exists() or sim_dir.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(sim_dir)):
        raise FileExistsError(f"Simulation already exists; choose a new id: {sim_dir}")
    sim_dir.mkdir(parents=True, exist_ok=True)

    staged_dir = sim_dir / "staged_feeds"
    staged_dir.mkdir(parents=True, exist_ok=True)
    pub_dir = sim_dir / "publications"
    assert_safe_path(staged_dir, root_resolved)
    assert_safe_path(pub_dir, root_resolved)
    pub_dir.mkdir(parents=True, exist_ok=True)

    cycles = cycles_to_run if cycles_to_run is not None else get_scenario_cycles()

    previous_feed_jobs: dict[str, dict[str, Any]] | None = None
    cycle_results: list[dict[str, Any]] = []
    total_gpu_jobs = 0
    proc_fn = processor or default_processor

    for cycle in cycles:
        cycle_idx = cycle["cycle_index"]
        cycle_name = cycle["name"]
        expect_failure = cycle.get("expect_failure", False)
        current_jobs = cycle["jobs"]
        run_id = f"{simulation_id}-c{cycle_idx}"
        if len(run_id) > 64:
            raise ValueError(f"Cycle run id exceeds remote pipeline limit: {run_id}")

        delta = compute_lifecycle_delta(previous_feed_jobs, current_jobs)

        # Invariant: repeat snapshot skips inference and publication
        if delta["is_repeat"]:
            cycle_results.append({
                "cycle_index": cycle_idx,
                "name": cycle_name,
                "status": "skipped_repeat",
                "delta": delta,
                "gpu_jobs": 0,
                "published": False,
            })
            if interval_seconds > 0:
                time.sleep(interval_seconds)
            continue

        # Prepare and stage feed
        feed_content = "".join(json.dumps(job, ensure_ascii=False) + "\n" for job in current_jobs.values())
        feed_sha256 = hashlib.sha256(feed_content.encode("utf-8")).hexdigest()

        # Archive local staged copy for debugging
        local_staged = staged_dir / f"cycle_{cycle_idx}.jsonl"
        assert_safe_path(local_staged, root_resolved)
        local_staged.write_text(feed_content, encoding="utf-8")

        remote_feed_path = f"~/apolo-collector/simulations/{simulation_id}/cycle_{cycle_idx}/feed.jsonl"
        remote_feed_path = stage_feed_on_collector(collector, remote_feed_path, feed_content, transport_hook=transport_hook)
        previous_pointer = sim_dir / "current.json"
        previous_pointer_bytes = previous_pointer.read_bytes() if previous_pointer.exists() else None
        runs_dir = root_resolved / "runs"
        run_dir = runs_dir / run_id
        assert_safe_path(runs_dir, root_resolved)
        assert_safe_path(run_dir, root_resolved)
        if run_dir.exists() or run_dir.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(run_dir)):
            raise FileExistsError(f"Run already exists; refusing to resume/overwrite: {run_dir}")

        # Run pipeline
        cmd_args = [
            "--root", str(root_resolved),
            "--collector", collector,
            "--feed", remote_feed_path,
            "--inference", inference,
            "--inference-root", inference_root,
            "--run-id", run_id,
            "--synthetic",
        ]

        proc_ret = proc_fn(cmd_args)

        if expect_failure:
            if proc_ret == 0:
                raise RuntimeError(f"Cycle {cycle_idx} ({cycle_name}) was expected to fail but succeeded")

            # Check that previous pointer remained intact
            current_pointer_path = sim_dir / "current.json"
            current_pointer_bytes = current_pointer_path.read_bytes() if current_pointer_path.exists() else None
            pointer_preserved = current_pointer_bytes == previous_pointer_bytes
            if not pointer_preserved:
                raise RuntimeError("Controlled failure changed the active pointer")
            if previous_pointer_bytes is not None:
                read_active_publication(root_resolved, simulation_id)

            cycle_results.append({
                "cycle_index": cycle_idx,
                "name": cycle_name,
                "status": "expected_failure",
                "returncode": proc_ret,
                "pointer_preserved": pointer_preserved,
                "gpu_jobs": 0,
                "published": False,
            })
        else:
            if proc_ret != 0:
                raise RuntimeError(f"Cycle {cycle_idx} ({cycle_name}) failed unexpectedly with code {proc_ret}")

            # Verify the candidate through RAG before creating a manifest or changing the active pointer.
            run_dir = root_resolved / "runs" / run_id
            enriched_dir = run_dir / "enriched"
            run_json_path = run_dir / "run.json"
            run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
            import_data = run_data.get("import", {})
            successful = import_data.get("successful_result_rows", 0)
            failed = import_data.get("failed_result_rows", 0) + import_data.get("evidence_failed_result_rows", 0)
            if run_data.get("run_id") != run_id or run_data.get("status") != "complete" or successful != len(current_jobs) or failed:
                raise ValueError("Candidate run counts/status do not match active feed")
            rag_probe_res = probe_rag_consumer(root_resolved, {"enriched_dir": str(enriched_dir)}, probe_runner=probe_runner)
            if rag_probe_res.get("job_count") != len(current_jobs) or rag_probe_res.get("sample_size") != successful:
                raise ValueError("RAG candidate counts do not match successfully imported feed")
            run_data["rag"] = {"job_count": rag_probe_res["job_count"], "sample_size": rag_probe_res["sample_size"]}
            run_json_path.write_text(json.dumps(run_data, indent=2) + "\n", encoding="utf-8")

            # Publish only after all candidate checks pass.
            manifest_path, manifest_sha256, manifest_data = create_publication_manifest(
                root_resolved,
                simulation_id,
                run_id,
                cycle_idx,
                remote_feed_path,
                feed_sha256,
                delta,
                collector,
                inference,
            )

            rel_manifest = str(manifest_path.relative_to(root_resolved)).replace("\\", "/")
            rel_enriched = f"runs/{run_id}/enriched"
            advance_active_pointer(
                root_resolved,
                simulation_id,
                rel_manifest,
                manifest_sha256,
                run_id,
                rel_enriched,
                feed_sha256,
            )

            job_count = len(current_jobs)
            total_gpu_jobs += job_count
            cycle_results.append({
                "cycle_index": cycle_idx,
                "name": cycle_name,
                "status": "complete",
                "run_id": run_id,
                "publication_id": manifest_data["publication_id"],
                "delta": delta,
                "gpu_jobs": job_count,
                "published": True,
                "rag_probe": rag_probe_res,
            })
            previous_feed_jobs = current_jobs

        if interval_seconds > 0:
            time.sleep(interval_seconds)

    if total_gpu_jobs > 12:
        raise ValueError(f"Exceeded GPU job quota (expected <= 12, got {total_gpu_jobs})")

    active_summary = read_active_publication(root_resolved, simulation_id)
    report = {
        "simulation_id": simulation_id,
        "status": "success",
        "total_cycles": len(cycle_results),
        "total_gpu_jobs": total_gpu_jobs,
        "cycles": cycle_results,
        "active_publication": {
            "publication_id": active_summary["manifest"]["publication_id"],
            "run_id": active_summary["manifest"]["run_id"],
            "feed_sha256": active_summary["pointer"]["feed_sha256"],
            "enriched_dir": active_summary["enriched_dir"],
        },
    }

    report_path = sim_dir / "simulation_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Installed workstation node root")
    parser.add_argument("--collector", default="deb", help="SSH alias reachable from this workstation")
    parser.add_argument("--inference", default="hadron", help="SSH alias reachable from this workstation")
    parser.add_argument("--inference-root", default="~/apolo-node", help="Absolute remote node root or ~/path")
    parser.add_argument("--simulation-id", required=True, help="Safe simulation identifier")
    parser.add_argument("--interval-seconds", type=float, default=1.0, help="Seconds to sleep between cycles")
    parser.add_argument("--check-only", action="store_true", help="Validate active publication without running cycles")

    args = parser.parse_args(argv)

    if args.check_only:
        try:
            active = read_active_publication(args.root, args.simulation_id)
            print(json.dumps(active, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
            return 1

    try:
        report = run_simulation(
            args.root,
            args.simulation_id,
            collector=args.collector,
            inference=args.inference,
            inference_root=args.inference_root,
            interval_seconds=args.interval_seconds,
        )
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
