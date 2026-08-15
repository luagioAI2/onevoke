#!/usr/bin/env bash

# codex-review.sh — Codex 审核 wrapper 薄壳.
# 只读运行 Codex CLI 执行角色审核; 公共框架见 review-lib.sh.

set -euo pipefail

export REVIEW_AGENT_NAME=codex
export REVIEW_AGENT_LABEL=Codex
export REVIEW_BIN="${CODEX_REVIEW_BIN:-codex}"
export REVIEW_HOME="${CODEX_HOME:-$HOME/.codex}"
export REVIEW_MODEL="${CODEX_REVIEW_MODEL:-gpt-5.6-sol}"
export REVIEW_REASONING_EFFORT="${CODEX_REVIEW_REASONING_EFFORT:-high}"
export REVIEW_CHECK_INTERVAL_SECONDS="${CODEX_REVIEW_CHECK_INTERVAL_SECONDS:-600}"
export REVIEW_MAX_RUNTIME_SECONDS="${CODEX_REVIEW_MAX_RUNTIME_SECONDS:-1800}"
export REVIEW_TOOLS_GUIDANCE="Use only read-only filesystem and shell operations needed to inspect code. Do not modify files, the index, refs, or the worktree."

review_launch() {
  env CODEX_HOME="$REVIEW_STATE_ROOT" "$REVIEW_BIN" exec \
    --cd "$ROOT" \
    --model "$REVIEW_MODEL" \
    --sandbox read-only \
    --ephemeral \
    --config "model_reasoning_effort=\"$REVIEW_REASONING_EFFORT\"" \
    --config 'web_search="live"' \
    --config 'allow_login_shell=false' \
    --output-last-message "$OUTPUT_FILE" \
    - <"$PROMPT_FILE" >"$STDOUT_FILE" 2>"$ERROR_FILE" &
  REVIEW_PID=$!
}

review_extract() {
  if [[ ! -s "$OUTPUT_FILE" ]]; then
    cat "$STDOUT_FILE"
    fail "$REVIEW_AGENT_LABEL review did not complete with review text" 1
  fi
  cat "$OUTPUT_FILE"
}

source "$(dirname "$0")/review-lib.sh"
