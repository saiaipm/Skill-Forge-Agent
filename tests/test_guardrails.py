"""Guardrail tests.

The contract these defend: a guardrail must return (False, <actionable text>)
on bad output rather than raising, because raising is what makes CrewAI's
output_pydantic path fatal in the first place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from skill_forge.guardrails import extract_json, pydantic_guardrail, typed_output
from skill_forge.schemas import CourseCatalog, MediaLibrary, Roadmap


@dataclass
class FakeOutput:
    """Stands in for crewai's TaskOutput — the guardrail only reads .raw."""

    raw: str


def _phase(n: int) -> dict:
    return {
        "phase_id": f"phase_{n}",
        "phase_name": f"Phase {n}: Foundations",
        "estimated_hours": 30,
        "summary": (
            "A grounding in how the technology works underneath, covering the "
            "primitives everything else is built from."
        ),
        "core_concepts": [
            "Staging area and index",
            "Three-way merge algorithm",
            "Reflog recovery",
            "Interactive rebase",
            "Detached HEAD state",
        ],
        "milestone_project": {
            "title": "Build a branching workflow",
            "description": (
                "Create a repository, branch, resolve a merge conflict, and "
                "rebase cleanly onto main."
            ),
        },
    }


def _roadmap_dict() -> dict:
    return {
        "roadmap_title": "Zero to Hero: Git",
        "target_domain": "Git",
        "total_estimated_weeks": 12,
        "weekly_hours": 10,
        "phases": [_phase(i) for i in range(1, 5)],
    }


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #


def test_extracts_bare_json():
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extracts_json_from_markdown_fence():
    raw = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps!'
    assert json.loads(extract_json(raw)) == {"a": 1}


def test_extracts_json_surrounded_by_prose():
    raw = 'Sure! The roadmap is {"a": 1} — let me know if you need changes.'
    assert json.loads(extract_json(raw)) == {"a": 1}


def test_returns_none_when_there_is_no_json():
    assert extract_json("I could not complete this task.") is None


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_valid_payload_passes_and_returns_a_json_string():
    """Must return a str: CrewAI stores it as task_output.raw, which is what
    `typed_output` later parses back into a model."""
    guard = pydantic_guardrail(Roadmap)
    ok, result = guard(FakeOutput(raw=json.dumps(_roadmap_dict())))
    assert ok is True
    assert isinstance(result, str)
    assert Roadmap.model_validate_json(result).target_domain == "Git"


def test_valid_payload_wrapped_in_prose_still_passes():
    guard = pydantic_guardrail(Roadmap)
    raw = f"Here is the roadmap:\n```json\n{json.dumps(_roadmap_dict())}\n```"
    ok, _ = guard(FakeOutput(raw=raw))
    assert ok is True


# --------------------------------------------------------------------------- #
# Failure paths — must NOT raise
# --------------------------------------------------------------------------- #


def test_schema_violation_is_reported_not_raised():
    """The exact 8B failure: a phase with 4 concepts instead of 5."""
    bad = _roadmap_dict()
    bad["phases"][3]["core_concepts"] = ["a", "b", "c", "d"]
    guard = pydantic_guardrail(Roadmap)
    ok, message = guard(FakeOutput(raw=json.dumps(bad)))
    assert ok is False
    assert "core_concepts" in message
    assert "add more items" in message


def test_vague_title_violation_names_the_offending_field():
    """The other observed 8B failure: milestone titled 'Advanced Git Topics'."""
    bad = _roadmap_dict()
    bad["phases"][3]["milestone_project"]["title"] = "Advanced Git Topics"
    guard = pydantic_guardrail(Roadmap)
    ok, message = guard(FakeOutput(raw=json.dumps(bad)))
    assert ok is False
    assert "milestone_project.title" in message


def test_non_json_output_gets_explicit_instructions():
    guard = pydantic_guardrail(Roadmap)
    ok, message = guard(FakeOutput(raw="I was unable to complete this."))
    assert ok is False
    assert "ONLY a single JSON object" in message


def test_malformed_json_is_reported_not_raised():
    guard = pydantic_guardrail(Roadmap)
    ok, message = guard(FakeOutput(raw='{"roadmap_title": "x", '))
    assert ok is False
    assert isinstance(message, str)


def test_error_list_is_truncated_so_it_does_not_flood_context():
    empty = {"roadmap_title": "", "target_domain": "", "total_estimated_weeks": 0,
             "weekly_hours": 0, "phases": []}
    guard = pydantic_guardrail(Roadmap)
    ok, message = guard(FakeOutput(raw=json.dumps(empty)))
    assert ok is False
    assert message.count("\n  - ") <= 9  # 8 errors plus the "and N more" line


# --------------------------------------------------------------------------- #
# Cross-agent phase alignment, caught while retries remain
# --------------------------------------------------------------------------- #


def _catalog_dict(phase_ids: list[str]) -> dict:
    return {
        "curated_courses": [
            {
                "phase_id": pid,
                # Distinct URLs per phase — identical course sets are rejected.
                "free_courses": [
                    {"title": "Intro to Git", "provider": "freeCodeCamp",
                     "url": f"https://www.freecodecamp.org/news/git-{pid}/",
                     "cost_type": "Free"}
                ],
                "paid_courses": [
                    {"title": "Git Complete", "provider": "Udemy",
                     "url": f"https://www.udemy.com/course/git-{pid}/",
                     "cost_type": "Paid", "price_range": "$15-$25"}
                ],
            }
            for pid in phase_ids
        ],
        "certifications": [],
    }


