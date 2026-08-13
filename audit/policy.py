"""Policy loading + compliance evaluation for the Oceania license audit.

The policy file (license-policy.json) is fetched from the gov-license-policy
GitHub repository. Evaluation returns one of:
    COMPLIANT     - licence is allowed and consistent
    VIOLATION     - licence is prohibited / missing / contains prohibited terms
    NEEDS_REVIEW  - licence requires human review (share-alike, 'other', ...)
    CHANGED       - README declaration differs from the licence tag (review)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger("policy")

STATUS_COMPLIANT = "COMPLIANT"
STATUS_VIOLATION = "VIOLATION"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_CHANGED = "CHANGED"
STATUS_MISSING = "MISSING"


def load_policy_from_url(url: str, headers: Optional[dict] = None,
                         timeout: int = 30) -> dict:
    """Fetch the policy JSON from a URL (GitHub raw / contents API)."""
    resp = requests.get(url, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def load_policy(owner: str, repo: str, path: str, token: Optional[str] = None,
                api_base: str = "https://api.github.com",
                raw_base: str = "https://raw.githubusercontent.com") -> dict:
    """Load policy from a GitHub repo, trying raw first then the contents API."""
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    raw_url = f"{raw_base}/{owner}/{repo}/HEAD/{path}"
    try:
        return load_policy_from_url(raw_url, headers={"User-Agent": "oceania-gov-license-audit"})
    except requests.HTTPError:
        pass
    api_url = f"{api_base}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.get(api_url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return json.loads(__import__("base64").b64decode(data["content"]))


def _normalise(license_id: Optional[str]) -> str:
    if not license_id:
        return ""
    return license_id.strip().lower()


def _contains_prohibited_term(text: Optional[str], terms) -> bool:
    if not text:
        return False
    low = text.lower()
    for term in terms:
        if term.lower() in low:
            return True
    return False


def evaluate(record: dict, policy: dict) -> Tuple[str, str, str]:
    """Return (status, reason, rule_basis) for a single catalog record.

    `record` expected keys: id, license, readme_license, title, ...
    """
    lic = _normalise(record.get("license"))
    readme_lic = _normalise(record.get("readme_license"))
    title = record.get("title") or ""

    allowed = {k.lower() for k in policy.get("allowed_licenses", {})}
    prohibited = {k.lower(): v for k, v in policy.get("prohibited_licenses", {}).items()}
    review = {k.lower(): v for k, v in policy.get("needs_review", {}).items()}
    prohibited_terms = policy.get("prohibited_terms", [])
    readme_mismatch_treatment = policy.get("readme_mismatch_treatment", "review")

    # --- missing licence -------------------------------------------------
    if not lic:
        basis = f"policy.missing_license_treatment={policy.get('missing_license_treatment')}"
        return STATUS_MISSING, "No licence tag declared on the dataset record.", basis

    # --- prohibited licence ----------------------------------------------
    if lic in prohibited:
        basis = f"policy.prohibited_licenses['{lic}']"
        return STATUS_VIOLATION, prohibited[lic], basis

    # --- prohibited terms in title / readme ------------------------------
    combined = f"{title} {readme_lic}"
    if _contains_prohibited_term(combined, prohibited_terms):
        basis = "policy.prohibited_terms"
        return STATUS_VIOLATION, f"Prohibited term detected: {combined}", basis

    # --- needs-review licence --------------------------------------------
    if lic in review:
        basis = f"policy.needs_review['{lic}']"
        return STATUS_NEEDS_REVIEW, review[lic], basis

    # --- README declaration vs tag mismatch (change) ---------------------
    if readme_lic and readme_lic != lic:
        basis = "policy.review_rules.readme_license_mismatch"
        return STATUS_CHANGED, (
            f"README card declares '{readme_lic}' but the licence tag is '{lic}'. "
            "Licence may have changed; human review required."
        ), basis

    # --- allowed ---------------------------------------------------------
    if lic in allowed:
        basis = f"policy.allowed_licenses['{lic}']"
        return STATUS_COMPLIANT, "Licence is allowed by policy.", basis

    # --- unknown licence -------------------------------------------------
    basis = "policy.needs_review (unknown licence)"
    return STATUS_NEEDS_REVIEW, f"Licence '{lic}' is not enumerated by policy; review required.", basis
