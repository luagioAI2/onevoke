#!/usr/bin/env bash

# deepseek-review.sh — DeepSeek 审核 wrapper 薄壳.
# 只读调用 llm-agent 适配器的 review 模式执行角色审核; 公共框架见 review-lib.sh.
# 适配器脚本 (deepseek) 由 llm-agent.py 提供, 见 bin/llm-agent.py.

set -euo pipefail

export REVIEW_AGENT_NAME=deepseek
export REVIEW_AGENT_LABEL=DeepSeek
export REVIEW_BIN="${DEEPSEEK_REVIEW_BIN:-deepseek}"
export REVIEW_HOME="${DEEPSEEK_HOME:-$HOME/.deepseek}"
export REVIEW_MODEL="${DEEPSEEK_REVIEW_MODEL:-}"
export REVIEW_REASONING_EFFORT="${DEEPSEEK_REVIEW_REASONING_EFFORT:-high}"
export REVIEW_CHECK_INTERVAL_SECONDS="${DEEPSEEK_REVIEW_CHECK_INTERVAL_SECONDS:-600}"
export REVIEW_MAX_RUNTIME_SECONDS="${DEEPSEEK_REVIEW_MAX_RUNTIME_SECONDS:-1800}"
export REVIEW_TOOLS_GUIDANCE="Use only read_file, grep, list_dir, and read-only shell inspection. Do not modify files, the index, refs, or the worktree."

if [[ ! -d "$REVIEW_HOME" ]]; then
  mkdir -p "$REVIEW_HOME"
fi

review_launch() {
  local command=(env "DEEPSEEK_HOME=$REVIEW_STATE_ROOT" "LLM_AGENT_PROVIDER=deepseek" "$REVIEW_BIN" review --cwd "$ROOT" --effort "$REVIEW_REASONING_EFFORT" --prompt-file "$PROMPT_FILE")
  if [[ -n "$REVIEW_MODEL" ]]; then
    command+=(--model "$REVIEW_MODEL")
  fi
  "${command[@]}" >"$OUTPUT_FILE" 2>"$ERROR_FILE" &
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
