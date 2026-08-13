#!/usr/bin/env bash
# Hourly wrapper for the Oceania government license audit.
# Run from the repository root. Produces outputs/audit_summary_<ts>.json
# and commits it so the README / dashboard can reference the latest result.
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONPATH="$PWD"

python -m audit.run_audit --config config/audit_config.json "$@"

# Optionally commit outputs back to the repository
if [[ "${COMMIT_OUTPUTS:-0}" == "1" && -n "${GH_TOKEN:-}" ]]; then
  git add outputs/ 2>/dev/null || true
  git -c user.name='license-audit-bot' -c user.email='license-audit-bot@example.com' \
      commit -m "hourly audit summary $(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true
  git push origin HEAD 2>/dev/null || true
fi
