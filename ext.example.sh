#!/usr/bin/env zsh
# agent-tools extension template
#
# Install at:
#   ~/.config/agent-tools/ext.sh       (preferred)
#   or point $AGENT_TOOLS_EXT at a custom path
#
# Override any of the hook stubs below to integrate with your task tracker,
# notes system, or to register custom agent types.

# ── Custom agent types ───────────────────────────────────────────────────
# agent-type-register <key> <shortcut> <label> <work_dir>
# {slug} in work_dir is replaced with the session slug at spawn time.
#
# Examples:
#   agent-type-register analytics a "analytics" "$HOME/reporting/_agents/{slug}"
#   agent-type-register docs      d "docs"      "$HOME/docs/_agents/{slug}"

# ── Hook: normalize task IDs ─────────────────────────────────────────────
# Called to normalize user-entered task IDs (e.g. bare numbers → PROJ-XXXX).
# _ext_normalize_task_id() {
#     local id="$1"
#     [[ "$id" =~ ^[0-9]+$ ]] && echo "PROJ-${id}" || echo "$id"
# }

# ── Hook: task lookup ────────────────────────────────────────────────────
# Called after a task ID is provided. Set `cu_name` / `cu_status` to
# pre-fill the agent-start prompt. Return 0 on success, 1 to skip.
# _ext_task_lookup() {
#     local task_id="$1"
#     cu_name="" cu_status=""
#     # curl your task tracker here, set cu_name, return 0 on success
#     return 1
# }

# ── Hook: task create ────────────────────────────────────────────────────
# Called when task_id == "new". Must set global `task_id` to new ID.
# _ext_task_create() {
#     local title="$1"
#     # create task, set task_id
#     return 1
# }

# ── Hook: prompt extras ──────────────────────────────────────────────────
# Extra lines appended to the agent's prompt file.
# _ext_prompt_extras() {
#     local task_id="$1" notes_file="$2"
#     [[ -n "$task_id" ]] && echo "Task: https://tracker.example.com/${task_id}"
# }

# ── Hook: notes reference ────────────────────────────────────────────────
# Returns a path (relative or absolute) stored in the Notes column of agents.csv.
# _ext_notes_ref() {
#     echo "notes/current-sprint.md"
# }
