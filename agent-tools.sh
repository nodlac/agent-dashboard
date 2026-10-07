#!/usr/bin/env zsh
# agent-tools.sh — Manage multiple AI coding agents via tmux
# Works with: claude, opencode, cursor, aider, codex, or any CLI tool
# Optional extensions: source agent-tools-ext.sh for project tracker integration

[[ -f ~/.env ]] && source ~/.env

# ── Self-path (resolved at source time) ──────────────────────────────────
AGENT_TOOLS_SCRIPT="${AGENT_TOOLS_SCRIPT:-${(%):-%N}}"
AGENT_TOOLS_SCRIPT="${AGENT_TOOLS_SCRIPT:A}"
AGENT_TOOLS_DIR="${AGENT_TOOLS_SCRIPT:h}"
export AGENT_TOOLS_SCRIPT AGENT_TOOLS_DIR

_agent_tools_hash() {
    if command -v md5 &>/dev/null; then
        md5 -q "$AGENT_TOOLS_SCRIPT" 2>/dev/null
    else
        md5sum "$AGENT_TOOLS_SCRIPT" 2>/dev/null | awk '{print $1}'
    fi
}

# ── Auto-reload ──────────────────────────────────────────────────────────
_agent_tools_reload() {
    local current_hash=$(_agent_tools_hash)
    if [[ -n "$_AGENT_TOOLS_HASH" && "$current_hash" != "$_AGENT_TOOLS_HASH" ]]; then
        _AGENT_TOOLS_HASH="$current_hash"
        source "$AGENT_TOOLS_SCRIPT"
        return 1
    fi
    _AGENT_TOOLS_HASH="$current_hash"
    return 0
}
[[ -z "$_AGENT_TOOLS_HASH" ]] && _AGENT_TOOLS_HASH=$(_agent_tools_hash)

# ── Config (all env-overridable) ─────────────────────────────────────────
AGENT_DIR="${AGENT_DIR:-$HOME/.agents}"
AGENT_FILE="${AGENT_FILE:-$AGENT_DIR/agents.csv}"
AGENT_LOG="${AGENT_LOG:-$AGENT_DIR/agent-log.md}"
EXCLUDED_SESSIONS="${EXCLUDED_SESSIONS:-^(control-center|settings|notes)$}"
REPO_DIR="${REPO_DIR:-$HOME/repos}"
AGENT_SESSION_PREFIX="${AGENT_SESSION_PREFIX:-z-}"
AGENT_TOOL="${AGENT_TOOL:-claude}"
AGENT_EDITOR="${AGENT_EDITOR:-${EDITOR:-nvim}}"
AGENT_PORT_RANGE_START="${AGENT_PORT_RANGE_START:-9000}"
AGENT_PORT_RANGE_END="${AGENT_PORT_RANGE_END:-9010}"
AGENT_DEV_CMD="${AGENT_DEV_CMD:-npm install && PORT=\$PORT npm start}"

# Per-repo auto dev-server command. Echo a command (with a literal $PORT
# placeholder, substituted later) to auto-launch a dev-server pane for that
# repo; echo nothing to skip auto-run. Only repos listed here auto-run — every
# other repo gets NO dev pane (their dev commands differ and aren't wired yet).
_agent_repo_dev_cmd() {
    case "$1" in
        www.vidangel.com)
            # Vite ignores the PORT env var, so pass the allocated port via --port.
            echo 'npm install && npm run serve -- --port $PORT --host' ;;
        *) ;;  # no auto dev server yet
    esac
}

# True when a repo is primarily HTML/CSS (static sites, email/landing
# templates). Such repos get the frontend-design skill wired into the agent
# prompt. Markup+styles must be present and at least match app-code volume
# (js/ts/jsx/tsx/vue/svelte), so framework apps with a stray .css don't match.
_agent_repo_is_frontend() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    local counts
    counts=$(git -C "$dir" ls-files 2>/dev/null | awk '
        /\.(html?|css|scss|sass|less|styl)$/           { fe++ }
        /\.(js|jsx|ts|tsx|vue|svelte|mjs|cjs|astro)$/  { app++ }
        END { print (fe+0), (app+0) }')
    local fe=${counts%% *} app=${counts##* }
    [[ "${fe:-0}" -gt 0 && "${fe:-0}" -ge "${app:-0}" ]]
}

# Map a working dir (e.g. a worktree at $REPO_DIR/<repo>-<slug>) back to its
# repo name. Name-matching $REPO_DIR/* can't work: worktrees live there too and
# match themselves, so ask git for the main checkout instead.
_agent_repo_from_dir() {
    local common
    common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return
    basename "${common:h}"
}

# ── Stub hooks (defaults, overwritten by ext if present) ─────────────────
_ext_normalize_task_id() { echo "$1"; }
_ext_task_lookup()       { return 1; }
_ext_task_create()       { echo "No task tracker configured."; return 1; }
_ext_prompt_extras()     { :; }
_ext_notes_ref()         { echo ""; }

# ── Custom agent types ───────────────────────────────────────────────────
# Extensions register types via: agent-type-register <key> <shortcut> <label> <work_dir>
# Example: agent-type-register analytics a "analytics" "$HOME/Reporting"
typeset -gA _AGENT_TYPES      # key → shortcut:label:dir
_AGENT_TYPE_ORDER=()           # display order

agent-type-register() {
    local key="$1" shortcut="$2" label="$3" dir="$4"
    _AGENT_TYPES[$key]="${shortcut}:${label}:${dir}"
    _AGENT_TYPE_ORDER+=("$key")
}

# Built-in types
agent-type-register repo r "repo" ""  # repo has special handling, dir set by worktree
agent-type-register general g "general" ""  # dir set by template
agent-type-register multi m "multi-repo" "$REPO_DIR/{slug}"  # one worktree per repo inside

# ── Load extensions (overwrites stubs, registers custom types) ───────────
# Lookup order: $AGENT_TOOLS_EXT → $XDG_CONFIG_HOME/agent-tools/ext.sh
#   → ~/.config/agent-tools/ext.sh → $AGENT_TOOLS_DIR/ext.sh
_agent_tools_ext_path() {
    [[ -n "$AGENT_TOOLS_EXT" && -f "$AGENT_TOOLS_EXT" ]] && { echo "$AGENT_TOOLS_EXT"; return; }
    local xdg="${XDG_CONFIG_HOME:-$HOME/.config}/agent-tools/ext.sh"
    [[ -f "$xdg" ]] && { echo "$xdg"; return; }
    [[ -f "$AGENT_TOOLS_DIR/ext.sh" ]] && { echo "$AGENT_TOOLS_DIR/ext.sh"; return; }
}
_ext_file=$(_agent_tools_ext_path)
[[ -n "$_ext_file" ]] && source "$_ext_file"
unset _ext_file

# ── Tool config ──────────────────────────────────────────────────────────
# Stable Claude conversation id per tmux session, so a restart resumes that
# agent's own conversation — --continue can't tell apart agents sharing a cwd.
_agent_claude_id() {
    python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, "agent-tools:" + sys.argv[1]))' "$1"
}

