"""Agent and task construction from YAML configuration.

CrewAI ships `@CrewBase` decorators that wire YAML to attributes implicitly.
This module does the same job explicitly instead: config is loaded, validated,
and passed as ordinary arguments. The tradeoff is a few more lines in exchange
for construction that can be unit-tested without instantiating a Crew, and
stack traces that point at real functions.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from crewai import Agent, Task

from skill_forge.guardrails import pydantic_guardrail
from skill_forge.llm import build_llm
from skill_forge.schemas import (
    CourseCatalog,
    DocumentPreamble,
    MediaLibrary,
    Relevance,
    Roadmap,
    RoadmapRequest,
)
from skill_forge.tools import (
    LinkVerifierTool,
    SerperVideoSearchTool,
    SerperWebSearchTool,
)

CONFIG_DIR = Path(__file__).parent / "config"


class ConfigError(RuntimeError):
    """Raised when a YAML config file is missing or malformed."""


@functools.lru_cache(maxsize=None)
def _load(filename: str) -> dict[str, Any]:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def agent_config(name: str) -> dict[str, Any]:
    """Fetch one agent's persona, failing loudly if it is absent."""
    cfg = _load("agents.yaml")
    if name not in cfg:
        raise ConfigError(
            f"No agent {name!r} in agents.yaml. Defined: {sorted(cfg)}"
        )
    return cfg[name]


def task_config(name: str) -> dict[str, Any]:
    cfg = _load("tasks.yaml")
    if name not in cfg:
        raise ConfigError(f"No task {name!r} in tasks.yaml. Defined: {sorted(cfg)}")
    return cfg[name]


# --------------------------------------------------------------------------- #
# Agent 1 — Curriculum Architect
# --------------------------------------------------------------------------- #


def build_curriculum_architect(llm=None) -> Agent:
    """The pedagogy specialist. Deliberately has no tools.

    Everything this agent needs is reasoning about how skills decompose. Giving
    it search would invite it to mirror whatever roadmap ranks highest on
    Google rather than build one from instructional-design principles.
    """
    cfg = agent_config("curriculum_architect")
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=cfg["backstory"],
        llm=llm or build_llm(),
        tools=[],
        allow_delegation=False,
        verbose=True,
        max_iter=3,
    )


def build_curriculum_task(request: RoadmapRequest, agent: Agent) -> Task:
    """Bind a concrete learner request to the curriculum brief."""
    cfg = task_config("design_curriculum")
    return Task(
        description=cfg["description"].format(
            topic=request.topic,
            user_experience_level=request.user_experience_level.value,
            hours_per_week=request.target_time_commitment_hrs_per_week,
        ),
        expected_output=cfg["expected_output"],
        agent=agent,
        # NOTE: output_pydantic is deliberately NOT set alongside a guardrail.
        # CrewAI's guardrail retry loop calls _export_output() on every
        # regenerated attempt (task.py ~line 1151) without re-checking the
        # guardrail first, so a second failed attempt raises fatally and the
        # retry budget is silently lost. With output_pydantic unset that call
        # is a no-op, and the guardrail — which returns validated JSON — stays
        # the single source of truth. Typed objects come from typed_output().
        # The guardrail — not output_pydantic — is what makes validation
        # recoverable. See skill_forge.guardrails for why.
        guardrail=pydantic_guardrail(Roadmap),
        guardrail_max_retries=3,
    )


# --------------------------------------------------------------------------- #
# Shared context for the parallel branch
# --------------------------------------------------------------------------- #


def render_phase_brief(roadmap: Roadmap) -> str:
    """Flatten the roadmap into the slice Agents 2 and 3 actually need.

    They receive the phase IDs, names, and concepts — the raw material for
    search queries — but not summaries or milestone projects, which would only
    dilute the context they reason over.
    """
    blocks: list[str] = []
    for phase in roadmap.phases:
        concepts = "\n".join(f"            - {c}" for c in phase.core_concepts)
        blocks.append(
            f"        {phase.phase_id} — {phase.phase_name} "
            f"({phase.estimated_hours}h)\n"
            f"          concepts to find resources for:\n{concepts}"
        )
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Agent 2 — Course & Certification Curator
# --------------------------------------------------------------------------- #


def build_course_curator(llm=None) -> Agent:
    """Finds courses and credentials. Searches the web and verifies its links."""
    cfg = agent_config("course_curator")
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=cfg["backstory"],
        llm=llm or build_llm(),
        tools=[SerperWebSearchTool(), LinkVerifierTool()],
        allow_delegation=False,
        verbose=True,
        # Four phases x (search + verify) plus retries. At 15 a broad topic
        # like Kubernetes was cut off mid-curation ("Maximum iterations
        # reached"), which surfaced downstream as a phase with no resources.
        max_iter=25,
    )


