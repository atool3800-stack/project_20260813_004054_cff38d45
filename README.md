# Oceania Government License — Hourly Audit

Automated **license compliance audit** for Oceania (Australia & New Zealand) open government
data catalogues mirrored on Hugging Face (`data.gov.au`, `data.govt.nz`, combined Oceania
catalogue). Runs **hourly**, scanning **12,000+ dataset records** via paginated Hugging Face API
traversal, comparing each record against the licensing policy in
[`atool3800-stack/gov-license-policy`](https://github.com/atool3800-stack/gov-license-policy),
and creating/updating GitHub issues labelled `hourly-license-audit`.

## How it works

1. **Discover** the configured Hugging Face mirror catalogues
   (`toolathon123/data-gov-au-mirror`, `toolathon123/data-govt-nz-mirror`,
   `toolathon123/oceania-gov-open-data-catalog`).
2. **Fetch HF metadata** per catalogue via `GET /api/datasets/{id}?full=true`
   (id, `license` tag, `lastModified`, `downloads`, README license declaration).
3. **Paginate all catalog rows** (`datasets-server /rows?offset&limit`, falling back to
   chunked raw-CSV reads) — complete traversal of the 10,000+ records.
4. **Load the policy** `license-policy.json` from `gov-license-policy`.
5. **Evaluate** every record → `COMPLIANT` | `VIOLATION` | `NEEDS_REVIEW` | `CHANGED` | `MISSING`.
6. **Sync GitHub issues** labelled `hourly-license-audit`: create for new findings, update on
   re-detection, auto-close when a dataset becomes compliant.
7. **Write a JSON audit summary** (counts, issue stats, elapsed time) to `outputs/`.

## Quick start

```bash
pip install -r requirements.txt
export HF_TOKEN=...     # Hugging Face token
export GH_TOKEN=...     # GitHub token (issues permission)

# dry run (scan + evaluate, no issue changes)
python -m audit.run_audit --config config/audit_config.json --dry-run

# real run (creates/updates issues + writes summary)
python -m audit.run_audit --config config/audit_config.json
```

## Hourly scheduling

- **GitHub Actions:** `.github/workflows/hourly-audit.yml` runs on `cron: "5 * * * *"`.
  Set `HF_TOKEN` and `GH_TOKEN` as repository secrets.
- **Cron wrapper:** `./run_audit.sh` (see `COMMIT_OUTPUTS=1` to commit summaries).

## Outputs

| File | Description |
|------|-------------|
| `outputs/audit_summary_<ts>.json` | Timestamped JSON audit summary |
| `outputs/latest_summary.json` | Pointer to the latest run |

## Repository layout

```
audit/
  huggingface_client.py   # HF API: pagination, retry, rate-limit, README extraction
  github_client.py        # GitHub API: labels, create/update/close issues
  policy.py               # policy loader + compliance evaluator
  auditor.py              # orchestration
  run_audit.py            # CLI entrypoint
config/audit_config.json  # sources, policy repo, retry/rate settings
.github/workflows/        # hourly GitHub Action
```

## Policy

See [`gov-license-policy`](https://github.com/atool3800-stack/gov-license-policy)
(`license-policy.json`): allowed licenses (CC BY 4.0 / CC0 / PDDL / ODC-BY), prohibited
licenses & terms (NC/ND, all-rights-reserved, unknown), and needs-review conditions
(share-alike, `other`, README/tag mismatch).
