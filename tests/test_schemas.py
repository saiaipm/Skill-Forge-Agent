"""Contract tests.

These assert that the *guardrails* fire, not just that valid data parses.
Each test names the design-doc rule it defends so a failure points straight at
the requirement that broke.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_forge.schemas import (
    Certification,
    Course,
    CourseCatalog,
    CostType,
    MediaLibrary,
    PhaseAlignmentError,
    PhaseCourses,
    PhaseMedia,
    Relevance,
    Roadmap,
    validate_phase_alignment,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _phase(n: int) -> dict:
    return {
        "phase_id": f"phase_{n}",
        "phase_name": f"Phase {n}: Container Fundamentals",
        "estimated_hours": 30,
        "summary": (
            "A grounding in how containers and orchestration actually work, "
            "covering the kernel primitives images are built on."
        ),
        "core_concepts": [
            "Linux namespaces",
            "cgroups v2 resource limits",
            "OCI image format",
            "Container networking with CNI",
            "Pod lifecycle and restart policies",
        ],
        "milestone_project": {
            "title": "Package a Flask app as a container image",
            "description": (
                "Write a Dockerfile, build a minimal image, and run it locally "
                "with a mounted volume and an exposed port."
            ),
        },
    }


def _roadmap(**overrides) -> Roadmap:
    # 4 phases x 30h = 120h; at 10h/week that is 12 weeks.
    data = {
        "roadmap_title": "Zero to Hero: Kubernetes & Cloud Native Engineering",
        "target_domain": "Kubernetes",
        "total_estimated_weeks": 12,
        "weekly_hours": 10,
        "phases": [_phase(i) for i in range(1, 5)],
    }
    data.update(overrides)
    return Roadmap(**data)


def _free_course() -> dict:
    return {
        "title": "Introduction to Kubernetes",
        "provider": "edX",
        "url": "https://www.edx.org/course/introduction-to-kubernetes",
        "cost_type": "Free",
    }


def _paid_course() -> dict:
    return {
        "title": "Certified Kubernetes Administrator Prep",
        "provider": "Udemy",
        "url": "https://www.udemy.com/course/cka-prep/",
        "cost_type": "Paid",
        "price_range": "$15-$25",
    }


def _catalog(phase_ids: list[str]) -> CourseCatalog:
    # URLs vary per entry, keyed on position rather than phase_id: identical
    # course sets are rejected at model level, and the alignment tests need to
    # construct catalogs that repeat a phase_id while staying constructable.
    return CourseCatalog(
        curated_courses=[
            {
                "phase_id": pid,
                "free_courses": [_free_course() | {
                    "url": f"https://www.edx.org/course/k8s-{i}"}],
                "paid_courses": [_paid_course() | {
                    "url": f"https://www.udemy.com/course/cka-{i}/"}],
            }
            for i, pid in enumerate(phase_ids)
        ],
        certifications=[],
    )


def _media(phase_ids: list[str]) -> MediaLibrary:
    return MediaLibrary(
        media_resources=[
            {
                "phase_id": pid,
                "articles_and_blogs": [
                    {
                        "title": "How Kubernetes schedules pods",
                        "author_publisher": "Learnk8s",
                        "url": "https://learnk8s.io/scheduling",
                        "key_takeaway": "Explains the two-phase scheduling algorithm.",
                    }
                ],
            }
            for pid in phase_ids
        ]
    )


# --------------------------------------------------------------------------- #
# Agent 1 — Curriculum Architect guardrails
# --------------------------------------------------------------------------- #


def test_valid_roadmap_parses_and_derives_totals():
    rm = _roadmap()
    assert rm.phase_ids == ["phase_1", "phase_2", "phase_3", "phase_4"]
    assert rm.total_estimated_hours == 120


def test_vague_core_concept_is_rejected():
    """Design doc: MUST avoid vague topic titles like 'Advanced Stuff'."""
    bad = _phase(1)
    bad["core_concepts"] = [*_phase(1)["core_concepts"][:4], "Advanced Stuff"]
    with pytest.raises(ValidationError, match="placeholder"):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


@pytest.mark.parametrize(
    "concept",
    [
        "Advanced Git branching strategies",
        "Understanding Git's security features",
        "Basic networking concepts",
        "Container orchestration and more",
        "Exploring Kubernetes fundamentals",
    ],
)
def test_concepts_that_name_no_actual_topic_are_rejected(concept):
    """Observed real 8B output — passes an exact-match blocklist, teaches nothing."""
    bad = _phase(1)
    bad["core_concepts"] = [*_phase(1)["core_concepts"][:4], concept]
    with pytest.raises(ValidationError, match="names no specific concept"):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


@pytest.mark.parametrize(
    "concept",
    [
        "Git object model and SHA-1 addressing",
        "Understanding the three-way merge algorithm",
        "Advanced rebase with --onto",
        "Ingress controllers and TLS termination",
    ],
)
def test_legitimately_specific_concepts_are_not_over_rejected(concept):
    """Guards the opposite failure: patterns so broad they cause retry loops."""
    ok = _phase(1)
    ok["core_concepts"] = [*_phase(1)["core_concepts"][:4], concept]
    _roadmap(phases=[ok, _phase(2), _phase(3), _phase(4)])


def test_summary_that_merely_restates_the_phase_name_is_rejected():
    """Observed: 'Develop intermediate working competence in Git version control.'"""
    bad = _phase(1)
    bad["summary"] = "Develop intermediate competence in Git."
    with pytest.raises(ValidationError):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


def test_three_concepts_is_no_longer_enough_for_a_phase():
    bad = _phase(1)
    bad["core_concepts"] = ["Linux namespaces", "cgroups", "OCI image format"]
    with pytest.raises(ValidationError):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


# --------------------------------------------------------------------------- #
# Schedule arithmetic
# --------------------------------------------------------------------------- #


def test_week_count_contradicting_phase_hours_is_rejected():
    """The exact bug an 8B produced: 100h at 10h/week reported as 26 weeks."""
    with pytest.raises(ValidationError, match="contradicts"):
        _roadmap(total_estimated_weeks=26, weekly_hours=10)


def test_week_count_is_allowed_one_week_of_rounding_slack():
    _roadmap(total_estimated_weeks=13, weekly_hours=10)  # exact is 12
    _roadmap(total_estimated_weeks=11, weekly_hours=10)


def test_week_count_consistent_at_a_different_pace_is_accepted():
    # 120h at 20h/week = 6 weeks
    _roadmap(total_estimated_weeks=6, weekly_hours=20)


def test_vague_milestone_project_title_is_rejected():
    bad = _phase(1)
    bad["milestone_project"]["title"] = "Learn More"
    with pytest.raises(ValidationError, match="placeholder"):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


def test_roadmap_must_have_exactly_four_phases():
    with pytest.raises(ValidationError):
        _roadmap(phases=[_phase(1), _phase(2), _phase(3)])


def test_non_contiguous_phase_ids_are_rejected():
    """A renumbered phase orphans every downstream reference."""
    skipped = _phase(5)
    with pytest.raises(ValidationError, match="ordered and contiguous"):
        _roadmap(phases=[_phase(1), _phase(2), _phase(3), skipped])


def test_duplicate_core_concepts_are_rejected():
    bad = _phase(1)
    concepts = _phase(1)["core_concepts"]
    bad["core_concepts"] = [*concepts, concepts[0]]  # 6 items, one repeated
    with pytest.raises(ValidationError, match="duplicates"):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


def test_unknown_field_is_rejected_rather_than_silently_dropped():
    bad = _phase(1) | {"difficulty": "spicy"}
    with pytest.raises(ValidationError):
        _roadmap(phases=[bad, _phase(2), _phase(3), _phase(4)])


# --------------------------------------------------------------------------- #
# Agent 2 — Course & Certification guardrails
# --------------------------------------------------------------------------- #


def test_every_phase_needs_at_least_one_free_course():
    """Design doc: EVERY phase MUST have at least 1 verified Free resource."""
    with pytest.raises(ValidationError):
        PhaseCourses(phase_id="phase_1", free_courses=[], paid_courses=[_paid_course()])


def test_every_phase_needs_at_least_one_paid_course():
    with pytest.raises(ValidationError):
        PhaseCourses(phase_id="phase_1", free_courses=[_free_course()], paid_courses=[])


def test_paid_course_must_state_a_price():
    with pytest.raises(ValidationError, match="price_range"):
        Course(
            title="Certified Kubernetes Administrator Prep",
            provider="Udemy",
            url="https://www.udemy.com/course/cka-prep/",
            cost_type=CostType.PAID,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=Q1kHG842HoI",
        "https://youtu.be/Q1kHG842HoI",
        "https://vimeo.com/123456",
    ],
)
def test_video_links_are_rejected_as_courses(url):
    """Observed: a free YouTube video listed as a paid course at '$15-$25'."""
    with pytest.raises(ValidationError, match="is a video, not a course"):
        Course(title="Git Branching Tutorial", provider="YouTube",
               url=url, cost_type=CostType.FREE)


def test_same_url_cannot_be_both_free_and_paid_in_a_phase():
    """Observed: identical video free in one phase, $15-$25 in another."""
    shared = "https://www.coursera.org/learn/version-control-with-git"
    with pytest.raises(ValidationError, match="both free and paid"):
        PhaseCourses(
            phase_id="phase_1",
            free_courses=[{"title": "Version Control with Git", "provider": "Coursera",
                           "url": shared, "cost_type": "Free"}],
            paid_courses=[{"title": "Version Control with Git", "provider": "Coursera",
                           "url": shared, "cost_type": "Paid", "price_range": "$49"}],
        )


def test_free_course_cannot_be_filed_under_paid():
    with pytest.raises(ValidationError, match="free_courses but is Paid"):
        PhaseCourses(
            phase_id="phase_1",
            free_courses=[_paid_course()],
            paid_courses=[_paid_course()],
        )


def test_urls_survive_plain_json_dumps():
    """Regression: HttpUrl fields broke CrewAI's output path mid-pipeline.

    A model holding HttpUrl objects raises "Object of type HttpUrl is not JSON
    serializable" from any json.dumps(model_dump()) — which CrewAI does
    internally, 120 seconds into a run, long after the search quota is spent.
    """
    import json

    catalog = CourseCatalog(
        curated_courses=[
            {
                "phase_id": "phase_1",
                "free_courses": [_free_course()],
                "paid_courses": [_paid_course()],
            }
        ]
    )
    dumped = catalog.model_dump()  # deliberately NOT mode="json"
    json.dumps(dumped)  # must not raise
    assert isinstance(dumped["curated_courses"][0]["free_courses"][0]["url"], str)


def test_malformed_url_is_rejected():
    with pytest.raises(ValidationError):
        Course(
            title="Introduction to Kubernetes",
            provider="edX",
            url="not-a-url",
            cost_type=CostType.FREE,
        )


def test_certification_without_official_url_is_rejected():
    """Design doc: no hallucinated certifications."""
    with pytest.raises(ValidationError, match="official_url"):
        Certification(
            phase_id="phase_3",
            cert_name="Certified Kubernetes Administrator (CKA)",
            issuing_body="CNCF",
            relevance=Relevance.HIGH,
        )


def test_na_certification_requires_a_justification():
    with pytest.raises(ValidationError, match="justification"):
        Certification(
            phase_id="phase_1",
            cert_name="N/A",
            issuing_body="N/A",
            relevance=Relevance.NOT_APPLICABLE,
        )


def test_na_certification_with_justification_is_accepted():
    cert = Certification(
        phase_id="phase_1",
        cert_name="N/A",
        issuing_body="N/A",
        relevance=Relevance.NOT_APPLICABLE,
        justification="No vendor credential exists at the foundations level.",
    )
    assert cert.official_url is None


def test_half_na_certification_is_rejected():
    """Regression: rendered as a hyperlink captioned "N/A".

    Observed on a Gen AI run — the agent wanted to cite the NIST AI Risk
    Management Framework, which is a framework rather than a credential, and
    used the N/A escape hatch while still attaching a URL and relevance "Low".
    """
    with pytest.raises(ValidationError, match="is N/A in"):
        Certification(
            phase_id="phase_4",
            cert_name="N/A",
            issuing_body="N/A",
            relevance=Relevance.LOW,  # not N/A — incoherent
            official_url="https://www.nist.gov/itl/ai-risk-management-framework",
            justification="Framework rather than a certification.",
        )


def test_na_certification_cannot_carry_a_url():
    with pytest.raises(ValidationError, match="cannot have an official_url"):
        Certification(
            phase_id="phase_4",
            cert_name="N/A",
            issuing_body="N/A",
            relevance=Relevance.NOT_APPLICABLE,
            official_url="https://www.nist.gov/itl/ai-risk-management-framework",
            justification="No credential exists for this topic.",
        )


def test_real_certification_with_na_issuing_body_is_rejected():
    with pytest.raises(ValidationError, match="is N/A in"):
        Certification(
            phase_id="phase_3",
            cert_name="Microsoft Certified: Azure AI Fundamentals",
            issuing_body="N/A",
            relevance=Relevance.HIGH,
            official_url="https://learn.microsoft.com/certifications/azure-ai-fundamentals/",
        )


# --------------------------------------------------------------------------- #
# Agent 3 — Media guardrails
# --------------------------------------------------------------------------- #


def test_media_category_is_capped_to_prevent_overload():
    """Design doc: limit to 3-5 high-impact resources per category per phase."""
    article = {
        "title": "How Kubernetes schedules pods",
        "author_publisher": "Learnk8s",
        "url": "https://learnk8s.io/scheduling",
        "key_takeaway": "Explains the two-phase scheduling algorithm.",
    }
    with pytest.raises(ValidationError):
        PhaseMedia(phase_id="phase_1", articles_and_blogs=[article] * 6)


def test_completely_empty_phase_media_is_rejected():
    with pytest.raises(ValidationError, match="no media resources"):
        PhaseMedia(phase_id="phase_1")


# --------------------------------------------------------------------------- #
# QA rule #2 — cross-agent phase alignment
# --------------------------------------------------------------------------- #


def test_aligned_payloads_pass():
    rm = _roadmap()
    validate_phase_alignment(rm, _catalog(rm.phase_ids), _media(rm.phase_ids))


def test_missing_phase_in_catalog_is_caught():
    rm = _roadmap()
    partial = _catalog(["phase_1", "phase_2", "phase_3"])
    with pytest.raises(PhaseAlignmentError, match=r"missing \['phase_4'\]"):
        validate_phase_alignment(rm, partial, _media(rm.phase_ids))


def test_hallucinated_phase_id_is_caught():
    rm = _roadmap()
    invented = _catalog(["phase_1", "phase_2", "phase_3", "phase_9"])
    with pytest.raises(PhaseAlignmentError, match="unknown phase IDs"):
        validate_phase_alignment(rm, invented, _media(rm.phase_ids))


def test_duplicate_phase_entry_is_caught():
    rm = _roadmap()
    dupe = _catalog(["phase_1", "phase_1", "phase_2", "phase_3", "phase_4"])
    with pytest.raises(PhaseAlignmentError, match="duplicate"):
        validate_phase_alignment(rm, dupe, _media(rm.phase_ids))


def test_stray_certification_phase_id_is_caught():
    rm = _roadmap()
    catalog = _catalog(rm.phase_ids)
    catalog.certifications.append(
        Certification(
            phase_id="phase_7",
            cert_name="Certified Kubernetes Administrator (CKA)",
            issuing_body="CNCF",
            relevance=Relevance.HIGH,
            official_url="https://www.cncf.io/training/certification/cka/",
        )
    )
    with pytest.raises(PhaseAlignmentError, match="certifications references unknown"):
        validate_phase_alignment(rm, catalog, _media(rm.phase_ids))


def test_all_problems_are_reported_together_not_just_the_first():
    """A single retry should be able to fix everything at once."""
    rm = _roadmap()
    broken = _catalog(["phase_1", "phase_2", "phase_9"])
    with pytest.raises(PhaseAlignmentError) as exc:
        validate_phase_alignment(rm, broken, _media(rm.phase_ids))
    message = str(exc.value)
    assert "missing" in message
    assert "unknown phase IDs" in message
