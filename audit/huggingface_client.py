"""Hugging Face API client for the Oceania government license audit.

Implements:
- Paginated traversal of the datasets API and the datasets-server /rows API.
- Exponential backoff retry on transient 5xx errors and HTTP 429 rate limits
  (honouring the Retry-After header).
- A token-bucket style rate limiter to stay under API rate limits.
- README license declaration extraction.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import time
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import requests

log = logging.getLogger("hf")

USER_AGENT = "oceania-gov-license-hourly-audit/1.0 (+https://github.com/atool3800-stack/project_20260813_004054_cff38d45)"


class RateLimiter:
    """Very simple token-bucket rate limiter (requests per minute)."""

    def __init__(self, requests_per_minute: int = 600):
        self.min_interval = 60.0 / max(1, requests_per_minute)
        self._next = 0.0

    def wait(self):
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(self._next, now) + self.min_interval


class HFClient:
    def __init__(self, api_base: str, datasets_server_base: str,
                 token: Optional[str] = None, retry_cfg: Optional[dict] = None,
                 rate: RateLimiter = None, timeout: int = 30):
        self.api_base = api_base.rstrip("/")
        self.ds_base = datasets_server_base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retry_cfg = retry_cfg or {"max_attempts": 5, "base_delay_seconds": 2, "max_delay_seconds": 60}
        self.rate = rate or RateLimiter(600)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------ #
    # low-level request with retry + rate limit handling
    # ------------------------------------------------------------------ #
    def _request(self, method: str, url: str, params: Optional[dict] = None,
                 timeout: Optional[int] = None, stream: bool = False):
        """GET/HEAD request with exponential backoff and rate-limit handling."""
        timeout = timeout or self.timeout
        cfg = self.retry_cfg
        attempts = max(1, cfg.get("max_attempts", 5))
        base = max(0.5, cfg.get("base_delay_seconds", 2))
        cap = cfg.get("max_delay_seconds", 60)

        last_exc = None
        for attempt in range(1, attempts + 1):
            self.rate.wait()
            try:
                resp = self.session.request(method, url, params=params, timeout=timeout, stream=stream)
            except requests.RequestException as exc:
                last_exc = exc
                delay = min(cap, base * (2 ** (attempt - 1)))
                log.warning("request error %s attempt %d/%d: %s; retrying in %.1fs",
                            url, attempt, attempts, exc, delay)
                time.sleep(delay)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                delay = min(cap, base * (2 ** (attempt - 1)))
                # honour Retry-After if present
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = max(delay, int(ra))
                log.warning("HTTP %d on %s attempt %d/%d; retrying in %.1fs",
                            resp.status_code, url, attempt, attempts, delay)
                time.sleep(delay)
                if attempt == attempts:
                    resp.raise_for_status()
                continue
            return resp
        raise last_exc if last_exc else RuntimeError(f"failed: {url}")

    # ------------------------------------------------------------------ #
    # dataset enumeration (paginated)
    # ------------------------------------------------------------------ #
    def list_datasets(self, author: str = None, search: str = None,
                      limit: int = 100, max_pages: Optional[int] = None) -> Iterator[dict]:
        """Paginate GET /api/datasets. The HF API supports limit + offset (or cursor)."""
        per_page = max(1, min(limit, 100))
        offset = 0
        pages = 0
        while True:
            params = {"limit": per_page, "offset": offset}
            if author:
                params["author"] = author
            if search:
                params["search"] = search
            resp = self._request("GET", f"{self.api_base}/datasets", params=params)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for item in batch:
                yield item
            pages += 1
            if max_pages and pages >= max_pages:
                break
            if len(batch) < per_page:
                break
            offset += per_page
            log.debug("datasets page %d (offset %d) yielded %d", pages, offset, len(batch))

    # ------------------------------------------------------------------ #
    # dataset metadata
    # ------------------------------------------------------------------ #
    def get_dataset_metadata(self, repo_id: str) -> dict:
        url = f"{self.api_base}/datasets/{repo_id}"
        resp = self._request("GET", url, params={"full": "true"})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # README / license declaration
    # ------------------------------------------------------------------ #
    def get_readme(self, repo_id: str, filename: str = "README.md") -> Optional[str]:
        """Fetch the dataset card README text (raw), returns None if missing."""
        url = f"{self.api_base}/datasets/{repo_id}/README"
        try:
            resp = self._request("GET", url)
        except requests.HTTPError:
            return None
        if resp.status_code == 200:
            return resp.text
        return None

    # ------------------------------------------------------------------ #
    # catalog rows: server-side pagination with raw-file fallback
    # ------------------------------------------------------------------ #
    def iter_catalog_rows(self, repo_id: str, catalog_filename: str = "catalog.csv",
                          page_size: int = 1000, prefer_rows_api: bool = True) -> Iterator[dict]:
        """Yield one dict per catalog row across the whole file (pagination).

        Primary path  : datasets-server /rows API (config=default, split=train) with
                        offset/limit pagination.
        Fallback path : chunked read of the raw CSV file, resuming at the exact row
                        where the API stopped so no rows are duplicated or skipped.
        """
        seen = 0
        if prefer_rows_api:
            try:
                for row in self._iter_rows_api(repo_id, page_size):
                    seen += 1
                    yield row
                return  # completed fully via the API
            except StopIteration:
                return
            except Exception as exc:  # pragma: no cover - depends on remote
                log.warning("datasets-server rows API failed for %s after %d rows (%s); "
                            "continuing via raw-file pagination", repo_id, seen, exc)
        yield from self._iter_raw_csv(repo_id, catalog_filename, page_size, skip_rows=seen)

    def _iter_rows_api(self, repo_id: str, page_size: int) -> Iterator[dict]:
        # datasets-server clamps limit to 100 rows per request; paginate with offset.
        # This shared service is frequently rate-limited, so we use a small retry
        # budget and raise quickly; the caller falls back to raw-file pagination.
        page_size = max(1, min(page_size, 100))
        offset = 0
        while True:
            resp = self._rows_api_get(repo_id, offset, page_size)
            payload = resp.json()
            rows = payload.get("rows", [])
            if not rows:
                break
            for row in rows:
                yield row.get("row") or {}
            offset += len(rows)
            log.info("%s rows page offset=%d count=%d", repo_id, offset, len(rows))
            if len(rows) < page_size:
                break

    def _rows_api_get(self, repo_id: str, offset: int, page_size: int):
        """One rows-API request with a short retry budget (then raise to fallback)."""
        url = f"{self.ds_base}/rows"
        params = {"dataset": repo_id, "config": "default", "split": "train",
                  "offset": offset, "limit": page_size}
        # Single probe attempt: datasets-server is frequently rate-limited, so we
        # fall back to raw-file pagination quickly rather than burning time/retries.
        self.rate.wait()
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise exc
        if resp.status_code in (429, 500, 502, 503, 504):
            raise requests.HTTPError(f"rows API HTTP {resp.status_code}", response=resp)
        if resp.status_code == 200:
            return resp
        resp.raise_for_status()

    def _iter_raw_csv(self, repo_id: str, filename: str, page_size: int,
                      skip_rows: int = 0) -> Iterator[dict]:
        """Chunked read of the raw CSV (handles gzip). `skip_rows` lets the
        fallback resume where the rows API stopped without duplicating rows."""
        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{filename}"
        resp = self._request("GET", url, timeout=120, stream=True)
        resp.raise_for_status()
        if "gzip" in resp.headers.get("Content-Encoding", ""):
            raw = io.TextIOWrapper(gzip.GzipFile(fileobj=resp.raw), encoding="utf-8", newline="")
        else:
            raw = io.TextIOWrapper(resp.raw, encoding="utf-8", newline="")
        reader = csv.DictReader(raw)
        if not reader.fieldnames:
            return
        skipped = 0
        page = []
        for row in reader:
            if skipped < skip_rows:
                skipped += 1
                continue
            page.append(row)
            if len(page) >= page_size:
                yield from page
                page = []
        if page:
            yield from page

    def close(self):
        self.session.close()
