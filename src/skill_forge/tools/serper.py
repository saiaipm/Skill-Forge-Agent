"""Serper.dev search tools.

Hand-rolled rather than imported from ``crewai-tools`` for two reasons. The
practical one: ``crewai-tools`` pins ``crewai==1.15.6``, which hard-requires
``lancedb``, which ships no macOS x86_64 wheels — it cannot be installed on an
Intel Mac at all. The better one: the Course Curator and the Media Miner want
different things from search, and owning the client lets each get a tool shaped
for its job instead of a single generic ``search(query)``.

Serper exposes several endpoints on the same host and key; ``/search`` returns
organic web results and ``/videos`` returns video results (predominantly
YouTube). Both are wrapped below.
"""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

SERPER_HOST = "https://google.serper.dev"
DEFAULT_TIMEOUT = 15
SNIPPET_CHARS = 140


def _clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Trim a snippet to its first useful clause."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean_field(value: object) -> str | None:
    """Drop junk metadata rather than passing it through to the document.

    Serper occasionally returns scraped placeholders — an observed run produced
    the channel name "hUndefined", which the agent faithfully copied into the
    final roadmap. Omitting the field is better than printing nonsense.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text or "undefined" in text.lower() or text.lower() in {"null", "none", "n/a"}:
        return None
    return text


class SerperError(RuntimeError):
    """Raised when Serper cannot be reached or rejects the request."""


def _api_key() -> str:
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key or any(m in key.lower() for m in ("xxx", "your-", "<your")):
        raise SerperError(
            "SERPER_API_KEY is not set (or still holds the .env.example "
            "placeholder). Get a free key at https://serper.dev — 2,500 "
            "queries, no card required — and put it in .env."
        )
    return key


def _post(endpoint: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    """One HTTP call to Serper, with errors translated into something readable.

    Agents surface tool exceptions back into their own context, so the message
    text here is effectively a prompt: it should tell the model what to do next,
    not just what went wrong.
    """
    try:
        response = requests.post(
            f"{SERPER_HOST}/{endpoint}",
            headers={"X-API-KEY": _api_key(), "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.Timeout as e:
        raise SerperError(
            f"Serper timed out after {timeout}s. Retry once with a shorter query."
        ) from e
    except requests.RequestException as e:
        raise SerperError(f"Could not reach Serper: {e}") from e

    if response.status_code == 401:
        raise SerperError("Serper rejected the API key (401). Check SERPER_API_KEY.")
    if response.status_code == 429:
        raise SerperError(
            "Serper rate limit or quota exhausted (429). Stop searching and "
            "work with the results already gathered."
        )
    if response.status_code >= 400:
        raise SerperError(f"Serper returned {response.status_code}: {response.text[:200]}")

    try:
        return response.json()
    except json.JSONDecodeError as e:
        raise SerperError(f"Serper returned malformed JSON: {e}") from e


# --------------------------------------------------------------------------- #
# Web search
# --------------------------------------------------------------------------- #


class WebSearchInput(BaseModel):
    query: str = Field(description="The search query. Be specific — include the "
                                   "technology name and what you want, e.g. "
                                   "'free Kubernetes course edX' rather than 'k8s'.")
    num_results: int = Field(
        default=8, ge=1, le=20, description="How many organic results to return."
    )


class SerperWebSearchTool(BaseTool):
    """Google web search via Serper, returned as compact text.

    Results are formatted as plain text rather than raw JSON: the agent has to
    read them, and a wall of JSON burns context on punctuation. Each result is
    three lines — title, URL, snippet — which is what the curator agents
    actually need to judge relevance.
    """

    name: str = "web_search"
    description: str = (
        "Search the web for courses, documentation, articles, or certifications. "
        "Returns titles, URLs, and snippets. Use specific queries naming the "
        "technology and the kind of resource wanted."
    )
    args_schema: type[BaseModel] = WebSearchInput
    timeout: int = DEFAULT_TIMEOUT

    #: Domains that dominate search results for course queries but are content
    #: farms or aggregators rather than the reputable platforms the design doc
    #: requires. Filtered before the agent ever sees them.
    BLOCKED_DOMAINS: ClassVar[frozenset[str]] = frozenset(
        {
            "coursesity.com",
            "reddit.com",
            "quora.com",
            "medium.com/@",  # personal reposts; the engineering blogs are fine
            "classcentral.com/report",
            "pinterest.com",
            "facebook.com",
            "slideshare.net",
            "scribd.com",
            "coursedaddy.com",
            "freecoursesite.com",
            "tutorialspoint.com/videotutorials",
        }
    )

    def _run(self, query: str, num_results: int = 8) -> str:
        data = _post("search", {"q": query, "num": num_results}, self.timeout)
        organic = data.get("organic", []) or []

        kept = [
            r
            for r in organic
            if not any(b in (r.get("link") or "") for b in self.BLOCKED_DOMAINS)
        ]
        if not kept:
            return (
                f"No usable results for {query!r}. Try different wording — name "
                "the platform (edX, Coursera, freeCodeCamp) or the exact topic."
            )

        lines = [f"{len(kept)} results for {query!r}:"]
        for i, r in enumerate(kept[:num_results], 1):
            lines.append(f"{i}. {r.get('title', 'Untitled')}")
            lines.append(f"   {r.get('link', '')}")
            # Snippets are clipped: Google returns up to ~300 characters and the
            # tail is almost always boilerplate. The first sentence carries the
            # signal an agent needs to judge relevance.
            if snippet := r.get("snippet"):
                lines.append(f"   {_clip(snippet)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Video search
# --------------------------------------------------------------------------- #


class VideoSearchInput(BaseModel):
    query: str = Field(description="Video search query, e.g. 'Kubernetes full "
                                   "course for beginners'.")
    num_results: int = Field(default=6, ge=1, le=20)


class SerperVideoSearchTool(BaseTool):
    """Video search via Serper's /videos endpoint.

    Serper's video index is overwhelmingly YouTube, which is what the Media
    Mining agent wants. Channel and duration come back when Google exposes
    them; both are optional in the schema precisely because they often don't.
    """

    name: str = "video_search"
    description: str = (
        "Search for video tutorials, playlists, and full courses (mostly "
        "YouTube). Returns titles, URLs, channel names, and durations where "
        "available. Use for finding walkthroughs and long-form courses."
    )
    args_schema: type[BaseModel] = VideoSearchInput
    timeout: int = DEFAULT_TIMEOUT

    def _run(self, query: str, num_results: int = 6) -> str:
        data = _post("videos", {"q": query, "num": num_results}, self.timeout)
        videos = data.get("videos", []) or []
        if not videos:
            return (
                f"No videos found for {query!r}. Try broader wording, e.g. drop "
                "version numbers or use 'tutorial' instead of a specific task."
            )

        lines = [f"{len(videos)} videos for {query!r}:"]
        for i, v in enumerate(videos[:num_results], 1):
            lines.append(f"{i}. {v.get('title', 'Untitled')}")
            lines.append(f"   {v.get('link', '')}")
            meta = [
                cleaned
                for key in ("channel", "duration", "date")
                if (cleaned := _clean_field(v.get(key)))
            ]
            if meta:
                lines.append(f"   {' · '.join(meta)}")
        return "\n".join(lines)