def build_course_task(roadmap: Roadmap, agent: Agent) -> Task:
    cfg = task_config("curate_courses")
    return Task(
        description=cfg["description"].format(
            target_domain=roadmap.target_domain,
            phase_brief=render_phase_brief(roadmap),
        ),
        expected_output=cfg["expected_output"],
        agent=agent,
        # NOTE: output_pydantic is deliberately NOT set alongside a guardrail.
        # CrewAI's guardrail retry loop calls _export_output() on every
        # regenerated attempt (task.py ~line 1151) without re-checking the
        # guardrail first, so a second failed attempt raises fatally and the
        # retry budget is silently lost. With output_pydantic unset that call
        # is a no-op, and the guardrail — which returns validated JSON — stays
        # the single source of truth. Typed objects come from typed_output().
        guardrail=pydantic_guardrail(
            CourseCatalog, expected_phase_ids=roadmap.phase_ids
        ),
        guardrail_max_retries=3,
        # Concurrency with the media task is handled by the orchestrator's
        # thread pool, not CrewAI's async_execution — see skill_forge.crew for
        # why. Leaving this synchronous keeps a single-task crew valid.
    )


# --------------------------------------------------------------------------- #
# Agent 3 — Resource & Media Mining
# --------------------------------------------------------------------------- #


def build_media_miner(llm=None) -> Agent:
    """Finds docs, articles, and video. Has video search in addition to web."""
    cfg = agent_config("media_miner")
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=cfg["backstory"],
        llm=llm or build_llm(),
        tools=[SerperWebSearchTool(), SerperVideoSearchTool(), LinkVerifierTool()],
        allow_delegation=False,
        verbose=True,
        # Three tools and two searches per phase — needs more headroom than the
        # curator, which has one search tool.
        max_iter=30,
    )


def build_media_task(roadmap: Roadmap, agent: Agent) -> Task:
    cfg = task_config("mine_media")
    return Task(
        description=cfg["description"].format(
            target_domain=roadmap.target_domain,
            phase_brief=render_phase_brief(roadmap),
        ),
        expected_output=cfg["expected_output"],
        agent=agent,
        # NOTE: output_pydantic is deliberately NOT set alongside a guardrail.
        # CrewAI's guardrail retry loop calls _export_output() on every
        # regenerated attempt (task.py ~line 1151) without re-checking the
        # guardrail first, so a second failed attempt raises fatally and the
        # retry budget is silently lost. With output_pydantic unset that call
        # is a no-op, and the guardrail — which returns validated JSON — stays
        # the single source of truth. Typed objects come from typed_output().
        guardrail=pydantic_guardrail(
            MediaLibrary, expected_phase_ids=roadmap.phase_ids
        ),
        guardrail_max_retries=3,
        # Concurrency handled by the orchestrator's thread pool; see crew.py.
    )


# --------------------------------------------------------------------------- #
# Agent 4 — Document Synthesis
# --------------------------------------------------------------------------- #


def build_document_synthesist(llm=None) -> Agent:
    """Writes the roadmap's prose. Has no tools and never sees the resource list.

    The design doc requires that no course, video, or certification be dropped
    at synthesis. Rather than instruct a model not to drop things — which it
    will do anyway, silently, given forty items to reformat — the resources are
    rendered from data by `skill_forge.render` and this agent is never shown
    them. It cannot lose what it was never given.
    """
    cfg = agent_config("document_synthesist")
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=cfg["backstory"],
        llm=llm or build_llm(),
        tools=[],
        allow_delegation=False,
        verbose=True,
        max_iter=3,
    )


def render_phase_outline(roadmap: Roadmap) -> str:
    """A one-line-per-phase digest — enough to orient, too little to pad with."""
    return "\n".join(
        f"        {p.phase_id} — {p.phase_name} ({p.estimated_hours}h): "
        f"{p.milestone_project.title}"
        for p in roadmap.phases
    )


def build_synthesis_task(
    roadmap: Roadmap,
    catalog: CourseCatalog,
    media: MediaLibrary,
    agent: Agent,
) -> Task:
    cfg = task_config("synthesise_document")

    course_count = sum(
        len(pc.free_courses) + len(pc.paid_courses) for pc in catalog.curated_courses
    )
    free_count = sum(len(pc.free_courses) for pc in catalog.curated_courses)
    media_count = sum(
        len(pm.articles_and_blogs)
        + len(pm.youtube_content)
        + len(pm.interactive_or_docs)
        for pm in media.media_resources
    )
    cert_count = len(
        [c for c in catalog.certifications if c.relevance is not Relevance.NOT_APPLICABLE]
    )

    return Task(
        description=cfg["description"].format(
            roadmap_title=roadmap.roadmap_title,
            target_domain=roadmap.target_domain,
            weekly_hours=roadmap.weekly_hours,
            total_weeks=roadmap.total_estimated_weeks,
            total_hours=roadmap.total_estimated_hours,
            phase_outline=render_phase_outline(roadmap),
            course_count=course_count,
            free_count=free_count,
            media_count=media_count,
            cert_count=cert_count,
        ),
        expected_output=cfg["expected_output"],
        agent=agent,
        # NOTE: output_pydantic is deliberately NOT set alongside a guardrail.
        # CrewAI's guardrail retry loop calls _export_output() on every
        # regenerated attempt (task.py ~line 1151) without re-checking the
        # guardrail first, so a second failed attempt raises fatally and the
        # retry budget is silently lost. With output_pydantic unset that call
        # is a no-op, and the guardrail — which returns validated JSON — stays
        # the single source of truth. Typed objects come from typed_output().
        guardrail=pydantic_guardrail(DocumentPreamble),
        guardrail_max_retries=2,
    )
