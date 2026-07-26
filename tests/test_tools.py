"""Tool tests. No network — every HTTP call is stubbed.

The interesting cases here are the ones that look like failures but are not:
a 405 from a site that dislikes HEAD, a 403 from bot protection. Both must NOT
be reported as dead links, because doing so silently strips the reputable
platforms the design doc requires.
"""

from __future__ import annotations

import json

import pytest
import requests

from skill_forge.tools.link_verifier import (
    LinkStatus,
    LinkVerifierTool,
    check_url,
    check_urls,
)
from skill_forge.tools.serper import (
    SerperError,
    SerperVideoSearchTool,
    SerperWebSearchTool,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload

    def close(self):
        pass


@pytest.fixture
def serper_key(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "test-key-not-a-placeholder")


@pytest.fixture(autouse=True)
def link_verify_on(monkeypatch):
    monkeypatch.setenv("SKIP_LINK_VERIFY", "0")
    monkeypatch.setenv("LINK_VERIFY_TIMEOUT", "3")


# --------------------------------------------------------------------------- #
# Serper — web search
# --------------------------------------------------------------------------- #


def test_web_search_formats_results_for_reading(monkeypatch, serper_key):
    payload = {
        "organic": [
            {
                "title": "Introduction to Kubernetes",
                "link": "https://www.edx.org/course/intro-k8s",
                "snippet": "Free course from the Linux Foundation.",
            }
        ]
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, payload))
    out = SerperWebSearchTool()._run("kubernetes free course")
    assert "Introduction to Kubernetes" in out
    assert "https://www.edx.org/course/intro-k8s" in out
    assert "Linux Foundation" in out


def test_web_search_filters_content_farms(monkeypatch, serper_key):
    payload = {
        "organic": [
            {"title": "Reddit thread", "link": "https://reddit.com/r/kubernetes/x"},
            {"title": "Real course", "link": "https://www.coursera.org/learn/k8s"},
        ]
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, payload))
    out = SerperWebSearchTool()._run("kubernetes course")
    assert "coursera.org" in out
    assert "reddit.com" not in out


def test_web_search_with_no_usable_results_tells_the_agent_what_to_do(
    monkeypatch, serper_key
):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse(200, {"organic": []})
    )
    out = SerperWebSearchTool()._run("asdfqwerzxcv")
    assert "No usable results" in out
    assert "Try different wording" in out


def test_missing_api_key_explains_how_to_fix_it(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "xxxxxxxxxxxx")
    with pytest.raises(SerperError, match="serper.dev"):
        SerperWebSearchTool()._run("anything")


def test_rate_limit_tells_the_agent_to_stop_searching(monkeypatch, serper_key):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(429, text="slow down"))
    with pytest.raises(SerperError, match="Stop searching"):
        SerperWebSearchTool()._run("kubernetes")


def test_bad_key_is_reported_as_such(monkeypatch, serper_key):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(401, text="nope"))
    with pytest.raises(SerperError, match="rejected the API key"):
        SerperWebSearchTool()._run("kubernetes")


def test_timeout_is_surfaced_with_retry_advice(monkeypatch, serper_key):
    def boom(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(SerperError, match="Retry once"):
        SerperWebSearchTool()._run("kubernetes")


# --------------------------------------------------------------------------- #
# Serper — video search
# --------------------------------------------------------------------------- #


def test_video_search_includes_channel_and_duration(monkeypatch, serper_key):
    payload = {
        "videos": [
            {
                "title": "Kubernetes Full Course",
                "link": "https://www.youtube.com/watch?v=abc",
                "channel": "TechWorld with Nana",
                "duration": "4:12:00",
                "date": "2 years ago",
            }
        ]
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, payload))
    out = SerperVideoSearchTool()._run("kubernetes full course")
    assert "TechWorld with Nana" in out
    assert "4:12:00" in out


