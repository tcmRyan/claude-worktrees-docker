# claude-worktrees-docker

Spin up fully isolated Docker Compose stacks for each git branch — with built-in Claude Code integration.

## The problem

Working across multiple branches means port conflicts. Running `docker compose up` for `feature/auth` kills your `main` stack. You either context-switch or run one environment at a time.

## The solution

`claude-worktrees-docker` gives each branch its own git worktree with a dedicated Docker Compose project and non-conflicting port group:

```
main             → project: main          → ports: 8000, 5432, 6379 (your defaults)
feature/auth     → project: wt-feature-auth  → ports: 5750, 5710, 5720
fix/payment-bug  → project: wt-fix-payment   → ports: 5850, 5810, 5820
```

Each worktree is a real git worktree — you can run tests, make commits, and open PRs from any of them independently.

## Claude Code integration

The real power is how this works with Claude Code. When you run:

```
just claude-worktree feature/auth
```

It:
1. Creates the worktree and Docker stack (if they don't exist yet)
2. `cd`s into the worktree directory
3. Injects env vars into `.claude/settings.json` so Claude knows the stack's ports
4. Sets the terminal title to `wt: feature/auth`
5. Launches `claude`

Claude Code running inside a worktree automatically knows the layout of that branch's stack — which port the API is on, which port Postgres is on, etc. If you run `just claude-worktree` in another terminal for a different branch, that Claude session sees a completely separate stack.

### MCP server override

If you have an MCP server that calls your application's API, configure `mcp_override` in `worktree-config.yml`:

```yaml
mcp_override:
  server_name: my-api-tool
  api_url_env: API_BASE_URL
```

When the worktree is created, the script reads `claude mcp get my-api-tool` to find the server binary, then writes a `.claude/settings.json` in the worktree that overrides `API_BASE_URL` to `http://localhost:<this-worktree's-api-port>`.

Result: Claude's MCP tools automatically target the right local stack for each branch, with zero manual configuration.

## Prerequisites

- **Python 3.10+** and PyYAML (`pip install pyyaml`)
- **Docker** with Docker Compose v2 (`docker compose`, not `docker-compose`)
- **[just](https://just.systems/)** command runner
- **Git**
- **[Claude Code CLI](https://claude.ai/code)** — only needed for `just claude-worktree`

## Setup

### Option A: Add to an existing project (recommended)

1. Copy `worktree.py` into your project (anywhere is fine, e.g. `scripts/worktree.py`)
2. Copy `worktree-config.example.yml` → `worktree-config.yml` in your repo root and customize
3. Add the [just recipes](#just-recipes) to your existing `justfile`
4. Update paths in the recipes if you placed `worktree.py` in a subdirectory
5. Add to your `.gitignore`:
   ```
   .worktrees/
   .worktree-registry.json
   ngrok_domains.txt
   ```

### Option B: Use this repo as a base

```bash
git clone https://github.com/yourname/claude-worktrees-docker my-project
cd my-project
cp worktree-config.yml worktree-config.yml
# Edit worktree-config.yml, add your docker-compose.yml
pip install pyyaml
just worktree-create feature/my-branch
```

## Docker Compose requirements

Port-mapped services must use environment variable substitution for the **host** port:

```yaml
services:
  api:
    ports:
      - "${API_HOST_PORT:-8000}:8000"   # ✅ variable host port
      # - "8000:8000"                   # ✗ hardcoded — will conflict between worktrees

  db:
    ports:
      - "${DB_HOST_PORT:-5432}:5432"    # ✅

  redis:
    ports:
      - "${REDIS_HOST_PORT:-6379}:6379" # ✅
```

The tool parses your `docker-compose.yml`, finds all `${VAR:-default}` port variables, and automatically allocates unique values for each worktree. See `docker-compose.example.yml` for a complete working example.

## Configuration

Copy `worktree-config.example.yml` to `worktree-config.yml` and customize:

| Key | Default | Description |
|-----|---------|-------------|
| `compose_file` | `docker-compose.yml` | Path to your Docker Compose file |
| `worktrees_dir` | `.worktrees` | Directory where git worktrees are created |
| `registry_file` | `.worktree-registry.json` | Tracks active worktrees and port assignments |
| `port_base` | `5710` | Starting port for the first worktree group |
| `port_step` | `100` | Gap between port groups (must be ≥ number of port-mapped services) |
| `max_worktrees` | `10` | Maximum concurrent worktrees |
| `project_prefix` | `wt` | Docker Compose project name prefix |
| `exclude_services` | `[]` | Services to skip in worktree stacks |
| `health_wait_service` | *(auto)* | Service to wait for before running `post_start` |
| `post_start` | *(none)* | Command to run in the stack after it's healthy |
| `env_overrides` | `{}` | Additional env vars to write into each worktree's `.env` |
| `ngrok_domains_file` | *(none)* | Path to a file with reserved ngrok domains |
| `mcp_override` | *(none)* | Claude Code MCP server configuration |

### `post_start` — database migrations

```yaml
post_start:
  service: api
  command: "flask db upgrade"
```

Runs `docker compose exec api flask db upgrade` after the stack is healthy. Works with any migration tool:

| Tool | command |
|------|---------|
| Django | `python manage.py migrate` |
| Alembic (uv) | `uv run alembic upgrade head` |
| Prisma | `npx prisma migrate deploy` |
| Rails | `bundle exec rails db:migrate` |
| Flyway | `flyway migrate` |

### `env_overrides` — extra environment variables

```yaml
env_overrides:
  MY_API_URL: "http://localhost:{api_host_port}"
  FRONTEND_URL: "http://localhost:{ui_host_port}"
  SERVER_HOST: "https://{ngrok_domain}"
```

Template variables: `{branch}`, `{slug}`, `{compose_project}`, `{ngrok_domain}`, and any port variable name lowercased (e.g. `{api_host_port}`, `{db_host_port}`).

### `mcp_override` — Claude Code MCP integration

```yaml
mcp_override:
  server_name: my-mcp-tool       # name as registered with `claude mcp add`
  api_url_env: MY_API_BASE_URL   # env var the server reads for the API URL
  api_port_var: API_HOST_PORT    # which port var to use (auto-detected if omitted)
  extra_env:                     # additional static env vars for the server process
    LOG_LEVEL: debug
```

The MCP server must already be registered in your Claude config (`claude mcp add ...`) before creating a worktree. The script reads the registered command with `claude mcp get <server_name>` and reuses the same binary, only changing where it points.

### ngrok integration (optional)

Assign a dedicated HTTPS tunnel to each worktree — useful for services requiring webhook callbacks.

Requirements: ngrok Pro/Business plan (reserved domains), `NGROK_AUTHTOKEN` in your `.env`.

1. Create `ngrok_domains.txt` (one domain per line; see `ngrok_domains.example.txt`)
2. Add a `ngrok` service to your `docker-compose.yml` (see the commented block in `docker-compose.example.yml`)
3. Add `ngrok` to `exclude_services` (the tool starts it only when a domain is available)
4. Configure:
   ```yaml
   ngrok_domains_file: ngrok_domains.txt
   exclude_services:
     - ngrok
   env_overrides:
     SERVER_HOST: "https://{ngrok_domain}"
   ```

## Commands

```bash
# Worktree management
just worktree-create <branch> [base]   # Create worktree + Docker stack (default base: origin/main)
just worktree-remove <branch>          # Stop stack, remove volumes, delete worktree
just worktree-list                     # List all worktrees with running status
just worktree-status [branch]          # Show ports and URLs (omit branch for all)

# Claude Code
just claude-worktree <branch> [base]   # Create if needed, then launch Claude inside the worktree
```

You can also call the script directly:

```bash
python3 worktree.py create feature/my-branch
python3 worktree.py create feature/my-branch upstream/develop
python3 worktree.py remove feature/my-branch
python3 worktree.py list
python3 worktree.py status
python3 worktree.py status feature/my-branch
python3 worktree.py slug "feature/my branch"   # prints: feature-my-branch
```

## Just recipes

If you're adding this to an existing `justfile`, here are the recipes to copy. Adjust the Python path if `worktree.py` is in a subdirectory.

```just
# At the top of your justfile (skip if already present):
set windows-shell := ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoLogo", "-Command"]
call_recipe := just_executable() + " --justfile=" + justfile()
python := if os() == "windows" { "python" } else { "python3" }

worktree-create branch base="origin/main":
    {{python}} worktree.py create '{{branch}}' '{{base}}'

worktree-remove branch:
    {{python}} worktree.py remove '{{branch}}'

worktree-list:
    {{python}} worktree.py list

worktree-status branch="":
    {{python}} worktree.py status '{{branch}}'

claude-worktree branch base="origin/main":
    {{call_recipe}} claude-worktree-{{os()}} '{{branch}}' '{{base}}'

claude-worktree-linux branch base="origin/main":
    {{python}} worktree.py ensure '{{branch}}' '{{base}}'
    cd ".worktrees/$({{python}} worktree.py slug '{{branch}}')" && printf '\033]0;wt: {{branch}}\007' && claude; printf '\033]0;wt: {{branch}}\007'

claude-worktree-macos branch base="origin/main":
    {{python}} worktree.py ensure '{{branch}}' '{{base}}'
    cd ".worktrees/$({{python}} worktree.py slug '{{branch}}')" && printf '\033]0;wt: {{branch}}\007' && claude; printf '\033]0;wt: {{branch}}\007'

claude-worktree-windows branch base="origin/main":
    @{{python}} worktree.py ensure '{{branch}}' '{{base}}'
    @$slug = ({{python}} worktree.py slug '{{branch}}').Trim(); Set-Location (Join-Path '.worktrees' $slug); $host.UI.RawUI.WindowTitle = "wt: {{branch}}"; claude; $host.UI.RawUI.WindowTitle = "wt: {{branch}}"
```

## How it works

1. **Config loading** — reads `worktree-config.yml` from the repo root
2. **Compose parsing** — scans `docker-compose.yml` for services and `${VAR:-N}` host port variables
3. **Port allocation** — finds the next free group of ports not used by any other worktree or process
4. **Git worktree** — creates `.worktrees/<slug>/` as a real git worktree on the target branch
5. **`.env` generation** — copies your root `.env`, overrides port variables + any `env_overrides`
6. **`.claude/settings.json`** — writes `COMPOSE_PROJECT_NAME`, all port vars, and optionally the MCP server override
7. **Docker stack** — runs `docker compose up -d` with `--project-name` and `--env-file` targeting the worktree
8. **Health wait** — polls the database container until healthy
9. **Post-start hook** — runs your configured command (migrations, seeds, etc.) inside the stack
10. **Registry** — records the worktree in `.worktree-registry.json` for future `list`/`status`/`remove`

## Troubleshooting

**"No port variables found"**
Your `docker-compose.yml` doesn't use `${VAR:-default}:container_port` for port mappings. Update your ports to use this pattern (see `docker-compose.example.yml`).

**"No free port group available"**
All `max_worktrees` slots are in use. Run `just worktree-list` to identify stale worktrees and remove them with `just worktree-remove <branch>`.

**Database never becomes healthy**
Check `docker logs <compose_project>-db-1`. Common causes: port already in use, insufficient Docker memory, or the healthcheck command is wrong for your database image. Increase `DB_HEALTH_TIMEOUT` in `worktree.py` (line near the top) if your database just starts slowly.

**MCP override not applied**
Verify the server is registered: `claude mcp get <server_name>` should print a `Command:` line. The server must be registered before creating the worktree.

**Windows: `ui/node_modules` symlink fails**
Enable [Developer Mode](https://learn.microsoft.com/en-us/windows/apps/get-started/enable-your-device-for-development) in Windows Settings, or run just with administrator privileges.

**Port conflicts between worktrees**
Each port group is checked for availability before allocation. If a group appears free but you still get conflicts, another process may be using those ports. Inspect with `netstat -an | grep <port>` and adjust `port_base` in your config.
