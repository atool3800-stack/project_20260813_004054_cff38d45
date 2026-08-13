"""GitHub API client used by the hourly Oceania government license audit.

Implements:
- Label creation (idempotent).
- Paginated issue listing (for dedupe / create-or-update).
- Issue create / update / close.
- Exponential backoff retry and rate-limit handling (honouring Retry-After,
  and the X-RateLimit-* headers returned by GitHub).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Iterator, List, Optional

import requests

log = logging.getLogger("gh")


class GHClient:
    def __init__(self, token: str, api_base: str = "https://api.github.com",
                 retry_cfg: Optional[dict] = None, timeout: int = 30):
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retry_cfg = retry_cfg or {"max_attempts": 5, "base_delay_seconds": 2, "max_delay_seconds": 60}
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oceania-gov-license-hourly-audit/1.0",
        })

    # ------------------------------------------------------------------ #
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        cfg = self.retry_cfg
        attempts = max(1, cfg.get("max_attempts", 5))
        base = max(0.5, cfg.get("base_delay_seconds", 2))
        cap = cfg.get("max_delay_seconds", 60)

        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(min(cap, base * (2 ** (attempt - 1))))
                continue

            # secondary rate limit / server errors -> backoff
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                delay = min(cap, base * (2 ** (attempt - 1)))
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = max(delay, int(ra))
                # primary rate limit: sleep until reset if provided
                if resp.status_code == 403 and "x-ratelimit-reset" in resp.headers:
                    reset = int(resp.headers["x-ratelimit-reset"])
                    sleep_s = max(delay, reset - int(time.time()) + 1)
                    if sleep_s > 0 and sleep_s < 600:
                        log.warning("GitHub rate limit: sleeping %.0fs", sleep_s)
                        time.sleep(sleep_s)
                        continue
                log.warning("HTTP %d on %s attempt %d/%d; retrying in %.1fs",
                            resp.status_code, url, attempt, attempts, delay)
                time.sleep(delay)
                if attempt == attempts:
                    resp.raise_for_status()
                continue
            return resp
        raise last_exc if last_exc else RuntimeError(f"failed: {url}")

    # ------------------------------------------------------------------ #
    # labels
    # ------------------------------------------------------------------ #
    def ensure_label(self, owner: str, repo: str, name: str,
                     color: str = "b60205", description: str = "") -> bool:
        url = f"{self.api_base}/repos/{owner}/{repo}/labels/{name}"
        resp = self._request("GET", url)
        if resp.status_code == 200:
            return True
        url = f"{self.api_base}/repos/{owner}/{repo}/labels"
        resp = self._request("POST", url, json={"name": name, "color": color, "description": description})
        if resp.status_code in (200, 201):
            log.info("created label %s on %s/%s", name, owner, repo)
            return True
        log.warning("could not ensure label %s: HTTP %d", name, resp.status_code)
        return False

    # ------------------------------------------------------------------ #
    # issues
    # ------------------------------------------------------------------ #
    def iter_issues_with_label(self, owner: str, repo: str, label: str,
                               state: str = "open", per_page: int = 100) -> Iterator[dict]:
        page = 1
        while True:
            url = f"{self.api_base}/repos/{owner}/{repo}/issues"
            resp = self._request("GET", url, params={
                "labels": label, "state": state, "per_page": per_page, "page": page,
            })
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for item in batch:
                if "pull_request" in item:  # skip PRs
                    continue
                yield item
            if len(batch) < per_page:
                break
            page += 1

    def create_issue(self, owner: str, repo: str, title: str, body: str,
                     labels: Optional[List[str]] = None) -> dict:
        url = f"{self.api_base}/repos/{owner}/{repo}/issues"
        resp = self._request("POST", url, json={"title": title, "body": body, "labels": labels or []})
        resp.raise_for_status()
        return resp.json()

    def update_issue(self, owner: str, repo: str, number: int, body: str = None,
                     state: str = None) -> dict:
        url = f"{self.api_base}/repos/{owner}/{repo}/issues/{number}"
        payload = {}
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        resp = self._request("PATCH", url, json=payload)
        resp.raise_for_status()
        return resp.json()

    def close_issue(self, owner: str, repo: str, number: int, comment: str = None) -> dict:
        if comment:
            self.add_comment(owner, repo, number, comment)
        return self.update_issue(owner, repo, number, state="closed")

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        url = f"{self.api_base}/repos/{owner}/{repo}/issues/{number}/comments"
        resp = self._request("POST", url, json={"body": body})
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self.session.close()
