"""Orchestration: the DAG that turns a topic into a roadmap document.

    RoadmapRequest
          |
          v
    [Curriculum Architect]          stage 1, sequential
          |
          v  Roadmap  <-- validated here, before any search quota is spent
          |
    +-----+-----+
    |           |                   stage 2, concurrent
    v           v
 [Course     [Media
  Curator]    Miner]
    |           |
    +-----+-----+
          |
          v  CourseCatalog + MediaLibrary  <-- phase alignment checked
          |
          v
  [Document Synthesist]             stage 3, prose only
          |
          v
   render_document()                deterministic assembly
          |
          v
      Markdown

Two stages rather than one crew, deliberately. CrewAI can pass a task's output
to the next through implicit context, but that would mean dispatching four
search-heavy agents against a roadmap nobody had checked. Splitting at the
validation boundary means a malformed roadmap costs one cheap LLM call instead
of a full run's worth of Serper quota.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from crewai import Crew, Process

from skill_forge.agents import (
    build_course_curator,
    build_course_task,
    build_curriculum_architect,
    build_curriculum_task,
    build_document_synthesist,
    build_media_miner,
    build_media_task,
    build_synthesis_task,
)
from skill_forge.guardrails import typed_output
from skill_forge.llm import build_llm
from skill_forge.render import render_document
from skill_forge.verify import VerificationReport, verify_and_prune
from skill_forge.schemas import (
    CourseCatalog,
    DocumentPreamble,
    MediaLibrary,
    PhaseAlignmentError,
    Roadmap,
    RoadmapRequest,
    validate_phase_alignment,
)


class PipelineError(RuntimeError):
    """Raised when a stage produces nothing usable."""


@dataclass
class RunReport:
    """What happened during a run — timings, counts, and any degradation.

    Returned alongside the document so callers can distinguish "worked" from
    "worked, but the synthesis agent failed and the prose is missing".
    """

    request: RoadmapRequest
    roadmap: Roadmap | None = None
    catalog: CourseCatalog | None = None
    media: MediaLibrary | None = None
    preamble: DocumentPreamble | None = None
    links: VerificationReport | None = None
    markdown: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return sum(self.timings.values())

    def summary(self) -> str:
        lines = [f"{stage}: {secs:.1f}s" for stage, secs in self.timings.items()]
        lines.append(f"total: {self.total_seconds:.1f}s")
        if self.warnings:
            lines.append(f"warnings: {len(self.warnings)}")
        return "  |  ".join(lines)


def _stage_one(request: RoadmapRequest, llm, verbose: bool) -> Roadmap:
    architect = build_curriculum_architect(llm)
    task = build_curriculum_task(request, architect)
    roadmap = _run_single_task_crew(architect, task, Roadmap, verbose)
    if roadmap is None:
        raise PipelineError(
            "The Curriculum Architect produced no valid roadmap after retries. "
            "This usually means the model cannot hold the output schema — try a "
            "stronger model via MODEL in .env."
        )
    return roadmap


def _run_single_task_crew(agent, task, model, verbose: bool):
    """Run one agent/task pair and return its validated output, or None.

    The typed object is recovered from the guardrail's output rather than from
    ``task.output.pydantic``, which is always None for guarded tasks. See
    `skill_forge.guardrails.typed_output`.
    """
    Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=verbose,
    ).kickoff()
    return typed_output(task, model)


def _stage_two(
    roadmap: Roadmap, verbose: bool
) -> tuple[CourseCatalog, MediaLibrary]:
    """Curator and Miner, concurrently.

    Parallelism is done with threads rather than CrewAI's ``async_execution``.
    CrewAI rejects any crew ending in more than one asynchronous task — they
    must be consumed by a trailing synchronous one — and the only task that
    could play that role is the Synthesist, which cannot be constructed yet
    because its brief needs resource counts from these very payloads.

    Threads also buy something CrewAI's async does not: if the Miner fails, the
    Curator's result survives, and the run degrades instead of collapsing. Both
    branches are pure network waits, so threads are the right tool regardless.
    """
    # A separate LLM client per thread. The vendor SDKs are generally
    # thread-safe, but sharing one buys nothing and risks a subtle interleaving
    # bug that would only appear under concurrency.
    curator = build_course_curator(build_llm())
    miner = build_media_miner(build_llm())

    course_task = build_course_task(roadmap, curator)
    media_task = build_media_task(roadmap, miner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        course_future = pool.submit(
            _run_single_task_crew, curator, course_task, CourseCatalog, verbose
        )
        media_future = pool.submit(
            _run_single_task_crew, miner, media_task, MediaLibrary, verbose
        )
        catalog = course_future.result()
        media = media_future.result()

    if catalog is None:
        raise PipelineError(
            "The Course Curator produced no valid catalog after retries."
        )
    if media is None:
        raise PipelineError(
            "The Media Miner produced no valid media library after retries."
        )
    return catalog, media


def _stage_three(
    roadmap: Roadmap,
    catalog: CourseCatalog,
    media: MediaLibrary,
    llm,
    verbose: bool,
) -> DocumentPreamble | None:
    """Write the prose. A failure here degrades the document, it does not fail it."""
    synthesist = build_document_synthesist(llm)
    task = build_synthesis_task(roadmap, catalog, media, synthesist)
    return _run_single_task_crew(synthesist, task, DocumentPreamble, verbose)


def generate_roadmap(
    request: RoadmapRequest,
    *,
    llm=None,
    verbose: bool = False,
) -> RunReport:
    """Run the full pipeline and return the document plus a report.

    Raises:
        PipelineError: if a stage that cannot be degraded produces nothing.
    """
    llm = llm or build_llm()
    report = RunReport(request=request)

    t0 = time.monotonic()
    report.roadmap = _stage_one(request, llm, verbose)
    report.timings["architect"] = time.monotonic() - t0

    t0 = time.monotonic()
    catalog, media = _stage_two(report.roadmap, verbose)
    report.catalog, report.media = catalog, media
    report.timings["curate+mine"] = time.monotonic() - t0

    # QA rule #1, enforced rather than requested. The agents have a verify_links
    # tool and are told to use it, but an agent cut off by max_iter skips it and
    # fabricates the resources it never searched for. Checking here is the only
    # way the document's "every link was verified" claim is actually true.
    t0 = time.monotonic()
    catalog, media, link_report = verify_and_prune(catalog, media)
    report.catalog, report.media = catalog, media
    report.links = link_report
    report.timings["verify"] = time.monotonic() - t0

    if link_report.dead_urls:
        report.warnings.append(
            f"{len(link_report.dead_urls)} dead links found and removed "
            f"({len(link_report.removed)} resources dropped)"
        )
    report.warnings.extend(link_report.warnings)

    # QA rule #2, belt and braces: each agent's guardrail already checked its own
    # phase IDs, but only here are all three payloads visible at once.
    try:
        validate_phase_alignment(report.roadmap, catalog, media)
    except PhaseAlignmentError as e:
        report.warnings.append(f"phase alignment: {e}")

    t0 = time.monotonic()
    try:
        report.preamble = _stage_three(report.roadmap, catalog, media, llm, verbose)
    except Exception as e:
        # Prose is the one part the document can survive without.
        report.warnings.append(f"synthesis failed, prose omitted: {type(e).__name__}: {e}")
    report.timings["synthesis"] = time.monotonic() - t0

    if report.preamble is None and "synthesis failed" not in " ".join(report.warnings):
        report.warnings.append("synthesis produced no preamble; prose omitted")

    report.markdown = render_document(
        request=request,
        roadmap=report.roadmap,
        catalog=catalog,
        media=media,
        executive_summary=report.preamble.executive_summary if report.preamble else "",
        getting_started=report.preamble.getting_started if report.preamble else "",
        flagged_urls=frozenset(link_report.kept_dead),
    )
    return report
