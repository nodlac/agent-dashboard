# agent-tools

Manage multiple AI coding agents via tmux. Each agent gets its own session, working directory (git worktree for repo agents), and status in a CSV tracker. Supports `claude`, `opencode`, `cursor`, `aider`, `codex`, or any CLI.

## How it works

Each agent is a **tmux session** running an AI CLI (`claude`, `opencode`, etc.) inside a chosen working directory. The session name, task, status, and optional task-tracker ID live in a CSV tracker; the AI tool reads its initial instructions from a generated prompt file.

**Lifecycle of a repo agent:**

1. `agent-start` — interactive prompt collects task, branch, task ID, type. `repo` type creates a git worktree at `$REPO_DIR/<repo>-<slug>` on a new branch.
2. Prompt file is written to `/tmp/agent-prompt-<session>.md` with the task description plus any lines added by the `_ext_prompt_extras` hook (e.g. task-tracker link).
3. A fresh tmux session is created in the worktree; the AI CLI is launched with the prompt piped in. For repo agents, a second pane runs `AGENT_DEV_CMD` on an allocated port.
4. A row is appended to `agents.csv` with status `active`.
5. Inside the session, the agent calls `agent-update <status> "note"` when done / blocked / ready-for-review. That updates both the CSV row and `agent-log.md`.
6. `agent-dashboard` reads the CSV, cross-references live tmux sessions, and presents a TUI for reviewing and switching.
7. `agent-resume` scans for sessions where the AI tool stopped (e.g. shell reboot) and relaunches it using the original prompt file.

**Key design choices:**

- **tmux = isolation.** Each agent has its own session, shell history, and working directory. Quitting the AI CLI doesn't kill the session; `agent-resume` can relaunch later.
- **CSV = source of truth.** Plain-text, human-editable, easy to grep/sed. No daemon, no database.
- **Hooks, not plugins.** The core is ~800 lines of zsh. All tracker/notes integration lives in a single optional `ext.sh` with five override points. Ship your private config separately from the tool.
- **Worktrees over branches.** Repo agents get a full checkout per task so the main working copy isn't disturbed, and multiple agents on the same repo can run in parallel without stomping each other.

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