# _agent_tool_cmd <tool> <prompt_file> [session] [continue_dir]
# continue_dir: only pass a dir unique to this agent (a worktree); agents
# launched before ids existed fall back to --continue there.
_agent_tool_cmd() {
    local tool="$1" prompt_file="$2" session="$3" continue_dir="$4"
    case "$tool" in
        claude*)
            local id="" prior=()
            [[ -n "$session" ]] && id=$(_agent_claude_id "$session")
            [[ -n "$id" ]] && prior=(~/.claude/projects/*/"$id".jsonl(N))
            local legacy=()
            [[ -n "$continue_dir" ]] && legacy=(~/.claude/projects/"${continue_dir//[^a-zA-Z0-9]/-}"/*.jsonl(N))
            if (( $#prior )); then
                echo "$tool --resume $id"
            elif (( $#legacy )); then
                echo "$tool --continue"
            elif [[ -n "$id" ]]; then
                echo "$tool --session-id $id \"\$(cat ${prompt_file})\""
            else
                echo "$tool \"\$(cat ${prompt_file})\""
            fi ;;
        opencode) echo "opencode --prompt \"\$(cat ${prompt_file})\"" ;;
        cursor)   echo "cursor --cli --prompt \"\$(cat ${prompt_file})\"" ;;
        aider)    echo "aider --message \"\$(cat ${prompt_file})\"" ;;
        codex)    echo "codex \"\$(cat ${prompt_file})\"" ;;
        *)        echo "$tool \"\$(cat ${prompt_file})\"" ;;
    esac
}

_agent_tool_processes() {
    # Claude Code retitles its process to its version (e.g. 2.1.286).
    echo "claude|opencode|cursor|aider|codex|[0-9]+\\.[0-9]+\\.[0-9]+"
}

# ── CSV helpers ──────────────────────────────────────────────────────────
# Ensure data dir + agents.csv exist
mkdir -p "$AGENT_DIR" 2>/dev/null
if [[ ! -f "$AGENT_FILE" ]]; then
    echo "Status,Task,TaskID,Session,Notes,Started,Type,Focus,Hackathon" > "$AGENT_FILE"
elif ! head -1 "$AGENT_FILE" | grep -q "^Status,"; then
    echo "Status,Task,TaskID,Session,Notes,Started,Type,Focus,Hackathon" | cat - "$AGENT_FILE" > /tmp/_agents_fix.csv && mv /tmp/_agents_fix.csv "$AGENT_FILE"
fi

_sanitize_session() {
    # Truncate to 60 chars so derived paths (e.g. /tmp/agent-prompt-<sess>.md)
    # stay well under the 255-byte filename limit.
    echo "$1" | tr ' ' '-' | tr '[:upper:]' '[:lower:]' \
        | sed 's/[^a-z0-9_-]/-/g' | sed 's/--*/-/g' | sed 's/-$//' \
        | cut -c1-60 | sed 's/-$//'
}

_agent_append_row() {
    python3 -c "
import csv, sys
row = sys.argv[1:]
path = '$AGENT_FILE'
with open(path, 'a', newline='') as f:
    csv.writer(f).writerow(row)
" "$@"
}

_agent_has_session() {
    python3 -c "
import csv, sys
session = sys.argv[1]
with open('$AGENT_FILE') as f:
    for row in csv.reader(f):
        if len(row) > 3 and row[3] == session:
            sys.exit(0)
sys.exit(1)
" "$1"
}

_agent_field() {
    python3 -c "
import csv, sys
for row in csv.reader(open(sys.argv[1])):
    if len(row) > int(sys.argv[3]) and row[3] == sys.argv[2]:
        print(row[int(sys.argv[3])]); break
" "$AGENT_FILE" "$1" "$2" 2>/dev/null
}

_agent_update_field() {
    local session="$1" field_idx="$2" new_val="$3"
    python3 -c "
import csv, sys
session, idx, val = sys.argv[1], int(sys.argv[2]), sys.argv[3]
rows = []
with open('$AGENT_FILE') as f:
    rows = list(csv.reader(f))
for row in rows:
    if len(row) > 3 and row[3] == session:
        row[idx] = val
with open('$AGENT_FILE', 'w', newline='') as f:
    csv.writer(f).writerows(rows)
" "$session" "$field_idx" "$new_val"
}

_allocate_port() {
    python3 -c "
import subprocess, re
used = set()
try:
    out = subprocess.check_output(
        ['lsof', '-iTCP:${AGENT_PORT_RANGE_START}-${AGENT_PORT_RANGE_END}', '-sTCP:LISTEN', '-nP'],
        stderr=subprocess.DEVNULL, text=True)
    for line in out.splitlines():
        m = re.search(r':(\d+)\s', line)
        if m:
            used.add(int(m.group(1)))
except Exception:
    pass
for p in range(${AGENT_PORT_RANGE_START}, ${AGENT_PORT_RANGE_END} + 1):
    if p not in used:
        print(p)
        break
" 2>/dev/null
}

