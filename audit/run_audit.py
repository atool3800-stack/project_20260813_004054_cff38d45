#!/usr/bin/env python3
"""CLI entrypoint for the Oceania government license hourly audit.

Usage:
    python -m audit.run_audit --config config/audit_config.json \
        [--dry-run] [--limit N] [--summary outputs/audit_summary.json]

Environment variables (optional but recommended):
    HF_TOKEN   - Hugging Face token (for private/rate-limited access)
    GH_TOKEN   - GitHub token (required to create/update issues)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# allow running as a script from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit.auditor import Auditor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Oceania gov license hourly audit")
    ap.add_argument("--config", default="config/audit_config.json", help="audit config JSON")
    ap.add_argument("--dry-run", action="store_true", help="scan + evaluate but do not touch GitHub issues")
    ap.add_argument("--limit", type=int, default=0, help="cap on records scanned (0 = all)")
    ap.add_argument("--summary", default=None, help="explicit summary output path (default: outputs/audit_summary_<ts>.json)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with open(args.config) as f:
        cfg = json.load(f)

    if args.limit:
        cfg["audit"]["max_records"] = args.limit

    hf_token = os.environ.get("HF_TOKEN") or None
    gh_token = os.environ.get("GH_TOKEN") or None
    if not gh_token and not args.dry_run:
        print("WARNING: GH_TOKEN not set; issue sync will be skipped. "
              "Set GH_TOKEN or use --dry-run.", file=sys.stderr)

    auditor = Auditor(cfg, hf_token=hf_token, gh_token=gh_token, dry_run=args.dry_run)
    t0 = time.time()
    try:
        summary = auditor.run()
    finally:
        auditor.hf.close()
        if auditor.gh:
            auditor.gh.close()
    summary["elapsed_seconds"] = round(time.time() - t0, 3)

    # ---- write summary ----
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = cfg["output"]["summary_dir"]
    summary_path = args.summary or os.path.join(out_dir, f"audit_summary_{ts}.json")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    # latest pointer
    latest = cfg["output"].get("latest_symlink")
    if latest:
        with open(latest, "w") as f:
            json.dump(summary, f, indent=2)
    print(f"\nSummary written to {summary_path}")
    print(json.dumps(summary.get("counts", {}), indent=2))
    print("issues:", summary.get("issues"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