def test_catalog_matching_the_roadmap_passes():
    guard = pydantic_guardrail(
        CourseCatalog, expected_phase_ids=["phase_1", "phase_2", "phase_3", "phase_4"]
    )
    ok, _ = guard(FakeOutput(raw=json.dumps(
        _catalog_dict(["phase_1", "phase_2", "phase_3", "phase_4"])
    )))
    assert ok is True


def test_catalog_missing_a_phase_is_rejected_with_the_id_named():
    guard = pydantic_guardrail(
        CourseCatalog, expected_phase_ids=["phase_1", "phase_2", "phase_3", "phase_4"]
    )
    ok, message = guard(FakeOutput(raw=json.dumps(
        _catalog_dict(["phase_1", "phase_2", "phase_3"])
    )))
    assert ok is False
    assert "phase_4" in message


def test_catalog_inventing_a_phase_is_rejected():
    guard = pydantic_guardrail(
        CourseCatalog, expected_phase_ids=["phase_1", "phase_2", "phase_3", "phase_4"]
    )
    ok, message = guard(FakeOutput(raw=json.dumps(
        _catalog_dict(["phase_1", "phase_2", "phase_3", "phase_9"])
    )))
    assert ok is False
    assert "unknown phase IDs" in message


def test_media_library_alignment_is_checked_too():
    guard = pydantic_guardrail(
        MediaLibrary, expected_phase_ids=["phase_1", "phase_2", "phase_3", "phase_4"]
    )
    payload = {
        "media_resources": [
            {
                "phase_id": pid,
                "articles_and_blogs": [
                    {"title": "How Git stores data", "author_publisher": "Git Book",
                     "url": "https://git-scm.com/book/en/v2",
                     "key_takeaway": "Explains the object database."}
                ],
            }
            for pid in ["phase_1", "phase_2"]
        ]
    }
    ok, message = guard(FakeOutput(raw=json.dumps(payload)))
    assert ok is False
    assert "missing" in message


# --------------------------------------------------------------------------- #
# Custom semantic checks
# --------------------------------------------------------------------------- #


def test_extra_check_can_reject_semantically_valid_but_unwanted_output():
    guard = pydantic_guardrail(
        Roadmap,
        extra_check=lambda rm: (
            "target_domain is too broad; name the specific technology"
            if rm.target_domain in {"Software Development", "Programming"}
            else None
        ),
    )
    payload = _roadmap_dict() | {"target_domain": "Software Development"}
    ok, message = guard(FakeOutput(raw=json.dumps(payload)))
    assert ok is False
    assert "too broad" in message


def test_extra_check_passing_lets_output_through():
    guard = pydantic_guardrail(Roadmap, extra_check=lambda rm: None)
    ok, _ = guard(FakeOutput(raw=json.dumps(_roadmap_dict())))
    assert ok is True


# --------------------------------------------------------------------------- #
# typed_output — recovering the model without output_pydantic
# --------------------------------------------------------------------------- #


@dataclass
class FakeTask:
    """Stands in for a completed crewai Task."""

    output: object | None


def test_typed_output_recovers_the_model_from_raw():
    """Guarded tasks leave .pydantic empty; the payload lives in .raw."""
    guard = pydantic_guardrail(Roadmap)
    _, validated_json = guard(FakeOutput(raw=json.dumps(_roadmap_dict())))
    task = FakeTask(output=FakeOutput(raw=validated_json))

    roadmap = typed_output(task, Roadmap)
    assert roadmap is not None
    assert roadmap.target_domain == "Git"
    assert roadmap.phase_ids == ["phase_1", "phase_2", "phase_3", "phase_4"]


def test_typed_output_handles_a_task_that_never_ran():
    assert typed_output(FakeTask(output=None), Roadmap) is None


def test_typed_output_returns_none_rather_than_raising_on_junk():
    """Never crash the assembly step over an unexpected task state."""
    assert typed_output(FakeTask(output=FakeOutput(raw="")), Roadmap) is None
    assert typed_output(FakeTask(output=FakeOutput(raw="no json here")), Roadmap) is None
    assert typed_output(FakeTask(output=FakeOutput(raw='{"partial": 1}')), Roadmap) is None


def test_week_mismatch_is_caught_by_the_guardrail_not_left_to_crewai():
    """Regression: the run that crashed on 'Gen AI for Product Managers'.

    120h at 8h/week is 15 weeks; the model claimed 12. With output_pydantic set,
    CrewAI raised fatally inside its own retry loop before the guardrail could
    ask for a correction.
    """
    bad = _roadmap_dict()
    bad["weekly_hours"] = 8
    bad["total_estimated_weeks"] = 12  # 120h / 8 = 15
    ok, message = pydantic_guardrail(Roadmap)(FakeOutput(raw=json.dumps(bad)))
    assert ok is False
    assert "contradicts" in message
    assert "15 weeks" in message
