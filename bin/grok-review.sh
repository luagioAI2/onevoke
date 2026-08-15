#!/usr/bin/env bash

# grok-review.sh — Grok 审核 wrapper 薄壳.
# 只读运行 Grok CLI 执行角色审核; 公共框架见 review-lib.sh.

set -euo pipefail

export REVIEW_AGENT_NAME=grok
export REVIEW_AGENT_LABEL=Grok
export REVIEW_BIN="${GROK_REVIEW_BIN:-grok}"
export REVIEW_HOME="${GROK_HOME:-$HOME/.grok}"
export REVIEW_MODEL="${GROK_REVIEW_MODEL:-}"
export REVIEW_REASONING_EFFORT="${GROK_REVIEW_REASONING_EFFORT:-high}"
export REVIEW_CHECK_INTERVAL_SECONDS="${GROK_REVIEW_CHECK_INTERVAL_SECONDS:-600}"
export REVIEW_MAX_RUNTIME_SECONDS="${GROK_REVIEW_MAX_RUNTIME_SECONDS:-1800}"
export REVIEW_TOOLS_GUIDANCE="Use only read_file, grep, and list_dir to inspect code. Do not modify files, the index, refs, or the worktree."

review_launch() {
  local command=(env "GROK_HOME=$REVIEW_STATE_ROOT" "$REVIEW_BIN" --cwd "$RUNTIME_DIR")
  if [[ -n "$REVIEW_MODEL" ]]; then
    command+=(--model "$REVIEW_MODEL")
  fi
  command+=(
    --effort "$REVIEW_REASONING_EFFORT"
    --output-format json
    --permission-mode dontAsk
    --allow Read
    --allow Grep
    --tools "read_file,grep,list_dir"
    --disallowed-tools "Agent,run_terminal_command,search_tool,use_tool,web_search,web_fetch,search_replace,todo_write,scheduler_create,scheduler_delete,scheduler_list,monitor,workflow,enter_plan_mode,exit_plan_mode,ask_user_question,image_gen,image_edit,image_to_video,reference_to_video,write"
    --deny Edit
    --deny Write
    --deny 'MCPTool(*)'
    --sandbox read-only
    --disable-web-search
    --no-memory
    --no-subagents
    --no-plan
    --verbatim
    --prompt-file "$PROMPT_FILE"
  )
  "${command[@]}" >"$OUTPUT_FILE" 2>"$ERROR_FILE" &
  REVIEW_PID=$!
}

review_extract() {
  if ! review_text=$(python3 - "$OUTPUT_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    result = json.load(stream)
text = result.get("text")
if result.get("stopReason") != "end_turn" or not isinstance(text, str) or not text:
    raise SystemExit(1)
print(text)
PY
); then
    cat "$OUTPUT_FILE"
    fail "$REVIEW_AGENT_LABEL review did not complete with review text" 1
  fi
  printf '%s\n' "$review_text"
}

source "$(dirname "$0")/review-lib.sh"
