#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$TASK_ROOT/literature-ai/deploy/scripts/github_deploy.py" ]]; then
  exec python3 "$TASK_ROOT/literature-ai/deploy/scripts/github_deploy.py" "$@"
fi
exec python3 "$TASK_ROOT/github_deploy.py" "$@"
