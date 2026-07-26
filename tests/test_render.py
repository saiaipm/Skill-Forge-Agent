"""Renderer tests.

The renderer exists to guarantee the design doc's "no resource may be dropped"
rule structurally rather than by prompting, so the load-bearing test here is
the one asserting every input item appears in the output.
"""

from __future__ import annotations

import pytest

from skill_forge.render import (
    _heading_for,
    _split_numbered_steps,
    render_document,
)
from skill_forge.schemas import (
    CourseCatalog,
    MediaLibrary,
    Roadmap,
    RoadmapRequest,
)


def _phase(n: int, name: str) -> dict:
    return {
        "phase_id": f"phase_{n}",
        "phase_name": name,
        "estimated_hours": 20,
        "summary": (
            "A stage covering the listed concepts and building toward "
            "independent day-to-day competence with the tooling."
        ),
        "core_concepts": [
            f"Concept {n}A", f"Concept {n}B", f"Concept {n}C",
            f"Concept {n}D", f"Concept {n}E",
        ],
        "milestone_project": {
            "title": f"Project {n}",
            "description": (
                "Apply the concepts of this phase to a real repository and "
                "demonstrate the workflow end to end."
            ),
        },
    }


@pytest.fixture
def bundle():
    roadmap = Roadmap(
        roadmap_title="Zero to Hero: Git",
        target_domain="Git",
        total_estimated_weeks=8,
        weekly_hours=10,
        phases=[
            _phase(1, "Foundations"),
            _phase(2, "Building Confidence"),
            _phase(3, "Professional"),
            _phase(4, "Mastery"),
        ],
    )
    catalog = CourseCatalog(
        curated_courses=[
            {
                "phase_id": f"phase_{i}",
                "free_courses": [{
                    "title": f"Free Course {i}", "provider": "freeCodeCamp",
                    "url": f"https://www.freecodecamp.org/news/course-{i}/",
                    "cost_type": "Free"}],
                "paid_courses": [{
                    "title": f"Paid Course {i}", "provider": "Udemy",
                    "url": f"https://www.udemy.com/course/course-{i}/",
                    "cost_type": "Paid", "price_range": "$15-$25"}],
            }
            for i in range(1, 5)
        ],
        certifications=[{
            "phase_id": "phase_3",
            "cert_name": "Certified Kubernetes Administrator (CKA)",
            "issuing_body": "CNCF",
            "relevance": "High",
            "official_url": "https://www.cncf.io/training/certification/cka/",
            "prerequisites": "None",
        }],
    )
    media = MediaLibrary(
        media_resources=[
            {
                "phase_id": f"phase_{i}",
                "articles_and_blogs": [{
                    "title": f"Article {i}", "author_publisher": "Pro Git Book",
                    "url": f"https://git-scm.com/book/article-{i}",
                    "key_takeaway": "Explains the underlying model."}],
                "youtube_content": [{
                    "title": f"Video {i}", "channel_name": "freeCodeCamp.org",
                    "url": f"https://www.youtube.com/watch?v=zTjRZNkhiE{i}",
                    "format": "Full Course", "duration": "3:43:34"}],
                "interactive_or_docs": [{
                    "title": f"Docs {i}",
                    "url": f"https://git-scm.com/docs/{i}",
                    "type": "Documentation"}],
            }
            for i in range(1, 5)
        ]
    )
    request = RoadmapRequest(topic="Git", user_experience_level="Beginner",
                             target_time_commitment_hrs_per_week=10)
    return request, roadmap, catalog, media


# --------------------------------------------------------------------------- #
# The rule the renderer exists to enforce
# --------------------------------------------------------------------------- #


def test_no_resource_is_ever_dropped(bundle):
    """Design doc: synthesis MUST NOT drop any course, video, or certification."""
    request, roadmap, catalog, media = bundle
    doc = render_document(request, roadmap, catalog, media)

    for pc in catalog.curated_courses:
        for course in (*pc.free_courses, *pc.paid_courses):
            assert course.url in doc, f"dropped course {course.title}"
            assert course.title in doc
    for cert in catalog.certifications:
        assert cert.cert_name in doc
    for pm in media.media_resources:
        for item in (*pm.articles_and_blogs, *pm.youtube_content,
                     *pm.interactive_or_docs):
            assert item.url in doc, f"dropped resource {item.title}"


