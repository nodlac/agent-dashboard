# agent-tools

Manage multiple AI coding agents via tmux. Each agent gets its own session, working directory (git worktree for repo agents), and status in a CSV tracker. Supports `claude`, `opencode`, `cursor`, `aider`, `codex`, or any CLI.

## Install

```sh
git clone <this-repo> ~/repos/agent-tools
~/repos/agent-tools/install.sh
source ~/.zshrc
```

Installs:
- `~/.local/bin/agent-dashboard`, `~/.local/bin/agent-update` wrappers
- `source ~/repos/agent-tools/agent-tools.sh` in `~/.zshrc`
- `~/.agents/agents.csv` tracker + `~/.agents/agent-log.md`
- `~/.config/agent-tools/ext.sh` scaffold (edit to integrate with your task tracker)

## Commands

| Command | Purpose |
|---------|---------|
| `agent-start`     | Interactive prompt → spawn new agent in a fresh tmux session |
| `agent-resume`    | Relaunch the AI tool in sessions where it's stopped |
| `agent-dashboard` | TUI for reviewing agent status (alias: `agents`) |
| `agent-update <status> "note"` | Status update — run from inside an agent session. Statuses: `active`, `review`, `testing`, `blocked`, `done` |
| `agent-track <session>` | Start tracking an existing tmux session |
| `agent-checkin`   | Walk through all active agents interactively |
| `agent-serve`     | Add a dev-server pane to an existing repo agent |

## Configuration

All env-overridable in your shell rc or `~/.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_DIR` | `~/.agents` | Data directory |
| `AGENT_FILE` | `$AGENT_DIR/agents.csv` | Tracker CSV |
| `AGENT_LOG` | `$AGENT_DIR/agent-log.md` | Log markdown |
| `AGENT_SESSION_PREFIX` | `z-` | Tmux session prefix |
| `AGENT_TOOL` | `claude` | AI CLI to launch |
| `REPO_DIR` | `~/repos` | Where repos live (parent dir for worktrees) |
| `AGENT_PORT_RANGE_START` / `END` | `9000` / `9010` | Dev-server port pool |
| `AGENT_DEV_CMD` | `npm install && PORT=$PORT npm start` | Dev command for repo agents |
| `AGENT_EXCLUDED` | `^(control-center\|settings\|notes)$` | Regex of tmux sessions the dashboard ignores |
| `AGENT_TOOLS_EXT` | (see below) | Path to extension file |

## Extensions

`agent-tools` looks for an extension file to integrate with a task tracker, notes system, or register custom agent types. Lookup order:

1. `$AGENT_TOOLS_EXT` (if set)
2. `${XDG_CONFIG_HOME:-~/.config}/agent-tools/ext.sh`
3. `<repo>/ext.sh`

See [`ext.example.sh`](ext.example.sh) for the hook surface: `_ext_normalize_task_id`, `_ext_task_lookup`, `_ext_task_create`, `_ext_prompt_extras`, `_ext_notes_ref`, plus custom `agent-type-register` entries.

## Agent types

- **`repo`** — creates a git worktree under `$REPO_DIR/<name>-<slug>` on a new branch, optionally with a dev server on a free port.
- **`general`** — works in any directory.
- **Custom** — register via `agent-type-register <key> <shortcut> <label> <work_dir>`. `{slug}` in `work_dir` is replaced with the sanitized session slug at spawn time.

## Data format

`agents.csv` columns: `Status, Task, TaskID, Session, Notes, Started, Type, Focus`.

Statuses drive dashboard coloring and `agent-resume` behavior. `Focus=1` flags a session for uninterrupted work.

## License

MIT
