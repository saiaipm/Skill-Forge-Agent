"""Tests for enforced link verification (design doc QA rule #1).

These defend the difference between "the agents were asked to verify links" and
"the links are verified". The distinction is not academic: a real run on
"Gen AI for Product Managers" hit CrewAI's max_iter ceiling, and the Media Miner
responded to "Requesting final answer" by inventing nine resources across phases
3 and 4 — including martinfowler.com/articles/scalable-ai.html and
hbr.org/2024/01/leading-ai-strategy, neither of which exists. It never called
verify_links, and nothing required it to.
"""

from __future__ import annotations

import pytest
import requests
from pydantic import ValidationError

from skill_forge.schemas import (
    Certification,
    CourseCatalog,
    MediaLibrary,
    YoutubeContent,
)
from skill_forge.verify import collect_urls, verify_and_prune

DEAD = "https://martinfowler.com/articles/scalable-ai.html"
ALSO_DEAD = "https://hbr.org/2024/01/leading-ai-strategy"


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def close(self):
        pass


@pytest.fixture(autouse=True)
def verification_on(monkeypatch):
    monkeypatch.setenv("SKIP_LINK_VERIFY", "0")
    monkeypatch.setenv("LINK_VERIFY_TIMEOUT", "2")


@pytest.fixture
def net(monkeypatch):
    """Every URL is alive except the two known-fabricated ones."""

    def respond(url, *a, **k):
        return FakeResponse(404 if url in {DEAD, ALSO_DEAD} else 200)

    monkeypatch.setattr(requests, "head", respond)
    monkeypatch.setattr(requests, "get", respond)


def _catalog() -> CourseCatalog:
    return CourseCatalog(
        curated_courses=[
            {
                "phase_id": f"phase_{i}",
                "free_courses": [{
                    "title": f"Free {i}", "provider": "freeCodeCamp",
                    "url": f"https://www.freecodecamp.org/news/c{i}/",
                    "cost_type": "Free"}],
                "paid_courses": [{
                    "title": f"Paid {i}", "provider": "Udemy",
                    "url": f"https://www.udemy.com/course/c{i}/",
                    "cost_type": "Paid", "price_range": "$15-$25"}],
            }
            for i in range(1, 5)
        ],
        certifications=[],
    )


def _media(extra_articles: dict[str, list[str]] | None = None) -> MediaLibrary:
    extra_articles = extra_articles or {}
    return MediaLibrary(
        media_resources=[
            {
                "phase_id": f"phase_{i}",
                "articles_and_blogs": [
                    {"title": f"Article {i}", "author_publisher": "Pro Git",
                     "url": f"https://git-scm.com/book/a{i}",
                     "key_takeaway": "Explains the model."}
                ] + [
                    {"title": "Invented", "author_publisher": "Nobody",
                     "url": u, "key_takeaway": "This resource does not exist."}
                    for u in extra_articles.get(f"phase_{i}", [])
                ],
            }
            for i in range(1, 5)
        ]
    )


# --------------------------------------------------------------------------- #
# The core guarantee
# --------------------------------------------------------------------------- #


def test_fabricated_links_are_removed(net):
    """The exact failure: invented resources in phases 3 and 4."""
    media = _media({"phase_3": [DEAD], "phase_4": [ALSO_DEAD]})
    _, pruned, report = verify_and_prune(_catalog(), media)

    surviving = {
        a.url for pm in pruned.media_resources for a in pm.articles_and_blogs
    }
    assert DEAD not in surviving
    assert ALSO_DEAD not in surviving
    assert sorted(report.dead_urls) == sorted([ALSO_DEAD, DEAD])
    assert len(report.removed) == 2


def test_healthy_payload_is_left_untouched(net):
    catalog, media = _catalog(), _media()
    pruned_catalog, pruned_media, report = verify_and_prune(catalog, media)
    assert report.dead_urls == []
    assert report.removed == []
    assert pruned_catalog == catalog
    assert pruned_media == media


def test_verification_runs_even_though_the_agent_had_a_verify_tool(net):
    """Verification is not conditional on the agent having done it.

    The whole point: an agent cut off by max_iter never calls its tool, and we
    cannot tell from the payload whether it did.
    """
    media = _media({"phase_3": [DEAD]})
    _, _, report = verify_and_prune(_catalog(), media)
    assert report.checked > 0
    assert report.dead_urls == [DEAD]


