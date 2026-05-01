#!/usr/bin/env python3
"""
worktree.py — Git worktree + Docker Compose stack management for isolated dev environments.

Creates a fully isolated development environment for each git branch:
- Git worktree in .worktrees/<slug>/
- Dedicated Docker Compose stack with non-conflicting ports
- Per-worktree .env with isolated port bindings
- Claude Code .claude/settings.json injection (env vars + optional MCP override)
- Optional: ngrok tunnel domain assignment
- Optional: post-startup hook (e.g. database migrations)

Subcommands:
  create <branch> [<base>]  Create worktree + start Docker + run post-start hook
  ensure <branch> [<base>]  Create only if it doesn't already exist
  remove <branch>           Stop Docker stack + delete worktree
  list                      List all worktrees with status
  status [<branch>]         Show ports and URLs for one or all worktrees
  slug <branch>             Print the filesystem slug for a branch name
"""

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ── Repo root detection ────────────────────────────────────────────────────────


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start.resolve()


SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = find_repo_root(SCRIPT_DIR)

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG: dict[str, Any] = {
    "compose_file": "docker-compose.yml",
    "worktrees_dir": ".worktrees",
    "registry_file": ".worktree-registry.json",
    "port_base": 5710,
    "port_step": 100,
    "max_worktrees": 10,
    "project_prefix": "wt",
    "exclude_services": [],
    "health_wait_service": None,
    "post_start": None,
    "ngrok_domains_file": None,
    "env_overrides": {},
    "mcp_override": None,
}


def _load_config() -> dict[str, Any]:
    for candidate in [REPO_ROOT / "worktree-config.yml", SCRIPT_DIR / "worktree-config.yml"]:
        if candidate.exists():
            with open(candidate) as f:
                user = yaml.safe_load(f) or {}
            return {**_DEFAULT_CONFIG, **user}
    print("Warning: worktree-config.yml not found; using defaults.", file=sys.stderr)
    return dict(_DEFAULT_CONFIG)


CONFIG = _load_config()

COMPOSE_FILE = REPO_ROOT / CONFIG["compose_file"]
WORKTREES_DIR = REPO_ROOT / CONFIG["worktrees_dir"]
REGISTRY_PATH = REPO_ROOT / CONFIG["registry_file"]
DB_HEALTH_TIMEOUT = 120

# ── Compose parsing ────────────────────────────────────────────────────────────


def _parse_compose(compose_file: Path) -> tuple[list[str], list[str]]:
    """Return (service_names, host_port_var_names) parsed from docker-compose.yml."""
    if not compose_file.exists():
        return [], []

    data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    services_data: dict[str, Any] = data.get("services") or {}

    exclude = set(CONFIG.get("exclude_services") or [])
    services = [s for s in services_data if s not in exclude]

    var_pattern = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-\d+)?\}")
    port_vars: list[str] = []
    seen: set[str] = set()

    for service in services_data.values():
        for entry in service.get("ports") or []:
            if isinstance(entry, dict):
                host_side = str(entry.get("published", ""))
            elif isinstance(entry, (int, float)):
                continue
            else:
                host_side = str(entry).rsplit(":", 1)[0]

            for var in var_pattern.findall(host_side):
                if var not in seen:
                    port_vars.append(var)
                    seen.add(var)

    return services, port_vars


SERVICES, PORT_VARS = _parse_compose(COMPOSE_FILE)

# ── Port allocation ────────────────────────────────────────────────────────────


def _generate_port_groups() -> list[dict[str, int]]:
    base: int = CONFIG["port_base"]
    step: int = CONFIG["port_step"]
    return [
        {var: base + i * step + j for j, var in enumerate(PORT_VARS)}
        for i in range(CONFIG["max_worktrees"])
    ]


PORT_GROUPS = _generate_port_groups()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _allocate_ports(registry: dict) -> dict[str, int] | None:
    claimed: set[int] = set()
    for wt in registry["worktrees"].values():
        claimed.update(wt["ports"].values())

    for group in PORT_GROUPS:
        ports = list(group.values())
        if any(p in claimed for p in ports):
            continue
        if all(_port_free(p) for p in ports):
            return group
    return None


# ── ngrok domain assignment ────────────────────────────────────────────────────


def _load_ngrok_domains() -> list[str]:
    path_key = CONFIG.get("ngrok_domains_file")
    if not path_key:
        return []
    path = REPO_ROOT / path_key
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _find_running_ngrok_domains() -> set[str]:
    import urllib.request

    active: set[str] = set()
    for port in range(4040, 4050):
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/api/tunnels", timeout=1) as r:
                for tunnel in json.loads(r.read()).get("tunnels", []):
                    url = tunnel.get("public_url", "")
                    if "://" in url:
                        active.add(url.split("://", 1)[1].rstrip("/"))
        except Exception:
            pass
    return active