# ── agent-start ──────────────────────────────────────────────────────────
# Usage: agent-start [--task ID] [--type repo|general] [--tool claude|...] [--dir path]
agent-start() {
    _agent_tools_reload || { agent-start "$@"; return; }

    # Parse flags
    local _flag_task="" _flag_type="" _flag_tool="" _flag_dir="" _flag_notes_file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --task) _flag_task="$2"; shift 2 ;;
            --type) _flag_type="$2"; shift 2 ;;
            --tool) _flag_tool="$2"; shift 2 ;;
            --dir)  _flag_dir="$2"; shift 2 ;;
            --notes-file) _flag_notes_file="$2"; shift 2 ;;
            *)      shift ;;
        esac
    done
    local tool="${_flag_tool:-$AGENT_TOOL}"

    # ── Task ID ──────────────────────────────────────────────────────────
    local task_id="" cu_name=""
    if [[ -n "$_flag_task" ]]; then
        task_id="$_flag_task"
        echo "Task ID: $task_id"
    else
        printf "Task ID (enter to skip, 'new' to create): "
        read -r task_id
    fi

    # Normalize via extension (e.g. bare number → TECH-XXXX)
    [[ -n "$task_id" && "$task_id" != "new" ]] && task_id=$(_ext_normalize_task_id "$task_id")

    # ── Existing-session short-circuit ───────────────────────────────────
    # Task already has an agent → open it, no questions. Old/auto-tracked rows
    # often lack TaskID, so fall back to the ClickUp title slug (how sessions
    # get named), then to the task id appearing in the session name.
    if [[ -n "$task_id" && "$task_id" != "new" ]]; then
        _ext_task_lookup "$task_id" >/dev/null 2>&1
        local _slug="" _sess=""
        [[ -n "$cu_name" ]] && _slug=$(_sanitize_session "$cu_name")
        _sess=$(python3 -c "
import csv, sys
tid, slug = sys.argv[2], sys.argv[3]
r = csv.reader(open(sys.argv[1])); next(r, None)
live = set(sys.argv[4].split())
rows = [row for row in r if len(row) > 3 and row[0] and (row[0] != 'done' or row[3] in live)]
hit = next((row for row in rows if row[2] == tid), None) or \\
      next((row for row in rows if slug and (row[3] == slug or row[3].endswith('-' + slug))), None)
if hit: print(hit[3])
" "$AGENT_FILE" "$task_id" "$_slug" "$(command tmux ls -F '#S' 2>/dev/null)" 2>/dev/null)
        if [[ -z "$_sess" ]]; then
            local _pats=(-e "$task_id"); [[ -n "$_slug" ]] && _pats+=(-e "-$_slug")
            _sess=$(command tmux ls -F '#S' 2>/dev/null | grep -i -F "${_pats[@]}" | head -1)
            [[ -n "$_sess" ]] && ! _agent_has_session "$_sess" && \
                _agent_append_row "active" "${cu_name:-$task_id}" "$task_id" "$_sess" "$(_ext_notes_ref)" "$(date +%Y-%m-%d)" "" ""
        fi
        # Session names drift from ClickUp titles; let the user link one by
        # hand. Picked session gets TaskID stamped, so next time it auto-matches.
        if [[ -z "$_sess" ]] && command -v fzf >/dev/null; then
            _sess=$( { echo "+ new agent"; command tmux ls -F '#S' 2>/dev/null; } | \
                fzf --prompt='attach> ' --header="No agent for $task_id${cu_name:+ — $cu_name}. Pick one or create new.") || return 0
            [[ "$_sess" == "+ new agent" ]] && _sess=""
            [[ -n "$_sess" ]] && ! _agent_has_session "$_sess" && \
                _agent_append_row "active" "${cu_name:-$task_id}" "$task_id" "$_sess" "$(_ext_notes_ref)" "$(date +%Y-%m-%d)" "" ""
        fi
        if [[ -n "$_sess" ]]; then
            _agent_update_field "$_sess" 2 "$task_id"
            if command tmux has-session -t "$_sess" 2>/dev/null; then
                echo "Opening existing agent '$_sess'"
                command tmux switch-client -t "$_sess" 2>/dev/null || command tmux attach -t "$_sess"
            else
                agent-restart "$_sess"
            fi
            return 0
        fi
    fi

    # Look up via extension
    if [[ -n "$task_id" && "$task_id" != "new" ]]; then
        if [[ -n "$cu_name" ]] || _ext_task_lookup "$task_id"; then
            echo "  Found: $cu_name"
        else
            echo "  Task $task_id not found in tracker."
        fi
    fi

    # ── Type ─────────────────────────────────────────────────────────────
    local agent_type="" repo_name="" branch_type_input=""
    if [[ -n "$_flag_type" ]]; then
        agent_type="$_flag_type"
    else
        # Build prompt from registered types
        printf "Type — "
        local _first=true
        for _tk in "${_AGENT_TYPE_ORDER[@]}"; do
            local _tv="${_AGENT_TYPES[$_tk]}"
            local _sc="${_tv%%:*}"
            $_first || printf "  "
            printf "[%s]%s" "$_sc" "${_tk#${_sc}}"
            _first=false
        done
        printf ": "
        read -r agent_type
    fi
    # Resolve shortcut or name to registered type key
    local _resolved=""
    for _tk in "${_AGENT_TYPE_ORDER[@]}"; do
        local _tv="${_AGENT_TYPES[$_tk]}"
        local _sc="${_tv%%:*}"
        if [[ "$agent_type" == "$_sc" || "$agent_type" == "$_tk" ]]; then
            _resolved="$_tk"
            break
        fi
    done
    [[ -n "$_resolved" ]] && agent_type="$_resolved"
    [[ -z "$agent_type" ]] && agent_type="general"

    # ── Repo selection (if type=repo) ────────────────────────────────────
    if [[ "$agent_type" == "repo" ]]; then
        local repos=()
        for d in "$REPO_DIR"/*/; do
            [[ -d "$d/.git" ]] || continue
            repos+=("$(basename "$d")")
        done

        if [[ ${#repos} -eq 0 ]]; then
            echo "No git repos found in $REPO_DIR"
            return 1
        fi

        echo ""
        local idx=1
        for r in "${repos[@]}"; do
            printf "  %2d) %s\n" "$idx" "$r"
            ((idx++))
        done
        echo ""
        printf "Repo (number or name): "
        read -r repo_pick

        if [[ "$repo_pick" =~ ^[0-9]+$ ]] && (( repo_pick >= 1 && repo_pick <= ${#repos} )); then
            repo_name="${repos[$repo_pick]}"
        else
            repo_name="$repo_pick"
        fi

        if [[ ! -d "$REPO_DIR/$repo_name/.git" ]]; then
            echo "Not a git repo: $REPO_DIR/$repo_name"
            return 1
        fi

        printf "Branch type — [f]eature  [c]hore  [b]ug: "
        read -r branch_type_input
        case "$branch_type_input" in
            f|feature) branch_type_input="feature" ;;
            c|chore)   branch_type_input="chore" ;;
            b|bug)     branch_type_input="bug" ;;
            *)
                echo "Invalid branch type."
                return 1
                ;;
        esac
    fi

    # ── Compute defaults ─────────────────────────────────────────────────
    local default_session=""
    local default_prompt="${cu_name}"
    if [[ -n "$cu_name" ]]; then
        default_session=$(_sanitize_session "$cu_name")
    fi
    if [[ -n "$_flag_notes_file" && -s "$_flag_notes_file" ]]; then
        default_prompt="${default_prompt}

Notes from sprint tracker:
$(cat "$_flag_notes_file")"
        rm -f "$_flag_notes_file"
    fi

    # ── Generate template ────────────────────────────────────────────────
    local tmpfile="/tmp/agent-start-$$.yaml"

    if [[ "$agent_type" == "repo" ]]; then
        cat > "$tmpfile" <<TMPL
# Agent Setup — :wq to launch, :cq to cancel
# Everything below "--- prompt ---" is the agent prompt (freeform).

branch: new
branch_type: ${branch_type_input}
task_id: ${task_id}
title: ${default_session}

--- prompt ---
${default_prompt}
TMPL
    else
        cat > "$tmpfile" <<TMPL
# Agent Setup — :wq to launch, :cq to cancel
# Everything below "--- prompt ---" is the agent prompt (freeform).

task_id: ${task_id}
title: ${default_session}
dir: ${_flag_dir:-$(pwd)}

--- prompt ---
${default_prompt}
TMPL
    fi

    # ── Open editor ──────────────────────────────────────────────────────
    $AGENT_EDITOR -c 'set ft=yaml' "$tmpfile"
    if [[ $? -ne 0 ]]; then
        rm -f "$tmpfile"
        echo "Cancelled."
        return 0
    fi

    # ── Parse template ───────────────────────────────────────────────────
    eval "$(python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
prompt_lines = []
in_prompt = False
for line in lines:
    stripped = line.strip()
    if stripped == '--- prompt ---':
        in_prompt = True
        continue
    if in_prompt:
        prompt_lines.append(line.rstrip())
        continue
    if not stripped or stripped.startswith('#'):
        continue
    key, _, val = stripped.partition(':')
    key = key.strip()
    val = val.split('#')[0].strip()
    safe = val.replace(\"'\", \"'\\\"'\\\"'\")
    print(f\"local P_{key}='{safe}'\")
while prompt_lines and not prompt_lines[0].strip():
    prompt_lines.pop(0)
while prompt_lines and not prompt_lines[-1].strip():
    prompt_lines.pop()
prompt_text = chr(10).join(prompt_lines)
safe = prompt_text.replace(\"'\", \"'\\\"'\\\"'\")
print(f\"local P_prompt='{safe}'\")
" "$tmpfile")"

    # ── Validate ─────────────────────────────────────────────────────────
    if [[ -z "$P_title" ]]; then
        echo "Title is required. Template saved: $tmpfile"
        return 1
    fi
    if [[ -z "$P_prompt" ]]; then
        echo "Prompt is required. Template saved: $tmpfile"
        return 1
    fi
    rm -f "$tmpfile"

    local session_slug=$(_sanitize_session "$P_title")

    # ── Check for duplicate task_id ──────────────────────────────────────
    local task_id="$P_task_id"
    [[ "$task_id" == "None" ]] && task_id=""

    # ── Create task via extension ────────────────────────────────────────
    if [[ "$task_id" == "new" ]]; then
        if _ext_task_create "${P_title:0:80}"; then
            : # task_id set by extension
        else
            task_id=""
        fi
    fi

    # ── Type dispatch: set work_dir ──────────────────────────────────────
    local work_dir="" type_label="$agent_type"

    case "$agent_type" in
        repo)
            local repo_path="$REPO_DIR/$repo_name"
            type_label="repo:${repo_name}"
            local worktree_path="${repo_path}-${session_slug}"

            if [[ "$P_branch" == "new" ]]; then
                local branch_type="$P_branch_type"
                case "$branch_type" in
                    feature|chore|bug) ;;
                    f) branch_type="feature" ;;
                    c) branch_type="chore" ;;
                    b) branch_type="bug" ;;
                    *) echo "branch_type required."; return 1 ;;
                esac

                local branch_name="${branch_type}/${session_slug}"
                [[ -n "$task_id" ]] && branch_name="${branch_type}/${session_slug}/${task_id}"

                local default_branch=$(git -C "$repo_path" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
                [[ -z "$default_branch" ]] && default_branch="main"

                echo "Fetching latest ${default_branch}..."
                git -C "$repo_path" fetch origin "$default_branch" 2>/dev/null

                # Reuse existing worktree if branch already checked out
                local existing_wt=$(git -C "$repo_path" worktree list --porcelain 2>/dev/null | \
                    awk -v branch="$branch_name" '/^worktree /{wt=$2} /^branch refs\/heads\//{if($2=="refs/heads/"branch) print wt}')

                if [[ -n "$existing_wt" && -d "$existing_wt" ]]; then
                    echo "Reusing existing worktree at $existing_wt"
                    worktree_path="$existing_wt"
                elif [[ -d "$worktree_path/.git" ]]; then
                    echo "Worktree exists at $worktree_path, reusing."
                else
                    git -C "$repo_path" worktree add "$worktree_path" -B "$branch_name" "origin/${default_branch}" 2>/dev/null
                    if [[ $? -ne 0 ]]; then
                        echo "Failed to create worktree for branch '$branch_name'."
                        return 1
                    fi
                fi

                work_dir="$worktree_path"
                echo "Branch: $branch_name"
            else
                local branch_name="$P_branch"
                echo "Fetching ${branch_name}..."
                git -C "$repo_path" fetch origin "$branch_name" 2>/dev/null
                git -C "$repo_path" worktree add "$worktree_path" "$branch_name" 2>/dev/null
                if [[ $? -ne 0 ]]; then
                    git -C "$repo_path" worktree add "$worktree_path" -b "$branch_name" "origin/${branch_name}" 2>/dev/null
                fi
                if [[ $? -ne 0 ]]; then
                    echo "Failed to create worktree for branch '$branch_name'."
                    return 1
                fi
                work_dir="$worktree_path"
                echo "Branch: $branch_name"
            fi
            ;;
        *)
            # Check registered types for a custom work dir
            if [[ -n "${_AGENT_TYPES[$agent_type]}" ]]; then
                local _tv="${_AGENT_TYPES[$agent_type]}"
                local _dir="${_tv##*:}"
                if [[ -n "$_dir" ]]; then
                    work_dir="${_dir//\{slug\}/$session_slug}"
                else
                    work_dir="${P_dir:-$(pwd)}"
                    work_dir="${work_dir/#\~/$HOME}"
                fi
            else
                work_dir="${P_dir:-$(pwd)}"
                work_dir="${work_dir/#\~/$HOME}"
            fi
            ;;
    esac

    [[ ! -d "$work_dir" ]] && mkdir -p "$work_dir" && echo "Created: $work_dir"

    # ── Session name ─────────────────────────────────────────────────────
    local type_short=""
    case "$agent_type" in
        repo) type_short="${repo_name//./-}" ;;
        *)    type_short="$agent_type" ;;
    esac
    local session_name="${AGENT_SESSION_PREFIX}${type_short}-${session_slug}"

    if command tmux has-session -t "$session_name" 2>/dev/null; then
        echo "Session '$session_name' already exists."
        return 1
    fi

    # ── Port allocation (repo agents w/ a known dev command) ─────────────
    local agent_port="" dev_template=""
    if [[ "$agent_type" == "repo" ]]; then
        dev_template=$(_agent_repo_dev_cmd "$repo_name")
        if [[ -n "$dev_template" ]]; then
            agent_port=$(_allocate_port)
            [[ -n "$agent_port" ]] && echo "Dev server port: $agent_port"
        fi
    fi

    # ── Create session + launch ──────────────────────────────────────────
    local notes_file=$(_ext_notes_ref)
    local today=$(date +%Y-%m-%d)
    _agent_append_row "active" "${P_title:0:120}" "$task_id" "$session_name" "$notes_file" "$today" "$type_label" ""

    command tmux new-session -d -s "$session_name" -c "$work_dir"

    # Dev server in background pane (only repos with a wired dev command)
    if [[ "$agent_type" == "repo" && -n "$dev_template" && -n "$agent_port" ]]; then
        local dev_cmd="${dev_template//\$PORT/$agent_port}"
        command tmux split-window -d -t "$session_name" -h -c "$work_dir" \
            "$dev_cmd; echo 'Dev server exited. Press enter to close.'; read"
    fi

    # Write prompt file
    local prompt_file="/tmp/agent-prompt-${session_name}.md"
    {
        echo "You are working on:"
        echo ""
        echo "$P_prompt"
        echo ""
        _ext_prompt_extras "$task_id" "$notes_file"
        if [[ "$agent_type" == "repo" ]] && _agent_repo_is_frontend "$work_dir"; then
            echo ""
            echo "This is an HTML/CSS repo. Use the frontend-design skill (/frontend-design) for any UI, layout, or styling work — invoke it before making visual changes."
        fi
        [[ -n "$agent_port" ]] && echo "Dev server: http://localhost:${agent_port} (running in right pane)"
        echo ""
        echo "When you finish or get blocked, run: agent-update <status> \"<note>\""
    } > "$prompt_file"

    # Save tool + port for resume
    echo "$tool" > "/tmp/agent-tool-${session_name}"

    local launch_cmd=$(_agent_tool_cmd "$tool" "$prompt_file" "$session_name")
    command tmux send-keys -t "$session_name" "$launch_cmd" Enter

    echo ""
    echo "Session '$session_name' created."
    [[ -n "$agent_port" ]] && echo "Dev server: http://localhost:${agent_port}"
    echo "Dir: $work_dir"

    command tmux switch-client -t "$session_name"
}

# ── agent-review (ar) ────────────────────────────────────────────────────
# Walk through an agent's change inside its own pane — all back-and-forth
# happens there. No arg: pick from agents in review/blocked.
agent-review() {
    local rows
    rows=$(python3 - "$AGENT_FILE" "${1:-}" <<'PY'
import csv, sys
path, want = sys.argv[1], sys.argv[2].lower()
rows = list(csv.DictReader(open(path)))
if want:
    rows = [r for r in rows if r['Status'] != 'done' and
            (want in r['Session'].lower() or want == (r.get('TaskID') or '').lower())]
else:
    rows = [r for r in rows if r['Status'] in ('review', 'blocked')]
for r in rows:
    print(f"{r['Session']}\t{r['Status']}\t{r.get('TaskID') or ''}\t{r['Task']}")
PY
)
    [[ -z "$rows" ]] && { echo "Nothing waiting on review${1:+ matching '$1'}."; return 1; }
    local session="$rows"
    if (( $(wc -l <<< "$rows") > 1 )); then
        session=$(fzf --delimiter='\t' --prompt='review> ' <<< "$rows") || return 0
    fi
    session="${session%%$'\t'*}"
    command tmux has-session -t "$session" 2>/dev/null \
        || { echo "tmux session '$session' is gone — run: agent-resume $session"; return 1; }

    local walkthrough="REVIEW WALKTHROUGH — I'm here now. 1) Run the ponytail:ponytail-review skill on your full diff vs the base branch; report findings, don't fix yet. 2) Walk me through the change: the problem, what changed per file (key hunks only), why this approach. 3) NEEDS MY ATTENTION: decisions only I can make, assumptions, risky spots, anything untested, anything skipped or deviating from the ticket. 4) How to check it: branch, MR link, what's pushed, video (agent-video $session), checklist path. Keep it scannable, then wait for me. If I ask for changes: agent-update active while working, agent-update review when ready again. Never merge or deploy."

    local pane=$(command tmux list-panes -t "$session" -F '#{pane_id} #{pane_current_command}' \
        | grep -E " ($(_agent_tool_processes))\$" | head -1 | cut -d' ' -f1)
    if [[ -n "$pane" ]]; then
        command tmux send-keys -t "$pane" -l "$walkthrough"
        sleep 0.3  # let the TUI take the paste before Enter, or Enter lands mid-paste
        command tmux send-keys -t "$pane" Enter
    else
        pane=$(command tmux list-panes -t "$session" -F '#{pane_id}' | head -1)
        local tool="$AGENT_TOOL"
        [[ -f "/tmp/agent-tool-${session}" ]] && tool=$(cat "/tmp/agent-tool-${session}")
        command tmux send-keys -t "$pane" "$tool --continue ${(q)walkthrough}" Enter
    fi
    command tmux select-pane -t "$pane"
    command tmux switch-client -t "$session" 2>/dev/null || command tmux attach -t "$session"
}
alias ar=agent-review

# ── agent-resume ─────────────────────────────────────────────────────────
# Relaunch AI tool in sessions where it's not running
# Rebuild /tmp/agent-prompt-<sess>.md if wiped (e.g. /tmp cleared on reboot).
# _agent_ensure_prompt <session> [task taskid notes] — reads agents.csv when omitted.
_agent_ensure_prompt() {
    local sess="$1" task="$2" taskid="$3" notes="$4"
    local prompt_file="/tmp/agent-prompt-${sess}.md"
    [[ -f "$prompt_file" ]] && return 0
    if [[ -z "$task" ]]; then
        local row=$(python3 -c "
import csv, sys
for r in csv.DictReader(open(sys.argv[1])):
    if r.get('Session') == sys.argv[2]:
        print(chr(31).join([r.get('Task') or '', r.get('TaskID') or '', r.get('Notes') or ''])); break
" "$AGENT_FILE" "$sess" 2>/dev/null)
        [[ -z "$row" ]] && return 1
        local parts=("${(@ps:\x1f:)row}")
        task="${parts[1]}" taskid="${parts[2]}" notes="${parts[3]}"
    fi
    {
        echo "You are working on:"
        echo ""
        echo "$task"
        echo ""
        _ext_prompt_extras "$taskid" "$notes"
        echo ""
        echo "When you finish or get blocked, run: agent-update <status> \"<note>\""
    } > "$prompt_file"
}

agent-resume() {
    _agent_tools_reload || { agent-resume "$@"; return; }
    local target="$1"
    local resumed=0
    local tool_procs=$(_agent_tool_processes)

    local sessions=""
    if [[ -n "$target" ]]; then
        sessions="$target"
    else
        sessions=$(for s in $(command tmux ls -F '#S' 2>/dev/null | grep "^${AGENT_SESSION_PREFIX}"); do
            has_tool=$(command tmux list-panes -t "$s" -F '#{pane_current_command}' 2>/dev/null | grep -cE "$tool_procs")
            [[ "$has_tool" -eq 0 ]] && echo "$s"
        done)
    fi

    if [[ -z "$sessions" ]]; then
        echo "All agent sessions have AI tool running."
        return 0
    fi

    while IFS= read -r s; do
        [[ -z "$s" ]] && continue
        local prompt_file="/tmp/agent-prompt-${s}.md"
        local tool="$AGENT_TOOL"
        [[ -f "/tmp/agent-tool-${s}" ]] && tool=$(cat "/tmp/agent-tool-${s}")
        local pane_dir=$(command tmux display -p -t "$s" '#{pane_current_path}' 2>/dev/null) cdir=""
        local repo=$(_agent_repo_from_dir "$pane_dir")
        [[ -n "$repo" && "$(basename "$pane_dir")" != "$repo" ]] && cdir="$pane_dir"
        local launch_cmd=$(_agent_tool_cmd "$tool" "$prompt_file" "$s" "$cdir")
        [[ "$launch_cmd" == *"cat ${prompt_file}"* ]] && _agent_ensure_prompt "$s"
        if [[ "$launch_cmd" == *"cat ${prompt_file}"* && ! -f "$prompt_file" ]]; then
            echo "SKIP $s — no prompt file and no conversation to resume"
            continue
        fi
        command tmux send-keys -t "$s" "$launch_cmd" Enter
        echo "→ resumed $s"
        ((resumed++))
    done <<< "$sessions"

    echo "Resumed $resumed session(s)."
}

# ── agent-serve ──────────────────────────────────────────────────────────
# Add a dev server pane to an existing agent session
agent-serve() {
    _agent_tools_reload || { agent-serve "$@"; return; }
    local session="$1"

    if [[ -z "$session" ]]; then
        local sessions=$(python3 -c "
import csv
with open('$AGENT_FILE') as f:
    for row in csv.reader(f):
        if len(row) > 6 and row[0] in ('active','review','hold') and row[6].startswith('repo:'):
            print(row[3])
" 2>/dev/null)
        if [[ -z "$sessions" ]]; then
            echo "No active repo agents."
            return 0
        fi
        echo "Active repo agents:"
        local i=1 picks=()
        while IFS= read -r s; do
            printf "  %2d) %s\n" "$i" "$s"
            picks+=("$s")
            ((i++))
        done <<< "$sessions"
        printf "Pick (number or name): "
        read -r pick
        if [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick <= ${#picks[@]} )); then
            session="${picks[$pick]}"
        else
            session="$pick"
        fi
    fi

    if ! command tmux has-session -t "$session" 2>/dev/null; then
        echo "Session '$session' not found."
        return 1
    fi

    local work_dir=$(command tmux display-message -t "$session" -p '#{pane_current_path}' 2>/dev/null)
    local agent_port=$(_allocate_port)

    if [[ -z "$agent_port" ]]; then
        echo "No available ports."
        return 1
    fi

    # Resolve the repo's wired dev command; fall back to the default for a
    # manual serve so this still works on repos without an auto-run entry.
    local dev_template=$(_agent_repo_dev_cmd "$(_agent_repo_from_dir "$work_dir")")
    [[ -z "$dev_template" ]] && dev_template="$AGENT_DEV_CMD"
    local dev_cmd="${dev_template//\$PORT/$agent_port}"
    command tmux split-window -t "$session" -h -c "$work_dir" \
        "$dev_cmd; echo 'Dev server exited. Press enter to close.'; read"

    echo "Dev server on http://localhost:${agent_port} in '$session'"
}

# ── agent-done ───────────────────────────────────────────────────────────
agent-done() {
    _agent_tools_reload || { agent-done "$@"; return; }
    local session_name="$1"

    if [[ -z "$session_name" ]]; then
        echo "Usage: agent-done <session-name>"
        return 1
    fi

    if _agent_has_session "$session_name"; then
        _agent_update_field "$session_name" 0 "done"
        echo "Marked '$session_name' as done"
        echo "- [$(date +%Y-%m-%d)] ${session_name}: marked done" >> "$AGENT_LOG"
    else
        echo "Session '$session_name' not found in agents.csv"
        return 1
    fi

    local worktree_path=""
    if command tmux has-session -t "$session_name" 2>/dev/null; then
        local pane_path=$(command tmux display-message -t "$session_name" -p '#{pane_current_path}' 2>/dev/null)
        [[ -n "$pane_path" && -f "$pane_path/.git" ]] && worktree_path="$pane_path"
        [[ "$(_agent_field "$session_name" 6)" == multi ]] && worktree_path="$pane_path"

        printf "Kill tmux session? (y/n): "
        read -r kill_it
        if [[ "$kill_it" == "y" ]]; then
            command tmux kill-session -t "$session_name"
            echo "Session killed."
            if [[ -n "$worktree_path" ]]; then
                _remove_worktree "$worktree_path"
            fi
            if [[ -d "$HOME/.agents/videos/$session_name" ]]; then
                rm -rf "$HOME/.agents/videos/$session_name"
                # latest.mp4 may point into the purged dir
                [[ -L "$HOME/.agents/videos/latest.mp4" && ! -e "$HOME/.agents/videos/latest.mp4" ]] \
                    && rm -f "$HOME/.agents/videos/latest.mp4"
                echo "Recordings purged: videos/$session_name"
            fi
        fi
    fi
}

_remove_worktree() {
    local wt_path="$1"
    if [[ -d "$wt_path" && ! -e "$wt_path/.git" ]]; then
        # Multi-repo agent: the dir only holds worktrees, so it goes once they do.
        local d
        for d in "$wt_path"/*(N/); do _remove_worktree "$d"; done
        rmdir "$wt_path" 2>/dev/null
        return 0
    fi
    [[ -z "$wt_path" || ! -f "$wt_path/.git" ]] && return 1
    local main_repo=$(cd "$wt_path" && git rev-parse --git-common-dir 2>/dev/null | sed 's|/\.git$||')
    if [[ -n "$main_repo" ]]; then
        git -C "$main_repo" worktree remove "$wt_path" --force 2>/dev/null || {
            rm -rf "$wt_path"
            git -C "$main_repo" worktree prune 2>/dev/null
        }
        echo "Worktree removed: $(basename "$wt_path")"
    fi
}

# ── agent-status ─────────────────────────────────────────────────────────
agent-status() {
    _agent_tools_reload || { agent-status "$@"; return; }
    local tmpfile="/tmp/agent-status-view.csv"

    python3 -c '
import csv, subprocess, re, sys, os

agent_file = sys.argv[1]
excluded = sys.argv[2]
tmpfile = sys.argv[3]
prefix = sys.argv[4]

tracked = []
with open(agent_file) as f:
    for row in csv.DictReader(f):
        tracked.append(row)
tracked_names = {t["Session"] for t in tracked}

live = set()
try:
    out = subprocess.check_output(["tmux", "ls"], stderr=subprocess.DEVNULL, text=True)
    for line in out.splitlines():
        sname = line.split(":")[0]
        if not re.match(excluded, sname):
            live.add(sname)
except Exception:
    pass

with open(tmpfile, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Status", "Task", "TaskID", "Session", "Type", "Started"])
    for t in tracked:
        status = t.get("Status", "")
        if status == "active" and t.get("Session", "") not in live:
            status = "STALE"
        w.writerow([status, t.get("Task", ""), t.get("TaskID", t.get("ClickUp", "")),
                     t.get("Session", ""), t.get("Type", ""), t.get("Started", "")])

    for sname in sorted(live):
        if sname in tracked_names or not sname.startswith(prefix):
            continue
        w.writerow(["untracked", "", "", sname, "", ""])
' "$AGENT_FILE" "$EXCLUDED_SESSIONS" "$tmpfile" "$AGENT_SESSION_PREFIX"

    if command -v csvlens &>/dev/null; then
        csvlens "$tmpfile"
    else
        column -t -s, "$tmpfile"
    fi
}

# ── agent-track ──────────────────────────────────────────────────────────
agent-track() {
    _agent_tools_reload || { agent-track "$@"; return; }
    local rename_to=""
    while [[ "$1" == -* ]]; do
        case "$1" in
            -n) rename_to="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    local session_name="$1"
    [[ -z "$session_name" ]] && echo "Usage: agent-track [-n new-name] <session>" && return 1

    command tmux has-session -t "$session_name" 2>/dev/null || { echo "Session not found."; return 1; }
    _agent_has_session "$session_name" && { echo "Already tracked."; return 1; }

    printf "Task ID (enter to skip): "
    read -r task_id

    printf "Description: "
    read -r description
    [[ -z "$description" ]] && echo "Description required." && return 1

    local tracked_name="$session_name"
    if [[ -n "$rename_to" ]]; then
        rename_to=$(_sanitize_session "$rename_to")
        command tmux rename-session -t "$session_name" "$rename_to"
        tracked_name="$rename_to"
    fi

    local notes_file=$(_ext_notes_ref)
    _agent_append_row "active" "$description" "$task_id" "$tracked_name" "$notes_file" "$(date +%Y-%m-%d)" "" ""
    echo "Now tracking '$tracked_name'"
}

# ── agent-checkin ────────────────────────────────────────────────────────
agent-checkin() {
    _agent_tools_reload || { agent-checkin "$@"; return; }
    local sessions=() tasks=()

    while IFS=',' read -r status task taskid session rest; do
        [[ "$status" == "Status" ]] && continue
        [[ "$status" != "active" ]] && continue
        session=$(echo "$session" | xargs)
        [[ -z "$session" ]] && continue
        command tmux has-session -t "$session" 2>/dev/null || continue
        sessions+=("$session")
        tasks+=("$task")
    done < "$AGENT_FILE"

    if [[ ${#sessions} -eq 0 ]]; then
        echo "No active sessions."
        return 0
    fi

    echo ""
    echo " Checking ${#sessions} active session(s)..."
    echo " [enter] switch  [s] skip  [d] done  [q] quit"
    echo ""

    local current_session=$(command tmux display-message -p '#S' 2>/dev/null)

    for i in {1..${#sessions}}; do
        local sess="${sessions[$i]}"
        local task="${tasks[$i]}"

        echo " ─────────────────────────────────────────"
        printf " %s — %s\n" "$sess" "$task"
        printf " [enter/s/d/q]: "
        read -r action

        case "$action" in
            q|Q) break ;;
            s|S) continue ;;
            d|D) agent-done "$sess" ;;
            *)
                command tmux switch-client -t "$sess" 2>/dev/null
                printf " Back? [enter] next  [d] done  [b] blocked: "
                read -r result
                case "$result" in
                    d|D) agent-done "$sess" ;;
                    b|B) _agent_update_field "$sess" 0 "blocked"
                         echo "- [$(date +%Y-%m-%d)] ${sess}: blocked" >> "$AGENT_LOG" ;;
                esac
                ;;
        esac
    done

    [[ -n "$current_session" ]] && command tmux switch-client -t "$current_session" 2>/dev/null
    echo "Check-in complete."
}

# ── agent-restart ────────────────────────────────────────────────────────
# Recreate a tmux session for a tracked row whose session was killed.
# Reconstructs work_dir from type_label (repo:<name> or registered type
# with {slug} template), rebuilds prompt file if missing, launches tool.
agent-restart() {
    _agent_tools_reload || { agent-restart "$@"; return; }
    local sess="$1"
    [[ -z "$sess" ]] && echo "Usage: agent-restart <session>" && return 1

    if command tmux has-session -t "$sess" 2>/dev/null; then
        echo "Session already alive. Use agent-resume to relaunch the tool."
        return 1
    fi

    local row=$(python3 -c "
import csv, sys
with open('$AGENT_FILE') as f:
    r = csv.reader(f); next(r, None)
    for row in r:
        if len(row) > 3 and row[3] == sys.argv[1]:
            print(chr(31).join(row)); break
" "$sess" 2>/dev/null)
    if [[ -z "$row" ]]; then
        echo "Session '$sess' not found in agents.csv"
        return 1
    fi

    local IFS=$'\x1f'
    local parts=("${(@ps.\x1f.)row}")
    local csv_status="${parts[1]}"
    local csv_task="${parts[2]}"
    local csv_taskid="${parts[3]}"
    local csv_notes="${parts[5]}"
    local type_label="${parts[7]}"

    # Reconstruct work_dir
    local work_dir=""
    if [[ "$type_label" == repo:* ]]; then
        local repo="${type_label#repo:}"
        local type_short="${repo//./-}"
        local prefix="${AGENT_SESSION_PREFIX}${type_short}-"
        local slug="${sess#$prefix}"
        work_dir="$REPO_DIR/${repo}-${slug}"
        if [[ ! -d "$work_dir" ]]; then
            # Worktree may live inside the main repo's .git/worktrees structure.
            # Try the main-repo-relative path as fallback.
            local alt="$REPO_DIR/$repo"
            [[ -d "$alt/.git" ]] && work_dir="$alt"
        fi
    elif [[ "$type_label" == auto ]]; then
        work_dir="$REPO_DIR"
    elif [[ -n "${_AGENT_TYPES[$type_label]}" ]]; then
        local _tv="${_AGENT_TYPES[$type_label]}"
        local _dir="${_tv##*:}"
        local stripped="${sess#${AGENT_SESSION_PREFIX}${type_label}-}"
        work_dir="${_dir//\{slug\}/$stripped}"
    fi

    if [[ -z "$work_dir" || ! -d "$work_dir" ]]; then
        echo "Cannot resolve work dir (guess: '$work_dir'). Use: agent-restart $sess -d <path>"
        return 1
    fi

    local prompt_file="/tmp/agent-prompt-${sess}.md"
    _agent_ensure_prompt "$sess" "$csv_task" "$csv_taskid" "$csv_notes"

    local tool="$AGENT_TOOL"
    [[ -f "/tmp/agent-tool-${sess}" ]] && tool=$(cat "/tmp/agent-tool-${sess}")

    local cdir=""
    [[ "$type_label" == repo:* && "$work_dir" != "$REPO_DIR/${type_label#repo:}" ]] && cdir="$work_dir"
    [[ "$type_label" == multi ]] && cdir="$work_dir"
    command tmux new-session -d -s "$sess" -c "$work_dir"
    local launch_cmd=$(_agent_tool_cmd "$tool" "$prompt_file" "$sess" "$cdir")
    command tmux send-keys -t "$sess" "$launch_cmd" Enter

    echo "Restarted '$sess' at $work_dir ($launch_cmd)"
    [[ "$2" == --bg ]] && return 0
    command tmux switch-client -t "$sess" 2>/dev/null || command tmux attach -t "$sess"
}

# ── Aliases ──────────────────────────────────────────────────────────────
agents() { agent-dashboard "$@"; }

# ── Completion ───────────────────────────────────────────────────────────
_agent_all_sessions() {
    command tmux ls -F '#S' 2>/dev/null | grep "^${AGENT_SESSION_PREFIX}"
}

_agent_tracked_sessions() {
    python3 -c "
import csv, sys
try:
    with open('$AGENT_FILE') as f:
        r = csv.reader(f)
        header = next(r, None)
        for row in r:
            if len(row) > 3 and row[3]:
                print(row[3])
except Exception: pass
" 2>/dev/null
}

_agent_untracked_sessions() {
    local tracked=$(_agent_tracked_sessions)
    local all=$(_agent_all_sessions)
    comm -23 <(echo "$all" | sort) <(echo "$tracked" | sort)
}

_agent_track_complete() {
    local -a sessions
    sessions=(${(f)"$(_agent_untracked_sessions)"})
    _describe 'untracked session' sessions
}

_agent_session_complete() {
    local -a sessions
    sessions=(${(f)"$(_agent_tracked_sessions)"})
    _describe 'tracked session' sessions
}

if [[ -n "$ZSH_VERSION" ]]; then
    autoload -Uz compinit 2>/dev/null
    (( $+functions[compdef] )) || compinit -i 2>/dev/null
    compdef _agent_track_complete   agent-track
    compdef _agent_session_complete agent-resume agent-done agent-serve agent-restart
fi

# ── Auto-track ───────────────────────────────────────────────────────────
# Scan live tmux sessions with the agent prefix and insert rows for any
# not already in agents.csv. Derives a best-guess Type from session name
# and pane working directory. Idempotent — runs cheaply on every source.
_agent_auto_track() {
    [[ ! -f "$AGENT_FILE" ]] && return 0
    command -v tmux &>/dev/null || return 0

    local tracked=$(_agent_tracked_sessions)
    local today=$(date +%Y-%m-%d)
    local notes=$(_ext_notes_ref 2>/dev/null)
    local added=0

    local s
    for s in $(command tmux ls -F '#S' 2>/dev/null | grep "^${AGENT_SESSION_PREFIX}"); do
        grep -Fxq "$s" <<< "$tracked" && continue

        # Best-guess type from pane cwd
        local cwd=$(command tmux display-message -t "$s" -p '#{pane_current_path}' 2>/dev/null)
        local type_label=""
        if [[ -n "$cwd" && "$cwd" == "$REPO_DIR"/* ]]; then
            local rest="${cwd#$REPO_DIR/}"
            local repo="${rest%%/*}"
            # Strip worktree suffix (-<slug>) if the real repo name differs
            if [[ -d "$REPO_DIR/$repo/.git" ]]; then
                type_label="repo:$repo"
            else
                # Walk up to find a tracked sibling repo
                local base="${repo%%-*}"
                for r in "$REPO_DIR"/*/; do
                    local rn=$(basename "$r")
                    if [[ -d "$r/.git" && "$repo" == "$rn"* ]]; then
                        type_label="repo:$rn"
                        break
                    fi
                done
            fi
        else
            # Fall back to the prefix after z-/zz-
            local stripped="${s#${AGENT_SESSION_PREFIX}}"
            type_label="${stripped%%-*}"
            [[ "$type_label" == "$stripped" ]] && type_label=""
        fi

        _agent_append_row "active" "$s" "" "$s" "$notes" "$today" "$type_label" ""
        ((added++))
    done

    [[ $added -gt 0 ]] && echo "agent-tools: auto-tracked $added session(s)"
}

# Run at source time, but only in interactive shells so scripts / subshells
# don't spam noise. Wrapped so the source always returns 0 regardless —
# callers rely on `source agent-tools.sh && ...` chains.
if [[ -o interactive ]]; then
    _agent_auto_track 2>/dev/null || true
fi
true