def test_collect_urls_covers_every_field_that_reaches_the_document():
    urls = collect_urls(_catalog(), _media({"phase_2": [DEAD]}))
    assert DEAD in urls
    assert any("freecodecamp" in u for u in urls)
    assert any("udemy" in u for u in urls)
    assert any("git-scm" in u for u in urls)
    assert len(urls) == len(set(urls)), "collect_urls should de-duplicate"


# --------------------------------------------------------------------------- #
# Pruning must never produce an invalid payload
# --------------------------------------------------------------------------- #


def test_a_phases_only_free_course_is_kept_and_flagged_rather_than_dropped(monkeypatch):
    """min_length=1 on free_courses is the design doc's hardest guarantee.

    Dropping the last free course would make the catalogue unconstructable, so
    the dead link stays and the operator is warned instead.
    """
    dead_course = "https://www.freecodecamp.org/news/c1/"

    def respond(url, *a, **k):
        return FakeResponse(404 if url == dead_course else 200)

    monkeypatch.setattr(requests, "head", respond)
    monkeypatch.setattr(requests, "get", respond)

    pruned, _, report = verify_and_prune(_catalog(), _media())
    still_there = {c.url for c in pruned.curated_courses[0].free_courses}
    assert dead_course in still_there
    assert any("only free course is unreachable" in w for w in report.warnings)


def test_a_certification_with_a_dead_url_is_dropped(monkeypatch):
    """An unverifiable credential cannot be shown to exist, so it goes."""
    cert_url = "https://www.cncf.io/certification/cka/"

    def respond(url, *a, **k):
        return FakeResponse(404 if url == cert_url else 200)

    monkeypatch.setattr(requests, "head", respond)
    monkeypatch.setattr(requests, "get", respond)

    catalog = _catalog()
    catalog.certifications.append(
        Certification(
            phase_id="phase_3",
            cert_name="Certified Kubernetes Administrator (CKA)",
            issuing_body="CNCF",
            relevance="High",
            official_url=cert_url,
        )
    )
    pruned, _, report = verify_and_prune(catalog, _media())
    assert pruned.certifications == []
    assert any("dropped certification" in w for w in report.warnings)


def test_bot_blocked_links_are_kept(monkeypatch):
    """403 means the checker was blocked, not that the page is missing."""
    monkeypatch.setattr(requests, "head", lambda *a, **k: FakeResponse(403))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(403))
    catalog, media = _catalog(), _media()
    pruned_c, pruned_m, report = verify_and_prune(catalog, media)
    assert report.dead_urls == []
    assert report.unverified > 0
    assert pruned_c == catalog


def test_verification_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SKIP_LINK_VERIFY", "1")

    def should_not_run(*a, **k):
        raise AssertionError("no network call when verification is disabled")

    monkeypatch.setattr(requests, "head", should_not_run)
    _, _, report = verify_and_prune(_catalog(), _media())
    assert report.skipped is True


# --------------------------------------------------------------------------- #
# YouTube IDs — fabrication HTTP checking cannot catch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=1a2b3c4d5e6f",   # 12 chars
        "https://www.youtube.com/watch?v=xyz123abc456",   # 12 chars
        "https://www.youtube.com/watch?v=policy123456",   # 12 chars
        "https://www.youtube.com/watch?v=short",          # too short
        "https://youtu.be/abc",                           # too short
    ],
)
def test_invented_youtube_ids_are_rejected(url):
    """YouTube answers an invalid ID with HTTP 200, so only format catches these."""
    with pytest.raises(ValidationError, match="not a valid YouTube video ID"):
        YoutubeContent(title="Invented talk", channel_name="Nobody",
                       url=url, format="Specific Video")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=zTjRZNkhiEU",
        "https://www.youtube.com/watch?v=8JJ101D3knE",
        "https://youtu.be/wDRoduig_98",
        "https://www.youtube.com/watch?v=wDRoduig_98&vl=en",
        "https://www.youtube.com/watch?list=PLxyz&v=zTjRZNkhiEU",
    ],
)
def test_real_youtube_urls_are_accepted(url):
    """Guards against a validator so strict it rejects genuine links."""
    YoutubeContent(title="Real talk", channel_name="freeCodeCamp.org",
                   url=url, format="Full Course")


def test_non_youtube_video_urls_are_left_alone():
    """The ID rule applies to YouTube only; other hosts have other schemes."""
    YoutubeContent(title="Conference talk", channel_name="InfoQ",
                   url="https://www.infoq.com/presentations/some-talk/",
                   format="Specific Video")