def _assign_ngrok_domain(registry: dict) -> str | None:
    domains = _load_ngrok_domains()
    if not domains:
        return None
    claimed = {wt.get("ngrok_domain") for wt in registry["worktrees"].values() if wt.get("ngrok_domain")}
    running = _find_running_ngrok_domains()
    return next((d for d in domains if d not in claimed and d not in running), None)


# ── MCP command detection ──────────────────────────────────────────────────────


def _get_mcp_command(server_name: str) -> str | None:
    """Read the command for an MCP server from Claude's config via `claude mcp get`."""
    try:
        result = subprocess.run(["claude", "mcp", "get", server_name], capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if re.match(r"^\s*Command:", line):
                return re.sub(r"^\s*Command:\s*", "", line).strip()
    except FileNotFoundError:
        pass
    return None


# ── Registry ───────────────────────────────────────────────────────────────────


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"version": 1, "worktrees": {}}
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def _save_registry(registry: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


# ── Helpers ────────────────────────────────────────────────────────────────────


def slugify(branch: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")


def _make_template_vars(
    branch: str,
    slug: str,
    compose_project: str,
    ports: dict[str, int],
    ngrok_domain: str | None,
) -> dict[str, str]:
    tvars: dict[str, str] = {
        "branch": branch,
        "slug": slug,
        "compose_project": compose_project,
        "ngrok_domain": ngrok_domain or "",
    }
    for k, v in ports.items():
        tvars[k.lower()] = str(v)
    return tvars


def _read_env(path: Path) -> list[tuple[str, str]]:
    result = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped or "=" not in stripped:
            result.append((line, ""))
        else:
            result.append((line, stripped.split("=", 1)[0].strip()))
    return result


def _write_env(worktree_path: Path, overrides: dict[str, str]) -> None:
    root_env = REPO_ROOT / ".env"
    lines = _read_env(root_env) if root_env.exists() else []

    seen: set[str] = set()
    output = []
    for raw, key in lines:
        if key and key in overrides:
            output.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            output.append(raw)
    for key, value in overrides.items():
        if key not in seen:
            output.append(f"{key}={value}")

    (worktree_path / ".env").write_text("\n".join(output) + "\n")


def _write_ngrok_yml(worktree_path: Path, ngrok_domain: str) -> None:
    base = REPO_ROOT / "ngrok.yml"
    if not base.exists():
        return
    content = re.sub(r"(domain:\s*)\S+", f"\\1{ngrok_domain}", base.read_text())
    (worktree_path / "ngrok.yml").write_text(content)


def _write_claude_settings(
    worktree_path: Path,
    ports: dict[str, int],
    branch: str,
    compose_project_name: str,
    mcp_cmd: str | None,
) -> None:
    claude_dir = worktree_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    existing = claude_dir / "settings.json"

    settings: dict = {}
    if existing.exists():
        with open(existing) as f:
            settings = json.load(f)

    settings.setdefault("env", {})
    settings["env"]["COMPOSE_PROJECT_NAME"] = compose_project_name
    settings["env"]["WORKTREE_BRANCH"] = branch
    for var, port in ports.items():
        settings["env"][var] = str(port)

    mcp_cfg = CONFIG.get("mcp_override")
    if mcp_cfg and mcp_cmd:
        api_port_var = mcp_cfg.get("api_port_var")
        if api_port_var:
            api_port = ports.get(api_port_var, list(ports.values())[0])
        else:
            # Auto-detect: prefer a var with "API" in the name
            api_port = next(
                (v for k, v in ports.items() if "API" in k),
                list(ports.values())[0] if ports else 8000,
            )

        mcp_entry: dict[str, Any] = {"command": mcp_cmd, "env": {}}
        api_url_env = mcp_cfg.get("api_url_env")
        if api_url_env:
            mcp_entry["env"][api_url_env] = f"http://localhost:{api_port}"
        for k, v in (mcp_cfg.get("extra_env") or {}).items():
            mcp_entry["env"][k] = v

        settings.setdefault("mcpServers", {})
        settings["mcpServers"][mcp_cfg["server_name"]] = mcp_entry

    with open(existing, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def _set_terminal_title(title: str) -> None:
    sanitized = re.sub(r"[\x00-\x1f\x7f]", "", title)[:200]
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(sanitized)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        if sys.stdout.isatty():
            sys.stdout.write(f"\033]0;{sanitized}\007")
            sys.stdout.flush()


def _symlink_ui_node_modules(worktree_path: Path) -> None:
    """Symlink worktree ui/node_modules → repo-root ui/node_modules (silently skips if absent)."""
    src = REPO_ROOT / "ui" / "node_modules"
    if not src.exists():
        return

    _ensure_ui_dockerignore(worktree_path)

    dst = worktree_path / "ui" / "node_modules"
    if dst.exists() or dst.is_symlink():
        return

    print("  Linking ui/node_modules...")
    try:
        os.symlink(src, dst, target_is_directory=True)
        print(f"  ui/node_modules: symlinked → {src}")
        return
    except (OSError, NotImplementedError):
        pass

    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            print(f"  ui/node_modules: junction created → {src}")
        else:
            print(
                f"  Warning: could not link ui/node_modules.\n"
                f"    Enable Developer Mode or run as admin, or run:\n"
                f'    cmd /c mklink /J "{dst}" "{src}"'
            )
    else:  # type: ignore[unreachable]
        print(f"  Warning: could not link ui/node_modules. Run: ln -s {src} {dst}")


def _ensure_ui_dockerignore(worktree_path: Path) -> None:
    ui_dir = worktree_path / "ui"
    if not ui_dir.exists():
        return

    dockerignore = ui_dir / ".dockerignore"
    required = {"node_modules", "dist", ".yarn/cache"}
    existing = (
        {line.strip() for line in dockerignore.read_text().splitlines() if line.strip()}
        if dockerignore.exists()
        else set()
    )
    missing = required - existing
    if not missing:
        return

    with dockerignore.open("a") as f:
        for entry in sorted(missing):
            f.write(f"{entry}\n")
    print(f"  ui/.dockerignore: added {', '.join(sorted(missing))}")


# ── Docker helpers ─────────────────────────────────────────────────────────────


def _compose_base(compose_project_name: str, worktree_path: Path) -> list[str]:
    if not COMPOSE_FILE.exists():
        print(f"Error: compose file not found: {COMPOSE_FILE}", file=sys.stderr)
        sys.exit(1)

    cmd = ["docker", "compose", "--file", COMPOSE_FILE.as_posix()]
    env_file = worktree_path / ".env"
    if env_file.exists():
        cmd += ["--env-file", env_file.as_posix()]
    cmd += ["--project-directory", worktree_path.as_posix(), "--project-name", compose_project_name]
    return cmd


def _run_docker(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run docker compose with inherited env stripped of port vars (prevents root .env bleed)."""
    strip = frozenset(PORT_VARS) | {"COMPOSE_PROJECT_NAME"} | set(CONFIG.get("env_overrides") or {})
    env = {k: v for k, v in os.environ.items() if k not in strip}
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if result.returncode != 0 and result.stderr:
        print(result.stderr, file=sys.stderr)
    return result


def _find_db_service() -> str | None:
    keywords = ("db", "postgres", "mysql", "mariadb", "database", "mongo")
    return next((s for s in SERVICES if any(kw in s.lower() for kw in keywords)), None)


def _wait_healthy(compose_project_name: str) -> bool:
    service = CONFIG.get("health_wait_service") or _find_db_service()
    if not service:
        return True

    container = f"{compose_project_name}-{service}-1"
    deadline = time.time() + DB_HEALTH_TIMEOUT
    print(f"  Waiting for {container} to be healthy (up to {DB_HEALTH_TIMEOUT}s)...")
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        status = result.stdout.strip()
        if status == "healthy":
            return True
        if status == "":
            running = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if running.stdout.strip() == "true":
                print(f"  {container} is running (no healthcheck configured).")
                return True
        time.sleep(3)
    return False


# ── Commands ───────────────────────────────────────────────────────────────────


def _cleanup_worktree(branch: str, compose_project_name: str, worktree_path: Path) -> None:
    print("Cleaning up after failure...", file=sys.stderr)
    if worktree_path.exists():
        print(f"  Stopping Docker stack: {compose_project_name}...", file=sys.stderr)
        _run_docker(_compose_base(compose_project_name, worktree_path) + ["down", "--volumes"])
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"  Warning: could not auto-remove {worktree_path}.", file=sys.stderr)
            print("  Remove manually, then run: git worktree prune", file=sys.stderr)
        subprocess.run(["git", "branch", "-D", branch], cwd=REPO_ROOT, capture_output=True)
    registry = _load_registry()
    if branch in registry["worktrees"]:
        del registry["worktrees"][branch]
        _save_registry(registry)
    print("Cleanup complete. Fix the issue above and retry.", file=sys.stderr)


def cmd_slug(branch: str) -> None:
    print(slugify(branch))


def cmd_ensure(branch: str, base: str) -> None:
    registry = _load_registry()
    if branch in registry["worktrees"]:
        print(f"Worktree '{branch}' already exists, skipping creation.")
        _set_terminal_title(f"wt: {branch}")
        return
    cmd_create(branch, base)


def cmd_create(branch: str, base: str) -> None:
    if not PORT_VARS:
        print(
            f"Error: no port variables found in {COMPOSE_FILE}.\n"
            "  Ensure your compose file uses the '${VAR:-default}:container_port' pattern.",
            file=sys.stderr,
        )
        sys.exit(1)

    prefix: str = CONFIG["project_prefix"]
    slug = slugify(branch)
    compose_project_name = f"{prefix}-{slug}"
    worktree_path = WORKTREES_DIR / slug

    registry = _load_registry()

    if branch in registry["worktrees"]:
        print(f"Error: worktree for '{branch}' already exists.", file=sys.stderr)
        print(f"  Run: just worktree-remove '{branch}'  to remove it first.", file=sys.stderr)
        sys.exit(1)

    if worktree_path.exists():
        print(f"Error: {worktree_path} exists but is not in the registry.", file=sys.stderr)
        print(f"  Remove manually: rm -rf {worktree_path}", file=sys.stderr)
        sys.exit(1)

    ports = _allocate_ports(registry)
    if ports is None:
        print(f"Error: no free port group available (all {CONFIG['max_worktrees']} slots in use).", file=sys.stderr)
        sys.exit(1)

    ngrok_domain = _assign_ngrok_domain(registry)
    if ngrok_domain:
        print(f"  ngrok domain assigned: {ngrok_domain}")
    elif CONFIG.get("ngrok_domains_file"):
        print("  ngrok: skipped — no domains available in ngrok_domains.txt")

    if "/" in base:
        remote = base.split("/")[0]
        print(f"Fetching from {remote}...")
        r = subprocess.run(["git", "fetch", remote], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            print(f"Error: git fetch {remote} failed.", file=sys.stderr)
            if r.stderr:
                print(r.stderr.strip(), file=sys.stderr)
            sys.exit(1)

    print(f"Creating git worktree .worktrees/{slug} (branch: {branch}, base: {base})...")
    branch_exists = (
        subprocess.run(
            ["git", "rev-parse", "--verify", branch], capture_output=True, cwd=REPO_ROOT
        ).returncode
        == 0
    )
    git_cmd = (
        ["git", "worktree", "add", str(worktree_path), branch]
        if branch_exists
        else ["git", "worktree", "add", str(worktree_path), "-b", branch, base]
    )
    result = subprocess.run(git_cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        sys.exit(result.returncode)

    _symlink_ui_node_modules(worktree_path)

    # Build env overrides
    tvars = _make_template_vars(branch, slug, compose_project_name, ports, ngrok_domain)
    overrides: dict[str, str] = {
        "COMPOSE_PROJECT_NAME": compose_project_name,
        **{k: str(v) for k, v in ports.items()},
    }
    for key, tmpl in (CONFIG.get("env_overrides") or {}).items():
        try:
            overrides[key] = str(tmpl).format(**tvars)
        except KeyError:
            overrides[key] = str(tmpl)

    print("Writing .env...")
    _write_env(worktree_path, overrides)

    if ngrok_domain:
        print("Writing ngrok.yml...")
        _write_ngrok_yml(worktree_path, ngrok_domain)

    # MCP override
    mcp_cmd: str | None = None
    mcp_cfg = CONFIG.get("mcp_override")
    if mcp_cfg and mcp_cfg.get("server_name"):
        mcp_cmd = _get_mcp_command(mcp_cfg["server_name"])
        if not mcp_cmd:
            print(
                f"  Warning: MCP server '{mcp_cfg['server_name']}' not found in Claude config; "
                "skipping MCP override."
            )

    print("Writing .claude/settings.json...")
    _write_claude_settings(worktree_path, ports, branch, compose_project_name, mcp_cmd)

    # Register before Docker start so ports are reserved even if Docker fails
    registry["worktrees"][branch] = {
        "path": f"{CONFIG['worktrees_dir']}/{slug}",
        "branch_slug": slug,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "compose_project_name": compose_project_name,
        "base_branch": base,
        "ports": ports,
        "ngrok_domain": ngrok_domain,
        "has_ngrok": ngrok_domain is not None,
    }
    _save_registry(registry)

    # Start Docker stack
    services_to_start = [s for s in SERVICES if s != "ngrok" or ngrok_domain is not None]
    print(f"Starting Docker stack: {compose_project_name}...")
    result = _run_docker(
        _compose_base(compose_project_name, worktree_path) + ["up", "-d", "--force-recreate"] + services_to_start
    )
    if result.returncode != 0:
        print("Error: docker compose up failed.", file=sys.stderr)
        _cleanup_worktree(branch, compose_project_name, worktree_path)
        sys.exit(result.returncode)

    if not _wait_healthy(compose_project_name):
        print("Error: database did not become healthy within timeout.", file=sys.stderr)
        _cleanup_worktree(branch, compose_project_name, worktree_path)
        sys.exit(1)

    # Post-start hook
    post_start = CONFIG.get("post_start")
    if post_start and post_start.get("service") and post_start.get("command"):
        svc = post_start["service"]
        cmd_str: str = post_start["command"]
        print(f"Running post-start command ({svc}): {cmd_str}...")
        result = _run_docker(
            _compose_base(compose_project_name, worktree_path) + ["exec", svc] + cmd_str.split()
        )
        if result.returncode != 0:
            print("Error: post-start command failed.", file=sys.stderr)
            _cleanup_worktree(branch, compose_project_name, worktree_path)
            sys.exit(result.returncode)

    _set_terminal_title(f"wt: {branch}")

    print()
    print(f"Worktree ready: {branch}")
    for var, port in ports.items():
        label = var.replace("_HOST_PORT", "").replace("_", " ").title()
        print(f"  {label:<18} localhost:{port}")
    if ngrok_domain:
        print(f"  {'Ngrok':<18} https://{ngrok_domain}")
    print()
    print(f'  Launch Claude:  just claude-worktree "{branch}"')


def cmd_remove(branch: str) -> None:
    registry = _load_registry()
    if branch not in registry["worktrees"]:
        print(f"Error: no worktree registered for '{branch}'.", file=sys.stderr)
        sys.exit(1)

    entry = registry["worktrees"][branch]
    compose_project_name = entry["compose_project_name"]
    worktree_path = REPO_ROOT / entry["path"]

    print(f"Stopping Docker stack: {compose_project_name}...")
    _run_docker(_compose_base(compose_project_name, worktree_path) + ["down", "--volumes"])

    print(f"Removing git worktree: {worktree_path}...")
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=REPO_ROOT)

    del registry["worktrees"][branch]
    _save_registry(registry)
    print(f"Worktree '{branch}' removed.")


def cmd_list() -> None:
    registry = _load_registry()
    if not registry["worktrees"]:
        print("No worktrees registered.")
        return

    for branch, entry in registry["worktrees"].items():
        worktree_path = REPO_ROOT / entry["path"]
        ps = _run_docker(_compose_base(entry["compose_project_name"], worktree_path) + ["ps", "-q"])
        status = "running" if ps.stdout.strip() else "stopped"
        print(f"  {branch}  [{status}]")
        for var, port in entry["ports"].items():
            label = var.replace("_HOST_PORT", "")
            print(f"    {label}: localhost:{port}", end="  |  ")
        print()
        if entry.get("ngrok_domain"):
            print(f"    ngrok: https://{entry['ngrok_domain']}")
        print()


def cmd_status(branch: str | None) -> None:
    registry = _load_registry()
    worktrees = registry["worktrees"]
    targets = {branch: worktrees[branch]} if branch and branch in worktrees else worktrees

    if not targets:
        print("No worktrees registered.")
        return

    for br, entry in targets.items():
        print(f"Branch:   {br}")
        print(f"  Project:  {entry['compose_project_name']}")
        print(f"  Path:     {entry['path']}")
        for var, port in entry["ports"].items():
            label = var.replace("_HOST_PORT", "").replace("_", " ").title()
            print(f"  {label:<16} localhost:{port}")
        if entry.get("ngrok_domain"):
            print(f"  {'Ngrok':<16} https://{entry['ngrok_domain']}")
        print(f"  Created:  {entry['created_at']}")
        print()


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("create", "ensure"):
        p = sub.add_parser(name)
        p.add_argument("branch")
        p.add_argument("base", nargs="?", default="origin/main")

    sub.add_parser("remove").add_argument("branch")
    sub.add_parser("list")
    sub.add_parser("status").add_argument("branch", nargs="?", default=None)
    sub.add_parser("slug").add_argument("branch")

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args.branch, args.base)
    elif args.command == "ensure":
        cmd_ensure(args.branch, args.base)
    elif args.command == "remove":
        cmd_remove(args.branch)
    elif args.command == "list":
        cmd_list()
    elif args.command == "status":
        cmd_status(args.branch or None)
    elif args.command == "slug":
        cmd_slug(args.branch)


if __name__ == "__main__":
    main()