def test_video_search_survives_missing_optional_metadata(monkeypatch, serper_key):
    """Google frequently omits channel and duration; that must not crash."""
    payload = {"videos": [{"title": "Some video", "link": "https://youtu.be/x"}]}
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, payload))
    out = SerperVideoSearchTool()._run("kubernetes")
    assert "Some video" in out


# --------------------------------------------------------------------------- #
# Link verifier
# --------------------------------------------------------------------------- #


def test_healthy_url_is_alive(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(200))
    assert check_url("https://example.com").status is LinkStatus.ALIVE


def test_404_is_dead(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(404))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(404))
    r = check_url("https://example.com/gone")
    assert r.status is LinkStatus.DEAD
    assert r.http_code == 404


def test_head_405_falls_back_to_get(monkeypatch):
    """Udemy and Coursera reject HEAD but serve GET — must not be marked dead."""
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(405))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200))
    assert check_url("https://www.udemy.com/course/x/").status is LinkStatus.ALIVE


def test_bot_protection_is_unverified_not_dead(monkeypatch):
    """A 403 says the checker was blocked, not that the page is missing."""
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(403))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(403))
    r = check_url("https://www.cloudflare-protected.com/course")
    assert r.status is LinkStatus.UNVERIFIED
    assert "bot protection" in r.detail


def test_timeout_is_unverified_not_dead(monkeypatch):
    def slow(*a, **k):
        raise requests.Timeout()

    monkeypatch.setattr(requests, "head", slow)
    monkeypatch.setattr(requests, "get", slow)
    assert check_url("https://slow.example.com").status is LinkStatus.UNVERIFIED


def test_nonexistent_domain_is_dead(monkeypatch):
    def dns_fail(*a, **k):
        raise requests.ConnectionError("nodename nor servname provided")

    monkeypatch.setattr(requests, "head", dns_fail)
    monkeypatch.setattr(requests, "get", dns_fail)
    r = check_url("https://this-domain-does-not-exist-xyz.com")
    assert r.status is LinkStatus.DEAD
    assert "does not resolve" in r.detail


def test_non_http_scheme_is_rejected_without_a_request(monkeypatch):
    def should_not_run(*a, **k):
        raise AssertionError("no network call should be made")

    monkeypatch.setattr(requests, "head", should_not_run)
    assert check_url("ftp://example.com/file").status is LinkStatus.DEAD


def test_check_urls_dedupes_and_preserves_order(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(200))
    urls = ["https://a.com", "https://b.com", "https://a.com"]
    results = check_urls(urls)
    assert [r.url for r in results] == ["https://a.com", "https://b.com"]


def test_tool_summarises_and_flags_dead_links(monkeypatch):
    def head(url, *a, **k):
        return FakeResponse(404 if "dead" in url else 200)

    monkeypatch.setattr(requests, "head", head)
    monkeypatch.setattr(requests, "get", lambda url, *a, **k: FakeResponse(404))
    out = LinkVerifierTool()._run(["https://ok.com", "https://dead.com"])
    assert "1 ALIVE" in out and "1 DEAD" in out
    assert "REPLACE these" in out
    assert "https://dead.com" in out


def test_healthy_urls_are_not_echoed_back(monkeypatch):
    """Token economy: on a per-day-metered free tier, echoing 25 healthy URLs
    back at the agent is the difference between three runs and ten."""
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(200))
    urls = [f"https://example{i}.com" for i in range(25)]
    out = LinkVerifierTool()._run(urls)
    assert "25 ALIVE" in out
    assert "All reachable" in out
    assert "example7.com" not in out  # no per-URL echo
    assert len(out) < 200


def test_verification_can_be_disabled_for_fast_iteration(monkeypatch):
    monkeypatch.setenv("SKIP_LINK_VERIFY", "1")

    def should_not_run(*a, **k):
        raise AssertionError("no network call when verification is disabled")

    monkeypatch.setattr(requests, "head", should_not_run)
    out = LinkVerifierTool()._run(["https://example.com"])
    assert "disabled" in out


def test_empty_url_list_is_handled():
    assert "No URLs supplied" in LinkVerifierTool()._run([])
