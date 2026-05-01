set windows-shell := ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoLogo", "-Command"]
set dotenv-load

call_recipe := just_executable() + " --justfile=" + justfile()
python := if os() == "windows" { "python" } else { "python3" }

default:
    @just --list

# ── Worktree management ────────────────────────────────────────────────────────

# Create a new worktree + isolated Docker stack (branched from <base>)
worktree-create branch base="origin/main":
    {{python}} worktree.py create '{{branch}}' '{{base}}'

# Stop Docker stack and delete worktree
worktree-remove branch:
    {{python}} worktree.py remove '{{branch}}'

# List all worktrees and their running status
worktree-list:
    {{python}} worktree.py list

# Show ports and URLs for one or all worktrees
worktree-status branch="":
    {{python}} worktree.py status '{{branch}}'

# Create worktree (if needed) then launch Claude Code inside it
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
