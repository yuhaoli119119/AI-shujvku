#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -gt 0 ]]; then
  exec python3 "$PROJECT_DIR/deploy/scripts/github_deploy.py" "$@"
fi
COMMIT="$(git -C "$PROJECT_DIR" rev-parse HEAD)"
if [[ -f "$PROJECT_DIR/DEPLOYED_GITHUB_COMMIT" ]] && [[ "$(cat "$PROJECT_DIR/DEPLOYED_GITHUB_COMMIT")" == "$COMMIT" ]]; then
  exec python3 "$PROJECT_DIR/deploy/scripts/github_deploy.py" verify "$COMMIT"
fi
PLAN="$(python3 "$PROJECT_DIR/deploy/scripts/github_deploy.py" plan "$COMMIT")"
PLAN_SHA="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["plan_sha256"])' <<< "$PLAN")"
exec python3 "$PROJECT_DIR/deploy/scripts/github_deploy.py" apply "$COMMIT" --plan-sha256 "$PLAN_SHA"
