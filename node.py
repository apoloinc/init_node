#!/usr/bin/env python3
"""init_node: Cross-platform bootstrap CLI for Apolo distributed nodes.

Initializes, verifies, and tests intercommunication between node roles
(coordinator, rag, inference) without adding external services or daemons.
Requires Python >= 3.10.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info < (3, 10):
    sys.stderr.write("Error: init_node requires Python 3.10 or higher.\n")
    sys.exit(1)

PEER_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def normalize_git_url(url: str) -> str:
    """Normalize git URL for stable comparison."""
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    u = u.rstrip("/")
    return u.lower()


def resolve_safe_root(path_str: str) -> Path:
    """Resolve and validate a root workspace path."""
    if not path_str or not path_str.strip():
        raise ValueError("Root path cannot be empty.")
    root = Path(path_str).resolve()
    # Protect root filesystems
    if str(root) == "/" or (sys.platform == "win32" and root.drive and str(root) == f"{root.drive}\\"):
        raise ValueError("Root path cannot be filesystem root directory.")
    return root


def load_manifest(manifest_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the repository manifest or an explicitly provided one."""
    if manifest_path:
        p = Path(manifest_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Manifest file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        validate_manifest(manifest)
        return manifest

    local_manifest = Path(__file__).resolve().parent / "manifest.json"
    if not local_manifest.is_file():
        raise FileNotFoundError(f"Manifest file not found: {local_manifest}")
    with open(local_manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: Dict[str, Any]) -> None:
    """Reject incomplete or unsafe module references before touching the filesystem."""
    profiles = manifest.get("profiles")
    modules = manifest.get("modules")
    if not isinstance(profiles, dict) or not profiles or not isinstance(modules, dict) or not modules:
        raise ValueError("Manifest must define non-empty profiles and modules objects.")
    for name, members in profiles.items():
        if not isinstance(members, list) or not members:
            raise ValueError(f"Profile '{name}' must contain at least one module.")
        for member in members:
            if member not in modules:
                raise ValueError(f"Profile '{name}' references undefined module '{member}'.")
    for name, spec in modules.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
            raise ValueError(f"Module '{name}' must define a repository URL.")
        if not spec['url'] or spec['url'].startswith('-') or any(c in spec['url'] for c in '\r\n'):
            raise ValueError(f"Module '{name}' has an unsafe repository URL.")
        if spec.get('runtime', 'stdlib') not in {'stdlib', 'knowledge-lock', 'uv-project', 'build-standard'}:
            raise ValueError(f"Module '{name}' has an unknown runtime.")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(spec.get("ref", ""))):
            raise ValueError(f"Module '{name}' must pin a full 40-character Git SHA.")
        if "import_name" in spec and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec["import_name"]):
            raise ValueError(f"Module '{name}' has an invalid import name.")
        dir_name = spec.get("dir_name", name)
        if not isinstance(dir_name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', dir_name) or dir_name in (".", ".."):
            raise ValueError(f"Module '{name}' has an unsafe directory name.")


def run_git(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    """Execute git with argument list (safe against shell injection)."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_dirty(repo_dir: Path) -> Tuple[bool, str]:
    """Check if repository has uncommitted changes or untracked files."""
    res = run_git(["status", "--porcelain"], cwd=repo_dir)
    if res.returncode != 0:
        raise RuntimeError(f"git status failed in '{repo_dir}': {res.stderr.strip()}")
    dirty_output = res.stdout.strip()
    return (len(dirty_output) > 0, dirty_output)


def get_git_commit(repo_dir: Path) -> str:
    """Retrieve current commit SHA in repository."""
    res = run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    if res.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed in '{repo_dir}': {res.stderr.strip()}")
    return res.stdout.strip()


def get_git_origin(repo_dir: Path) -> str:
    """Retrieve remote origin URL for repository."""
    res = run_git(["remote", "get-url", "origin"], cwd=repo_dir)
    if res.returncode != 0:
        res = run_git(["config", "--get", "remote.origin.url"], cwd=repo_dir)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to get remote origin in '{repo_dir}': {res.stderr.strip()}")
    return res.stdout.strip()


def check_and_sync_module(
    module_name: str,
    module_spec: Dict[str, Any],
    repos_dir: Path,
    bundle_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Clone or verify existing module repository. Refuses dirty/wrong-origin/unexpected revisions."""
    dir_name = module_spec.get("dir_name", module_name)
    if "/" in dir_name or "\\" in dir_name or ".." in dir_name:
        raise ValueError(f"Invalid directory name for module '{module_name}': '{dir_name}'")

    target_dir = repos_dir / dir_name
    if not target_dir.resolve().is_relative_to(repos_dir.resolve()):
        raise ValueError(f"Module path escapes repositories directory: {target_dir}")
    expected_url = module_spec["url"]
    expected_ref = module_spec["ref"]

    if target_dir.exists():
        is_worktree = run_git(["rev-parse", "--is-inside-work-tree"], cwd=target_dir)
        if is_worktree.returncode != 0 or is_worktree.stdout.strip() != "true":
            raise RuntimeError(
                f"Path '{target_dir}' exists but is not a git repository. Refusing to overwrite."
            )

        actual_origin = get_git_origin(target_dir)
        if normalize_git_url(actual_origin) != normalize_git_url(expected_url):
            raise RuntimeError(
                f"Repository '{module_name}' at '{target_dir}' has remote origin '{actual_origin}', "
                f"which does not match expected '{expected_url}'. Refusing to overwrite."
            )

        dirty, dirty_output = is_git_dirty(target_dir)
        if dirty:
            raise RuntimeError(
                f"Repository '{module_name}' at '{target_dir}' is dirty (uncommitted/untracked files):\n"
                f"{dirty_output}\nRefusing to overwrite dirty repository."
            )

        actual_commit = get_git_commit(target_dir)
        if actual_commit.lower() != expected_ref.lower():
            raise RuntimeError(
                f"Repository '{module_name}' at '{target_dir}' is at revision '{actual_commit}', "
                f"which does not match expected pinned revision '{expected_ref}'. "
                "Refusing to overwrite unexpected revision."
            )

        return {
            "name": module_name,
            "status": "already_present_and_clean",
            "path": str(target_dir),
            "commit": actual_commit,
            "url": actual_origin,
        }

    # Repository does not exist: initialize from a transferred Git bundle or clone.
    bundle = bundle_dir / f"{module_name}.bundle" if bundle_dir else None
    if bundle is not None and bundle.is_file():
        clone_res = run_git(["clone", str(bundle), str(target_dir)])
    else:
        clone_res = run_git(["clone", expected_url, str(target_dir)])
    if clone_res.returncode != 0:
        raise RuntimeError(
            f"Failed to clone '{expected_url}' into '{target_dir}':\n{clone_res.stderr.strip()}"
        )

    checkout_res = run_git(["checkout", "--detach", expected_ref], cwd=target_dir)
    if checkout_res.returncode != 0:
        raise RuntimeError(
            f"Failed to checkout '{expected_ref}' in '{target_dir}':\n{checkout_res.stderr.strip()}"
        )

    if bundle is not None and bundle.is_file():
        origin_res = run_git(["remote", "set-url", "origin", expected_url], cwd=target_dir)
        if origin_res.returncode != 0:
            raise RuntimeError(f"Failed to set canonical origin for '{module_name}': {origin_res.stderr.strip()}")

    actual_commit = get_git_commit(target_dir)
    return {
        "name": module_name,
        "status": "cloned",
        "path": str(target_dir),
        "commit": actual_commit,
        "url": expected_url,
    }


def install_module_dependencies(
    module_name: str,
    module_spec: Dict[str, Any],
    repos_dir: Path,
) -> Dict[str, Any]:
    """Install dependencies for module using existing uv if available."""
    dir_name = module_spec.get("dir_name", module_name)
    target_dir = repos_dir / dir_name
    runtime = module_spec.get("runtime", "stdlib")
    if not (target_dir / '.venv').resolve().is_relative_to(target_dir.resolve()):
        raise ValueError('Virtual environment escapes module directory.')

    uv_path = shutil.which("uv")
    if runtime == "knowledge-lock":
        if not uv_path:
            raise RuntimeError(
                f"Module '{module_name}' requires 'uv' for dependency management, but 'uv' "
                "was not found in PATH.\nAction required: Install uv (e.g. from https://docs.astral.sh/uv/) "
                "and re-run with --install.\n(init_node will never curl pipe or install global binaries automatically)."
            )
        version_res = subprocess.run(["uv", "python", "find", "3.12"], capture_output=True, text=True, check=False)
        if version_res.returncode != 0:
            raise RuntimeError(
                f"Module '{module_name}' requires Python 3.12 for its pinned runtime. Install Python 3.12 or run 'uv python install 3.12'."
            )
        venv_dir = target_dir / ".venv"
        if venv_dir.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(venv_dir)):
            raise ValueError(f"Refusing to use linked virtual environment path: {venv_dir}")
        venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        venv_res = subprocess.run(["uv", "venv", "--allow-existing", "--python", "3.12", str(venv_dir)], capture_output=True, text=True, check=False)
        if venv_res.returncode != 0:
            raise RuntimeError(f"Failed to create Python 3.12 environment for '{module_name}': {venv_res.stderr.strip()}")
        lock = Path(__file__).resolve().parent / module_spec["lock_file"]
        if not lock.resolve().is_relative_to(Path(__file__).resolve().parent / 'locks'):
            raise ValueError('Dependency lock must be inside locks/.')
        install_cmd = ["uv", "pip", "sync", "--python", str(venv_python), str(lock)]
        res = subprocess.run(install_cmd, cwd=target_dir, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to install dependencies for '{module_name}': {res.stderr.strip()}")
        package_cmd = ["uv", "pip", "install", "--python", str(venv_python), "--no-deps", str(target_dir)]
        package_res = subprocess.run(package_cmd, cwd=target_dir, capture_output=True, text=True, check=False)
        if package_res.returncode != 0:
            raise RuntimeError(f"Failed to install '{module_name}' into its isolated environment: {package_res.stderr.strip()}")
        return {"name": module_name, "status": "installed", "runtime": "uv", "cmd": install_cmd + ["&&"] + package_cmd}
    if runtime == "uv-project":
        if not uv_path:
            raise RuntimeError(
                f"Module '{module_name}' requires 'uv' for dependency management. "
                "Install uv manually; init_node never uses curl pipe or installs global binaries."
            )
        install_cmd = ["uv", "sync", "--frozen", "--no-dev", "--python", "3.12"]
        env = os.environ.copy()
        env["UV_PROJECT_ENVIRONMENT"] = str(target_dir / ".venv")
        env.pop("UV_PROJECT", None)
        env.pop("UV_WORKING_DIRECTORY", None)
        res = subprocess.run(install_cmd, cwd=target_dir, env=env, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to install dependencies for '{module_name}': {res.stderr.strip()}")
        return {"name": module_name, "status": "installed", "runtime": "uv", "cmd": install_cmd}

    if runtime == "build-standard":
        output_dir = repos_dir.parent / "artifacts" / "skill-standard"
        for path in (output_dir.parent, output_dir):
            if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
                raise ValueError(f"Refusing linked skill-standard output path: {path}")
        if output_dir.resolve() != output_dir.absolute():
            raise ValueError(f"Skill-standard output resolves outside its expected path: {output_dir}")
        if not output_dir.resolve().is_relative_to(repos_dir.parent.resolve()):
            raise ValueError("Skill-standard output escapes workspace.")
        python_res = subprocess.run(["uv", "python", "find", "3.12"], capture_output=True, text=True, check=False)
        if python_res.returncode != 0:
            raise RuntimeError("Building the skill-standard release requires Python 3.12 via uv.")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(target_dir / "src")
        cmd = [python_res.stdout.strip(), "-m", "apolo_skill_standard.cli", "--source", str(target_dir / "standard"), "build", "--output", str(output_dir)]
        res = subprocess.run(cmd, cwd=target_dir, env=env, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to build skill-standard release: {res.stderr.strip()}")
        return {"name": module_name, "status": "built", "runtime": "stdlib", "output": str(output_dir)}

    # stdlib runtime (e.g. korax, apolo-skill-standard)
    return {
        "name": module_name,
        "status": "inferred_stdlib",
        "runtime": "stdlib",
        "message": "Standard-library runtime inferred; external package sync not required.",
    }


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize a node role profile at the given root."""
    try:
        manifest = load_manifest(args.manifest)
        profiles = manifest.get("profiles", {})
        if args.profile not in profiles:
            sys.stderr.write(
                f"Error: Unknown profile '{args.profile}'. Available profiles: {list(profiles.keys())}\n"
            )
            return 1

        root_path = resolve_safe_root(args.root)
        root_path.mkdir(parents=True, exist_ok=True)
        repos_dir = root_path / "repos"
        if not repos_dir.resolve().is_relative_to(root_path):
            raise ValueError('Repositories directory escapes workspace.')
        repos_dir.mkdir(parents=True, exist_ok=True)

        module_names = profiles[args.profile]
        modules_spec = manifest.get("modules", {})
        validate_manifest(manifest)
        synced_modules: Dict[str, Any] = {}
        install_results: Dict[str, Any] = {}

        for mod_name in module_names:
            if mod_name not in modules_spec:
                raise KeyError(f"Module '{mod_name}' not defined in manifest modules.")
            mod_spec = modules_spec[mod_name]
            sync_res = check_and_sync_module(mod_name, mod_spec, repos_dir, Path(args.bundle_dir).resolve() if getattr(args, "bundle_dir", None) else None)
            synced_modules[mod_name] = sync_res

            if args.install:
                inst_res = install_module_dependencies(mod_name, mod_spec, repos_dir)
                install_results[mod_name] = inst_res

        # Write persisted state
        state_file = root_path / "state.json"
        state_data = {
            "node_init_version": "1.0",
            "profile": args.profile,
            "root": str(root_path),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "installed": bool(args.install),
            "modules": {
                name: {
                    "url": modules_spec[name]["url"],
                    "ref": modules_spec[name]["ref"],
                    "path": str((repos_dir / modules_spec[name].get("dir_name", name)).relative_to(root_path)),
                    "runtime": modules_spec[name].get("runtime", "stdlib"),
                    "commit": synced_modules[name]["commit"],
                }
                for name in module_names
            },
        }

        if state_file.is_symlink():
            raise ValueError('state.json must not be a symlink.')
        temporary_state = state_file.with_suffix('.json.tmp')
        if temporary_state.is_symlink():
            raise ValueError('Temporary state must not be a symlink.')
        with open(temporary_state, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
        os.replace(temporary_state, state_file)

        output = {
            "status": "ok",
            "command": "init",
            "profile": args.profile,
            "root": str(root_path),
            "modules": synced_modules,
            "installation": install_results if args.install else "skipped",
            "state_file": str(state_file),
        }
        sys.stdout.write(json.dumps(output, indent=2) + "\n")
        return 0

    except Exception as exc:
        err_out = {
            "status": "error",
            "command": "init",
            "error": str(exc),
        }
        sys.stderr.write(json.dumps(err_out, indent=2) + "\n")
        return 1


def check_http_health(url: str, timeout: int = 5) -> Dict[str, Any]:
    """Verify an HTTP health check URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "init_node-doctor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            ok = 200 <= code < 300
            return {"url": url, "ok": ok, "status_code": code}
    except urllib.error.HTTPError as exc:
        return {"url": url, "ok": False, "status_code": exc.code, "error": str(exc)}
    except Exception as exc:
        return {"url": url, "ok": False, "status_code": None, "error": str(exc)}


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose local node health, pinned revisions, dirty trees, and HTTP health URLs."""
    overall_ok = True
    report: Dict[str, Any] = {
        "status": "ok",
        "command": "doctor",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "root": args.root,
        "checks": {},
    }

    try:
        root_path = resolve_safe_root(args.root)
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"Invalid root path: {exc}"
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 1

    # Check state file
    state_file = root_path / "state.json"
    if not state_file.is_file():
        report["status"] = "failed"
        report["checks"]["state_file"] = {"ok": False, "error": "state.json not found. Run 'init' first."}
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 1

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as exc:
        report["status"] = "failed"
        report["checks"]["state_file"] = {"ok": False, "error": f"Failed to parse state.json: {exc}"}
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 1

    profile = state.get("profile")
    try:
        manifest = load_manifest(args.manifest)
        expected_names = manifest['profiles'][profile]
        if set(state.get('modules', {})) != set(expected_names):
            raise ValueError('State modules differ from the required profile modules. Run init again.')
        for name in expected_names:
            expected_path = Path('repos') / manifest['modules'][name].get('dir_name', name)
            actual_path = Path(state['modules'][name]['path'])
            if actual_path != expected_path or not (root_path / actual_path).resolve().is_relative_to(root_path):
                raise ValueError(f'Unsafe state path for {name}.')
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = f'Invalid node state or manifest: {exc}'
        sys.stdout.write(json.dumps(report, indent=2) + '\n')
        return 1
    report["profile"] = profile
    report["checks"]["state_file"] = {"ok": True, "path": str(state_file), "profile": profile}

    # Check system executables
    executables_check: Dict[str, Any] = {"ok": True}
    py_ok = sys.version_info >= (3, 10)
    executables_check["python"] = {
        "ok": py_ok,
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "executable": sys.executable,
    }
    if not py_ok:
        executables_check["ok"] = False
        overall_ok = False

    git_res = run_git(["--version"])
    git_ok = git_res.returncode == 0
    executables_check["git"] = {
        "ok": git_ok,
        "version": git_res.stdout.strip() if git_ok else None,
        "error": git_res.stderr.strip() if not git_ok else None,
    }
    if not git_ok:
        executables_check["ok"] = False
        overall_ok = False

    uv_path = shutil.which("uv")
    if uv_path:
        uv_res = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=False)
        executables_check["uv"] = {
            "ok": uv_res.returncode == 0,
            "version": uv_res.stdout.strip() if uv_res.returncode == 0 else None,
            "path": uv_path,
        }
    else:
        executables_check["uv"] = {"ok": False, "present": False, "note": "Optional unless modules require uv"}

    report["checks"]["executables"] = executables_check

    # Check modules
    modules_report: Dict[str, Any] = {}
    manifest_modules = manifest.get("modules", {})
    state_modules = state.get("modules", {})

    for mod_name, mod_info in state_modules.items():
        spec = manifest_modules.get(mod_name, {})
        expected_ref = spec.get("ref", mod_info.get("ref"))
        expected_url = spec.get("url", mod_info.get("url"))
        repo_path = root_path / mod_info.get("path", f"repos/{mod_name}")

        mod_chk: Dict[str, Any] = {
            "ok": True,
            "path": str(repo_path),
            "expected_ref": expected_ref,
        }
        runtime = spec.get('runtime', 'stdlib')
        if runtime in {'knowledge-lock', 'uv-project'}:
            python_path = repo_path / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            package = spec.get('import_name', 'apolo_knowledge_data' if runtime == 'knowledge-lock' else 'apolo_rag')
            if python_path.is_file():
                extra_imports = ', pandas, pyarrow' if runtime == 'knowledge-lock' else ''
                probe = subprocess.run([str(python_path), '-c', f'import {package}{extra_imports}; import sys; assert sys.version_info[:2] == (3,12)'], capture_output=True, text=True)
                mod_chk['runtime_ready'] = probe.returncode == 0
                if probe.returncode:
                    mod_chk['runtime_error'] = probe.stderr.strip()
            else:
                mod_chk['runtime_ready'] = False
                mod_chk['runtime_error'] = 'Missing environment. Run init --install.'
            if not mod_chk['runtime_ready']:
                mod_chk['ok'] = False
                overall_ok = False
        if runtime == 'build-standard':
            catalog = root_path / 'artifacts/skill-standard/knowledge_catalog.json'
            mod_chk['catalog_ready'] = catalog.is_file()
            if not catalog.is_file():
                mod_chk['ok'] = False
                overall_ok = False

        if not repo_path.is_dir():
            mod_chk["ok"] = False
            mod_chk["error"] = "Directory missing"
            overall_ok = False
            modules_report[mod_name] = mod_chk
            continue

        is_repo = run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_path)
        if is_repo.returncode != 0 or is_repo.stdout.strip() != "true":
            mod_chk["ok"] = False
            mod_chk["error"] = "Not a git repository"
            overall_ok = False
            modules_report[mod_name] = mod_chk
            continue

        try:
            origin = get_git_origin(repo_path)
            mod_chk["origin"] = origin
            if expected_url and normalize_git_url(origin) != normalize_git_url(expected_url):
                mod_chk["ok"] = False
                mod_chk["origin_mismatch"] = True
                overall_ok = False
        except Exception as exc:
            mod_chk["ok"] = False
            mod_chk["origin_error"] = str(exc)
            overall_ok = False

        try:
            current_commit = get_git_commit(repo_path)
            mod_chk["commit"] = current_commit
            if expected_ref and current_commit.lower() != expected_ref.lower():
                mod_chk["ok"] = False
                mod_chk["revision_mismatch"] = True
                overall_ok = False
        except Exception as exc:
            mod_chk["ok"] = False
            mod_chk["commit_error"] = str(exc)
            overall_ok = False

        try:
            dirty, dirty_details = is_git_dirty(repo_path)
            mod_chk["dirty"] = dirty
            if dirty:
                mod_chk["ok"] = False
                mod_chk["dirty_details"] = dirty_details.splitlines()[:5]
                overall_ok = False
        except Exception as exc:
            mod_chk["ok"] = False
            mod_chk["dirty_check_error"] = str(exc)
            overall_ok = False

        modules_report[mod_name] = mod_chk

    report["checks"]["modules"] = modules_report

    # Optional HTTP health checks
    urls_to_check: List[str] = []
    if args.url:
        urls_to_check.extend(args.url)

    if urls_to_check:
        http_results = []
        for u in urls_to_check:
            res = check_http_health(u)
            http_results.append(res)
            if not res["ok"]:
                overall_ok = False
        report["checks"]["http_health"] = http_results

    report["status"] = "ok" if overall_ok else "failed"
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if overall_ok else 1


def check_single_ssh_peer(peer: str, timeout: int = 5) -> Dict[str, Any]:
    """Test SSH connectivity and roundtrip Python token to verify peer."""
    if not PEER_REGEX.match(peer):
        return {
            "peer": peer,
            "ok": False,
            "error": f"Invalid peer hostname '{peer}'. Hostnames must be alphanumeric and safe.",
        }

    nonce = secrets.token_hex(16)
    req_json = json.dumps({"action": "ping", "token": nonce})

    # Remote payload: echo token via stdin with platform metadata
    remote_script = (
        "import sys, json; "
        "data = json.loads(sys.stdin.read()); "
        "print(json.dumps({'status': 'ok', 'echo': data.get('token'), 'platform': sys.platform, 'py': sys.version.split()[0]}))"
    )
    remote_cmd = f"python3 -c \"{remote_script}\" 2>/dev/null || python -c \"{remote_script}\""

    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        peer,
        remote_cmd,
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=req_json,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "peer": peer,
                "ok": False,
                "returncode": proc.returncode,
                "error": proc.stderr.strip() or f"SSH exited with code {proc.returncode}",
            }

        stdout_trimmed = proc.stdout.strip()
        data = json.loads(stdout_trimmed)
        if data.get("echo") != nonce:
            return {
                "peer": peer,
                "ok": False,
                "error": f"Token mismatch: expected '{nonce}', got '{data.get('echo')}'",
            }

        return {
            "peer": peer,
            "ok": True,
            "token_verified": True,
            "remote_platform": data.get("platform"),
            "remote_python": data.get("py"),
        }

    except subprocess.TimeoutExpired:
        return {
            "peer": peer,
            "ok": False,
            "error": f"SSH connection timed out after {timeout} seconds",
        }
    except json.JSONDecodeError as exc:
        return {
            "peer": peer,
            "ok": False,
            "error": f"Invalid JSON response from remote peer: {exc}. Raw stdout: {proc.stdout.strip()}",
        }
    except Exception as exc:
        return {
            "peer": peer,
            "ok": False,
            "error": str(exc),
        }


def cmd_check_peers(args: argparse.Namespace) -> int:
    """Check SSH connectivity and token roundtrip to remote peers."""
    peers: List[str] = []

    if args.topology:
        top_path = Path(args.topology).resolve()
        if not top_path.is_file():
            sys.stderr.write(f"Error: Topology file not found: {top_path}\n")
            return 1
        with open(top_path, "r", encoding="utf-8") as f:
            top_data = json.load(f)
        nodes = top_data.get("nodes", {})
        for name, node_info in nodes.items():
            if not node_info.get("is_local", False):
                alias = node_info.get("ssh_alias", name)
                if alias not in peers:
                    peers.append(alias)

    if args.peers:
        for p in args.peers:
            for item in p.replace(",", " ").split():
                clean_p = item.strip()
                if clean_p and clean_p not in peers:
                    peers.append(clean_p)

    if not peers:
        sys.stderr.write("Error: No peers specified. Use --peers or --topology.\n")
        return 1

    overall_ok = True
    peer_results: List[Dict[str, Any]] = []

    for peer in peers:
        res = check_single_ssh_peer(peer, timeout=args.timeout)
        peer_results.append(res)
        if not res["ok"]:
            overall_ok = False

    output = {
        "status": "ok" if overall_ok else "failed",
        "command": "check-peers",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": args.timeout,
        "peer_count": len(peers),
        "results": peer_results,
    }
    sys.stdout.write(json.dumps(output, indent=2) + "\n")
    return 0 if overall_ok else 1


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for init_node."""
    parser = argparse.ArgumentParser(
        prog="node.py",
        description="Bootstrap CLI for Apolo distributed nodes (cross-platform, stdlib-only).",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # init
    p_init = sub.add_parser("init", help="Clone and initialize modules for a specific node role profile.")
    p_init.add_argument(
        "--profile",
        required=True,
        choices=sorted(load_manifest().get("profiles", {})),
        help="Role profile to instantiate.",
    )
    p_init.add_argument(
        "--root",
        required=True,
        help="Root workspace path where node modules and state will be placed.",
    )
    p_init.add_argument(
        "--manifest",
        default=None,
        help="Optional path to custom manifest JSON file.",
    )
    p_init.add_argument("--bundle-dir", default=None, help="Optional directory of NAME.bundle files for offline/private module cloning.")
    p_init.add_argument(
        "--install",
        action="store_true",
        help="Install dependencies for cloned modules via uv sync --frozen --no-dev.",
    )
    p_init.set_defaults(func=cmd_init)

    # doctor
    p_doc = sub.add_parser("doctor", help="Check local node health, pinned Git SHAs, dirty state, and health URLs.")
    p_doc.add_argument(
        "--root",
        required=True,
        help="Root workspace path containing state.json and repos/.",
    )
    p_doc.add_argument(
        "--manifest",
        default=None,
        help="Optional path to custom manifest JSON file.",
    )
    p_doc.add_argument(
        "--url",
        action="append",
        default=[],
        help="HTTP health URL to check (can be specified multiple times).",
    )
    p_doc.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output to stdout (default behavior).",
    )
    p_doc.set_defaults(func=cmd_doctor)

    # check-peers
    p_peers = sub.add_parser("check-peers", help="Verify SSH connectivity and Python token roundtrip to peer nodes.")
    p_peers.add_argument(
        "--peers",
        nargs="*",
        default=[],
        help="Peer SSH hostnames/aliases (e.g. deb hadron or deb,hadron).",
    )
    p_peers.add_argument(
        "--topology",
        default=None,
        help="Optional path to topology JSON file.",
    )
    p_peers.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="SSH connection timeout in seconds (default: 5).",
    )
    p_peers.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output to stdout (default behavior).",
    )
    p_peers.set_defaults(func=cmd_check_peers)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