def test_every_phase_and_concept_appears(bundle):
    request, roadmap, catalog, media = bundle
    doc = render_document(request, roadmap, catalog, media)
    for phase in roadmap.phases:
        assert phase.phase_name in doc
        assert phase.milestone_project.title in doc
        for concept in phase.core_concepts:
            assert concept in doc


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def test_phase_name_is_not_doubled_when_it_matches_the_stage(bundle):
    """Regression: rendered as "Foundations — Foundations"."""
    _, roadmap, _, _ = bundle
    # Exact match: subtitle suppressed.
    assert _heading_for(roadmap.phases[0]) == "Foundations"
    # Contains the stage word already: still suppressed, no "Building" twice.
    assert _heading_for(roadmap.phases[1]) == "Building Confidence"


def test_stage_label_is_appended_when_it_adds_information():
    from skill_forge.schemas import Phase

    phase = Phase.model_validate(_phase(2, "Remote Collaboration"))
    assert _heading_for(phase) == "Remote Collaboration — Building"


def test_numbered_steps_are_split_onto_separate_lines():
    """Regression: getting_started rendered as one wall of text."""
    text = "Install Git first. 2. Create a repo. 3. Make a commit. 4. Push it."
    out = _split_numbered_steps(text)
    assert out.count("\n") == 3
    assert out.splitlines()[1].startswith("2.")


def test_version_numbers_are_not_mistaken_for_list_items():
    text = "Install Git 2.40 or newer. Then configure your identity."
    assert "\n" not in _split_numbered_steps(text)


def test_prose_sections_are_omitted_cleanly_when_synthesis_failed(bundle):
    """The document must survive Agent 4 failing — prose is the degradable part."""
    request, roadmap, catalog, media = bundle
    doc = render_document(request, roadmap, catalog, media,
                          executive_summary="", getting_started="")
    assert "## Overview" not in doc
    assert "## Getting started" not in doc
    assert "Free Course 1" in doc  # everything structural survives


def test_pipes_in_titles_do_not_break_tables():
    request = RoadmapRequest(topic="Git")
    roadmap = Roadmap(
        roadmap_title="Zero to Hero: Git", target_domain="Git",
        total_estimated_weeks=8, weekly_hours=10,
        phases=[_phase(i, f"Phase {i}") for i in range(1, 5)],
    )
    catalog = CourseCatalog(
        curated_courses=[{
            "phase_id": f"phase_{i}",
            "free_courses": [{"title": "Git | GitHub Crash Course",
                              "provider": "freeCodeCamp",
                              "url": f"https://www.freecodecamp.org/news/c{i}/",
                              "cost_type": "Free"}],
            "paid_courses": [{"title": "Advanced Git", "provider": "Udemy",
                              "url": f"https://www.udemy.com/course/c{i}/",
                              "cost_type": "Paid", "price_range": "$15-$25"}],
        } for i in range(1, 5)],
    )
    media = MediaLibrary(media_resources=[{
        "phase_id": f"phase_{i}",
        "interactive_or_docs": [{"title": f"Docs {i}",
                                 "url": f"https://git-scm.com/docs/{i}",
                                 "type": "Documentation"}],
    } for i in range(1, 5)])

    doc = render_document(request, roadmap, catalog, media)
    assert r"Git \| GitHub Crash Course" in doc


def test_summary_counts_match_the_payload(bundle):
    request, roadmap, catalog, media = bundle
    doc = render_document(request, roadmap, catalog, media)
    assert "| Courses | 8 (4 free) |" in doc
    assert "| Certifications | 1 |" in doc
    assert "| Supplementary resources | 12 |" in doc


def test_unreachable_links_are_marked_and_explained(bundle):
    """A dead link that cannot be dropped must be visible, not silent.

    Removing a phase's only free course would violate the schema, so it stays —
    but shipping it unmarked would falsify the document's own claim that every
    link was verified.
    """
    request, roadmap, catalog, media = bundle
    dead = catalog.curated_courses[0].free_courses[0].url

    doc = render_document(request, roadmap, catalog, media,
                          flagged_urls=frozenset({dead}))
    assert f"]({dead})" + " ⚠️" in doc
    assert "did not respond when this roadmap was generated" in doc


def test_no_legend_when_every_link_is_healthy(bundle):
    request, roadmap, catalog, media = bundle
    doc = render_document(request, roadmap, catalog, media)
    assert "⚠️" not in doc
    assert "did not respond" not in doc
