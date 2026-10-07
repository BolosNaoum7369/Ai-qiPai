#!/usr/bin/env bash
set -euo pipefail
: "${GH_TOKEN:?缺少 GitHub Actions Token}"
: "${GH_REPO:?缺少目标仓库}"
: "${GITHUB_REF_NAME:?缺少工作流分支}"

# 包括等待 runner、并发锁或权限的任务，避免重复补启。
active="$(gh run list --workflow ci.yml --limit 100 --json status \
  --jq '[.[] | select(.status != "completed")] | length')"
if [[ "$active" -gt 0 ]]; then
  echo '已有运行中或等待中的棋牌服务器任务'
  exit 0
fi
gh workflow run ci.yml --ref "$GITHUB_REF_NAME"
