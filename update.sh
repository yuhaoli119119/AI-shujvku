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
printf '%s\n' "$PLAN"
printf 'Plan generated only. Review the exact plan and run apply explicitly with the printed plan_sha256 after all release gates pass.\n'
