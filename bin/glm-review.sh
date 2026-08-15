#!/usr/bin/env bash

# glm-review.sh — GLM (智谱) 审核 wrapper 薄壳.
# 只读调用 llm-agent 适配器的 review 模式执行角色审核; 公共框架见 review-lib.sh.
# 适配器脚本 (glm) 由 llm-agent.py 提供, 见 bin/llm-agent.py.

set -euo pipefail

export REVIEW_AGENT_NAME=glm
export REVIEW_AGENT_LABEL=GLM
export REVIEW_BIN="${GLM_REVIEW_BIN:-glm}"
export REVIEW_HOME="${GLM_HOME:-$HOME/.glm}"
export REVIEW_MODEL="${GLM_REVIEW_MODEL:-}"
export REVIEW_REASONING_EFFORT="${GLM_REVIEW_REASONING_EFFORT:-high}"
export REVIEW_CHECK_INTERVAL_SECONDS="${GLM_REVIEW_CHECK_INTERVAL_SECONDS:-600}"
export REVIEW_MAX_RUNTIME_SECONDS="${GLM_REVIEW_MAX_RUNTIME_SECONDS:-1800}"
export REVIEW_TOOLS_GUIDANCE="Use only read_file, grep, list_dir, and read-only shell inspection. Do not modify files, the index, refs, or the worktree."

if [[ ! -d "$REVIEW_HOME" ]]; then
  mkdir -p "$REVIEW_HOME"
fi

review_launch() {
  local command=(env "GLM_HOME=$REVIEW_STATE_ROOT" "LLM_AGENT_PROVIDER=glm" "$REVIEW_BIN" review --cwd "$ROOT" --effort "$REVIEW_REASONING_EFFORT" --prompt-file "$PROMPT_FILE")
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
