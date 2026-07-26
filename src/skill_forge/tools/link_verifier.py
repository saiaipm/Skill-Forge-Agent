"""Dead-link verification (design doc QA rule #1).

Search engines index pages that have since moved or died, and a model will
cheerfully invent a plausible-looking course URL. Both failure modes produce
the same symptom — a roadmap full of 404s — so every URL is checked before it
reaches the final document.

Two deliberate design choices:

**HEAD first, GET as fallback.** HEAD is cheap, but a meaningful number of
sites (Udemy and Coursera among them) return 405 or 403 to HEAD while serving
GET fine. Treating a failed HEAD as a dead link would strip out exactly the
reputable platforms the design doc requires.

**A bot-block is not a dead link.** Cloudflare and friends return 403 to
anything without a browser fingerprint. That says nothing about whether the
page exists, so 403 is reported as UNVERIFIED rather than DEAD — the agent is
told to use its judgement instead of being told the URL is broken.
"""

from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

DEFAULT_TIMEOUT = 6
MAX_PARALLEL = 8

#: Without a realistic UA, a large share of sites 403 immediately.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class LinkStatus(str, Enum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"
    #: Reachable but the server refused to confirm — bot protection, rate
    #: limiting, or a redirect loop. Explicitly not a verdict of "broken".
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class LinkResult:
    url: str
    status: LinkStatus
    http_code: int | None
    detail: str

    def render(self) -> str:
        code = f" [{self.http_code}]" if self.http_code else ""
        return f"{self.status.value}{code}  {self.url}  — {self.detail}"


def _timeout() -> int:
    try:
        return int(os.getenv("LINK_VERIFY_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


def check_url(url: str, timeout: int | None = None) -> LinkResult:
    """Resolve a single URL to ALIVE, DEAD, or UNVERIFIED."""
    timeout = timeout or _timeout()
    headers = {"User-Agent": BROWSER_UA}

    if not url.lower().startswith(("http://", "https://")):
        return LinkResult(url, LinkStatus.DEAD, None, "not an http(s) URL")

    def classify(code: int) -> LinkResult | None:
        if 200 <= code < 300:
            return LinkResult(url, LinkStatus.ALIVE, code, "reachable")
        if code in (401, 403, 429):
            return LinkResult(
                url, LinkStatus.UNVERIFIED, code, "blocked by bot protection or rate limit"
            )
        if code >= 400:
            return LinkResult(url, LinkStatus.DEAD, code, "server returned an error")
        return None

    try:
        head = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        # 405/501 mean "HEAD unsupported", not "page missing" — fall through to GET.
        if head.status_code not in (403, 405, 501):
            if (result := classify(head.status_code)) is not None:
                return result
    except requests.RequestException:
        pass  # fall through to GET

    try:
        get = requests.get(
            url, timeout=timeout, allow_redirects=True, headers=headers, stream=True
        )
        get.close()
        if (result := classify(get.status_code)) is not None:
            return result
        return LinkResult(url, LinkStatus.UNVERIFIED, get.status_code, "unexpected status")
    except requests.Timeout:
        return LinkResult(url, LinkStatus.UNVERIFIED, None, f"timed out after {timeout}s")
    except requests.TooManyRedirects:
        return LinkResult(url, LinkStatus.UNVERIFIED, None, "redirect loop")
    except requests.ConnectionError as e:
        # DNS failure means the host does not exist — that is genuinely dead.
        text = str(e).lower()
        if "name or service not known" in text or "nodename nor servname" in text:
            return LinkResult(url, LinkStatus.DEAD, None, "domain does not resolve")
        return LinkResult(url, LinkStatus.UNVERIFIED, None, "connection failed")
    except requests.RequestException as e:
        return LinkResult(url, LinkStatus.UNVERIFIED, None, f"{type(e).__name__}")


def check_urls(urls: list[str], timeout: int | None = None) -> list[LinkResult]:
    """Check many URLs concurrently, preserving input order.

    Sequential checking dominates pipeline runtime once there are 40+ links, and
    these are pure network waits, so threads are the right tool.
    """
    unique = list(dict.fromkeys(urls))
    if not unique:
        return []

    results: dict[str, LinkResult] = {}
    workers = min(MAX_PARALLEL, len(unique))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_url, u, timeout): u for u in unique}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as e:  # a worker should never escape, but never trust that
                results[url] = LinkResult(
                    url, LinkStatus.UNVERIFIED, None, f"checker error: {type(e).__name__}"
                )
    return [results[u] for u in unique]


def verification_enabled() -> bool:
    """Allow live checks to be switched off for fast local iteration."""
    return os.getenv("SKIP_LINK_VERIFY", "0").strip() not in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# Agent-facing tool
# --------------------------------------------------------------------------- #


class LinkVerifierInput(BaseModel):
    urls: list[str] = Field(
        description="URLs to verify. Pass every URL you intend to recommend, "
                    "in one call — they are checked in parallel."
    )


class LinkVerifierTool(BaseTool):
    """Lets an agent confirm its recommendations resolve before committing them."""

    name: str = "verify_links"
    description: str = (
        "Check that URLs are reachable before recommending them. Returns ALIVE, "
        "DEAD, or UNVERIFIED per URL. Replace anything DEAD. UNVERIFIED means "
        "the site blocked automated checking, not that the link is broken — "
        "keep those if the source is reputable."
    )
    args_schema: type[BaseModel] = LinkVerifierInput

    def _run(self, urls: list[str]) -> str:
        if not urls:
            return "No URLs supplied."

        if not verification_enabled():
            return (
                f"Link verification is disabled (SKIP_LINK_VERIFY=1). "
                f"{len(urls)} URLs assumed reachable."
            )

        results = check_urls(urls)
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))

        # Only the problems are listed. Echoing every healthy URL back at the
        # agent costs hundreds of tokens per call — on a free tier metered by
        # tokens per day, that is the difference between three runs and ten —
        # and tells it nothing it can act on. Silence means reachable.
        dead = [r for r in results if r.status is LinkStatus.DEAD]
        unverified = [r for r in results if r.status is LinkStatus.UNVERIFIED]

        lines = [f"Checked {len(results)} URLs: {summary}"]

        if dead:
            lines.append("")
            lines.append("REPLACE these — they do not resolve:")
            lines += [f"  {r.url} [{r.http_code or 'no response'}]" for r in dead]

        if unverified:
            lines.append("")
            lines.append("Blocked by bot protection, not broken — keep if reputable:")
            lines += [f"  {r.url}" for r in unverified]

        if not dead and not unverified:
            lines.append("All reachable. Proceed with these URLs.")

        return "\n".join(lines)
