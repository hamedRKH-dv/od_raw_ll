#!/bin/bash
# Stage 7. Cumulative tiers. The 500 pilot is already in catalog/ and is skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
# EC2 instance role (ml-developer) via IMDS. The default profile is SSO and expires.
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
PY=cache/venv/bin/python
for tier in 4000 8000 12000 20000; do
  echo "=== tier ${tier}: LL copy ===" >&2
  "$PY" scripts/copy_ll_subset.py --tier "$tier" --dest catalog --batch-rows 8 --skip-verify --once
  echo "=== tier ${tier}: raw write ===" >&2
  "$PY" scripts/write_raw_subset.py --tier "$tier" --dest catalog --log work/write_raw_subset.jsonl --skip-hash
  echo "=== tier ${tier}: validate ===" >&2
  "$PY" scripts/validate_paired_catalog.py --scope "tier:${tier}" --sample 100
  echo "=== tier ${tier}: done ===" >&2
done
echo "=== subset complete ===" >&2
