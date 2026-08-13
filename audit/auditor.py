"""Main orchestration for the Oceania government license audit.

Flow:
1. Load configuration.
2. Load the compliance policy from the gov-license-policy GitHub repo.
3. Discover / use the configured Hugging Face mirror catalogues.
4. For every mirror: fetch HF metadata + paginate all catalog rows (10000+).
5. Evaluate each record against the policy.
6. Create / update / close GitHub issues labelled `hourly-license-audit`.
7. Write a JSON audit summary to the output directory.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import policy as policy_mod
from .github_client import GHClient
from .huggingface_client import HFClient, RateLimiter

log = logging.getLogger("auditor")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Auditor:
    def __init__(self, cfg: dict, hf_token: Optional[str] = None,
                 gh_token: Optional[str] = None, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        a = cfg["audit"]
        self.rate = RateLimiter(a["rate_limit"]["requests_per_minute"])
        self.hf = HFClient(
            api_base=cfg["huggingface"]["api_base"],
            datasets_server_base=cfg["huggingface"]["datasets_server_base"],
            token=hf_token, retry_cfg=a["retry"], rate=self.rate, timeout=a["request_timeout_seconds"],
        )
        self.gh = GHClient(token=gh_token or "", api_base=cfg["github"]["api_base"],
                           retry_cfg=a["retry"], timeout=a["request_timeout_seconds"]) if gh_token else None
        self.summary = {
            "audit_name": cfg["audit"]["name"],
            "scan_start": _utcnow_iso(),
            "policy_version": None,
            "sources": [],
            "counts": {},
            "issues": {},
            "errors": [],
        }

    # ------------------------------------------------------------------ #
    def run(self) -> dict:
        gh = self.cfg["github"]
        hf = self.cfg["huggingface"]

        # 1. policy
        log.info("loading policy from %s/%s@%s", gh["owner"], gh["policy_repo"], gh["policy_path"])
        pol = policy_mod.load_policy(gh["owner"], gh["policy_repo"], gh["policy_path"],
                                     token=getattr(self, "_gh_token", None) or self._token_for_policy())
        self.summary["policy_version"] = pol.get("version")
        label = gh["issue_label"]

        # 2. discover mirror catalogues
        mirrors = self._discover_mirrors(hf)
        self.summary["sources"] = mirrors
        log.info("discovered %d mirror catalogues", len(mirrors))

        # 3. scan + evaluate
        counts = Counter()
        violations = []          # records needing an issue
        page_size = self.cfg["audit"]["page_size"]
        max_records = self.cfg["audit"].get("max_records", 0)
        scanned = 0
        total_found = 0

        for mirror in mirrors:
            repo_id = mirror["repo_id"]
            try:
                meta = self.hf.get_dataset_metadata(repo_id)
                mirror["metadata"] = {
                    "id": meta.get("id"),
                    "license_tag": (meta.get("cardData") or {}).get("license"),
                    "last_modified": meta.get("lastModified"),
                    "downloads": meta.get("downloads"),
                    "private": meta.get("private"),
                }
                readme = self.hf.get_readme(repo_id)
                mirror["readme_license_decl"] = self._extract_readme_license(readme)
            except Exception as exc:  # keep going even if one mirror fails
                log.exception("mirror metadata failed for %s", repo_id)
                self.summary["errors"].append(f"{repo_id}: metadata: {exc}")

            for row in self.hf.iter_catalog_rows(repo_id, hf["catalog_filename"], page_size=1000):
                total_found += 1
                if max_records and scanned >= max_records:
                    break
                status, reason, basis = policy_mod.evaluate(row, pol)
                counts[status] += 1
                scanned += 1
                if status != policy_mod.STATUS_COMPLIANT:
                    violations.append({
                        "mirror": repo_id,
                        "record": row,
                        "status": status,
                        "reason": reason,
                        "rule_basis": basis,
                        "detected_at": _utcnow_iso(),
                    })
            if max_records and scanned >= max_records:
                break

        self.summary["counts"] = {
            "total_scanned": scanned,
            "total_found_in_files": total_found,
            "compliant": counts[policy_mod.STATUS_COMPLIANT],
            "violations": counts[policy_mod.STATUS_VIOLATION],
            "missing": counts[policy_mod.STATUS_MISSING],
            "needs_review": counts[policy_mod.STATUS_NEEDS_REVIEW],
            "changed": counts[policy_mod.STATUS_CHANGED],
        }
        log.info("scan complete: %s", json.dumps(self.summary["counts"]))

        # 4. issues
        issues = {"created": 0, "updated": 0, "closed": 0, "skipped_dry_run": 0}
        if self.gh and not self.dry_run:
            self.gh.ensure_label(gh["owner"], gh["policy_repo"], label,
                                 color="b60205", description="Hourly Oceania gov dataset license audit findings")
            existing = self._index_existing_issues(gh["owner"], gh["policy_repo"], label)
            issues.update(self._sync_issues(gh["owner"], gh["policy_repo"], label, existing, violations))
        else:
            log.info("issue sync disabled (dry_run=%s, gh=%s)", self.dry_run, bool(self.gh))
            issues["skipped_dry_run"] = len(violations)
        self.summary["issues"] = issues
        self.summary["issue_count"] = sum(v for k, v in issues.items() if k != "skipped_dry_run")

        # 5. finalise
        self.summary["scan_end"] = _utcnow_iso()
        self.summary["elapsed_seconds"] = round(self.summary.get("elapsed_seconds", 0), 3)
        self.summary["sample_findings"] = violations[:10]
        return self.summary

    # ------------------------------------------------------------------ #
    def _token_for_policy(self) -> Optional[str]:
        return None  # policy repo is public; raw fetch needs no auth

    def _discover_mirrors(self, hf) -> List[dict]:
        configured = hf.get("mirror_datasets") or []
        mirrors = [{"repo_id": rid} for rid in configured]
        # optionally expand with a paginated search over the author's repos
        if hf.get("discover_via_search", False):
            search_terms = hf.get("mirror_search_terms", [])
            seen = {m["repo_id"] for m in mirrors}
            for term in search_terms:
                for item in self.hf.list_datasets(author=hf.get("author"), search=term, limit=100, max_pages=2):
                    rid = item.get("id")
                    if rid and rid not in seen:
                        seen.add(rid)
                        mirrors.append({"repo_id": rid, "discovered_by_search": term})
        return mirrors

    def _extract_readme_license(self, readme: Optional[str]) -> Optional[str]:
        if not readme:
            return None
        # try YAML front matter `license:` key
        m = re_search(r"(?m)^license:\s*[\"']?([\w\.\-]+)", readme)
        if m:
            return m.group(1)
        return None

    def _index_existing_issues(self, owner, repo, label) -> Dict[str, int]:
        mapping = {}
        for issue in self.gh.iter_issues_with_label(owner, repo, label, state="all"):
            marker = self._dataset_id_from_title(issue.get("title", ""))
            if marker:
                mapping[marker] = issue["number"]
        log.info("indexed %d existing labelled issues", len(mapping))
        return mapping

    def _dataset_id_from_title(self, title: str) -> Optional[str]:
        # [hourly-license-audit] <dataset_id>: ...
        parts = title.split("]", 1)
        if len(parts) < 2:
            return None
        rest = parts[1].strip()
        if ":" in rest:
            return rest.split(":", 1)[0].strip()
        return rest.strip() or None

    def _sync_issues(self, owner, repo, label, existing, violations) -> Dict[str, int]:
        """Create or update one issue per non-compliant record; close stale ones."""
        gh = self.gh
        counts = {"created": 0, "updated": 0, "closed": 0}
        touched = set()

        for v in violations:
            record = v["record"]
            ds_id = record.get("id")
            if not ds_id:
                continue
            touched.add(ds_id)
            title = f"{self.cfg['github']['issue_title_prefix']} {ds_id}: {record.get('license') or 'MISSING'}"
            body = self._build_issue_body(v)
            if ds_id in existing:
                gh.update_issue(owner, repo, existing[ds_id], body=body)
                counts["updated"] += 1
                log.debug("updated issue #%d for %s", existing[ds_id], ds_id)
            else:
                gh.create_issue(owner, repo, title, body, labels=[label])
                counts["created"] += 1
                log.debug("created issue for %s", ds_id)

        # close issues for records that are now compliant (no longer in violations)
        for ds_id, num in existing.items():
            if ds_id not in touched:
                gh.close_issue(owner, repo, num,
                               comment="Auto-closed by hourly audit: dataset is now compliant with policy.")
                counts["closed"] += 1
                log.info("closed issue #%d for %s (now compliant)", num, ds_id)
        return counts

    def _build_issue_body(self, v: dict) -> str:
        record = v["record"]
        mirror = v["mirror"]
        gh = self.cfg["github"]
        hf_url = f"https://huggingface.co/datasets/{mirror}"
        source = record.get("source", "")
        source_url = {
            "data.gov.au": f"https://data.gov.au/data/dataset/{record.get('id')}",
            "data.govt.nz": f"https://catalogue.data.govt.nz/dataset/{record.get('id')}",
        }.get(source, hf_url)
        return (
            f"## Hourly license audit finding\n\n"
            f"**Dataset name:** {record.get('title', record.get('id'))}\n\n"
            f"**Dataset ID:** `{record.get('id')}`\n\n"
            f"**Hugging Face URL:** {hf_url}\n\n"
            f"**Source portal:** [{source}]({source_url})  \n"
            f"**Publisher:** {record.get('publisher', '-')}  \n"
            f"**Category:** {record.get('category', '-')}  \n\n"
            f"---\n\n"
            f"**Status:** `{v['status']}`\n\n"
            f"**Discovered license:** `{record.get('license') or 'MISSING'}`  \n"
            f"**README license declaration:** `{record.get('readme_license') or 'MISSING'}`  \n\n"
            f"**Reason:** {v['reason']}\n\n"
            f"**Rule basis:** `{v['rule_basis']}`\n\n"
            f"**Last modified:** {record.get('last_modified', '-')}  \n"
            f"**Downloads:** {record.get('downloads', 0)}  \n\n"
            f"---\n\n"
            f"**Detected at:** {v['detected_at']}  \n"
            f"**Policy:** {gh['owner']}/{gh['policy_repo']} v{self.summary.get('policy_version')}  \n"
            f"**Audit:** {self.cfg['audit']['name']}"
        )


def re_search(pattern, text):
    import re
    return re.search(pattern, text)
