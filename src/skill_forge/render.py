"""Deterministic Markdown rendering of an assembled roadmap.

The design doc's rule for the synthesis stage is that no course, video, or
certification produced upstream may be dropped. That rule is the reason this
module exists and is not an LLM prompt.

Reformatting a large structured payload is precisely the task language models
fail at quietly: given forty resources they will emit thirty-four, in the right
format, with no indication anything is missing. A `for` loop cannot do that. So
everything that already exists as data is rendered here, and the model is left
to write only the two sections that genuinely require prose — the executive
summary and the getting-started guide.

Output targets GitHub Flavored Markdown, which is also what Pandoc and most
HTML converters accept.
"""

from __future__ import annotations

import re
from datetime import date

from skill_forge.schemas import (
    Certification,
    CourseCatalog,
    MediaLibrary,
    Phase,
    PhaseCourses,
    PhaseMedia,
    Relevance,
    Roadmap,
    RoadmapRequest,
)

PHASE_SUBTITLES = {
    "phase_1": "Foundations",
    "phase_2": "Building",
    "phase_3": "Professional",
    "phase_4": "Mastery",
}


def _escape_pipes(text: str) -> str:
    """Keep a stray | in a course title from shattering a Markdown table."""
    return text.replace("|", "\\|")


def _heading_for(phase: Phase) -> str:
    """Phase name plus its stage label, unless they are the same word.

    The architect frequently names a phase exactly what the stage is called,
    which without this check renders as "Foundations — Foundations".
    """
    subtitle = PHASE_SUBTITLES.get(phase.phase_id, "")
    if not subtitle or subtitle.lower() in phase.phase_name.lower():
        return phase.phase_name
    return f"{phase.phase_name} — {subtitle}"


def _split_numbered_steps(text: str) -> str:
    """Put "1. ... 2. ... 3. ..." on separate lines.

    Models routinely return numbered steps as one space-separated paragraph.
    Markdown needs them on their own lines to render as a list, so the numbering
    is re-broken here rather than trusting the prompt to produce newlines.
    """
    text = text.strip()
    # Only split on a number that follows sentence-ending punctuation or start,
    # so decimals and version numbers ("Git 2.40") are left alone.
    split = re.sub(r"(?<=[.!?])\s+(?=\d{1,2}\.\s)", "\n", text)
    return re.sub(r"^\s*(\d{1,2}\.)\s*", r"\1 ", split, flags=re.MULTILINE)


def _anchor(phase_id: str) -> str:
    return f"#{phase_id.replace('_', '-')}"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def render_header(roadmap: Roadmap, request: RoadmapRequest) -> str:
    hours = roadmap.total_estimated_hours
    lines = [
        f"# {roadmap.roadmap_title}",
        "",
        f"> A Zero-to-Hero learning roadmap for **{roadmap.target_domain}**, "
        f"generated for a **{request.user_experience_level.value.lower()}** "
        f"learner studying **{roadmap.weekly_hours} hours per week**.",
        "",
        "| | |",
        "|---|---|",
        f"| **Total effort** | {hours} hours |",
        f"| **Duration** | {roadmap.total_estimated_weeks} weeks "
        f"at {roadmap.weekly_hours} hrs/week |",
        f"| **Phases** | {len(roadmap.phases)} |",
        f"| **Generated** | {date.today().isoformat()} |",
        "",
    ]
    return "\n".join(lines)


def render_contents(roadmap: Roadmap) -> str:
    lines = ["## Contents", ""]
    for phase in roadmap.phases:
        label = _heading_for(phase)
        lines.append(
            f"{phase.phase_id[-1]}. [{_escape_pipes(label)}]({_anchor(phase.phase_id)})"
            f" · {phase.estimated_hours}h"
        )
    lines.append("")
    return "\n".join(lines)


#: Appended to any link verification found unreachable but which could not be
#: removed without breaking a schema guarantee. Shipping a known-dead link
#: unmarked would quietly falsify the document's own claim that links were
#: checked.
UNREACHABLE_NOTE = " ⚠️"
UNREACHABLE_LEGEND = (
    "⚠️ marks a link that did not respond when this roadmap was generated. It is "
    "listed because it is the only resource of its kind for that phase — treat "
    "it as a search term rather than a working link."
)


def render_courses_table(
    pc: PhaseCourses | None, flagged: frozenset[str] = frozenset()
) -> str:
    if pc is None:
        return "_No courses were curated for this phase._\n"

    lines = [
        "| Cost | Course | Provider | Price |",
        "|---|---|---|---|",
    ]
    for course in pc.free_courses:
        mark = UNREACHABLE_NOTE if course.url in flagged else ""
        lines.append(
            f"| Free | [{_escape_pipes(course.title)}]({course.url}){mark} "
            f"| {_escape_pipes(course.provider)} | — |"
        )
    for course in pc.paid_courses:
        price = _escape_pipes(course.price_range or "See site")
        mark = UNREACHABLE_NOTE if course.url in flagged else ""
        lines.append(
            f"| Paid | [{_escape_pipes(course.title)}]({course.url}){mark} "
            f"| {_escape_pipes(course.provider)} | {price} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_certifications(certs: list[Certification]) -> str:
    if not certs:
        return ""

    real = [c for c in certs if c.relevance is not Relevance.NOT_APPLICABLE]
    absent = [c for c in certs if c.relevance is Relevance.NOT_APPLICABLE]

    lines = ["**Certifications**", ""]
    if real:
        lines += ["| Credential | Issuing body | Relevance | Prerequisites |",
                  "|---|---|---|---|"]
        for c in real:
            url = f"[{_escape_pipes(c.cert_name)}]({c.official_url})" if c.official_url \
                else _escape_pipes(c.cert_name)
            prereq = _escape_pipes(c.prerequisites or "None stated")
            lines.append(
                f"| {url} | {_escape_pipes(c.issuing_body)} "
                f"| {c.relevance.value} | {prereq} |"
            )
        lines.append("")
    for c in absent:
        lines.append(f"> **No certification applies here.** {c.justification}")
        lines.append("")
    return "\n".join(lines)


def render_media(pm: PhaseMedia | None) -> str:
    if pm is None:
        return ""

    blocks: list[str] = []

    if pm.youtube_content:
        blocks.append("**Video**\n")
        for v in pm.youtube_content:
            meta = [v.format.value]
            if v.duration:
                meta.append(v.duration)
            suffix = f" · {' · '.join(meta)}" if meta else ""
            blocks.append(
                f"- [{_escape_pipes(v.title)}]({v.url}) — "
                f"{_escape_pipes(v.channel_name)}{suffix}"
            )
            if v.key_takeaway:
                blocks.append(f"  {v.key_takeaway}")
        blocks.append("")

    if pm.articles_and_blogs:
        blocks.append("**Reading**\n")
        for a in pm.articles_and_blogs:
            blocks.append(
                f"- [{_escape_pipes(a.title)}]({a.url}) — "
                f"{_escape_pipes(a.author_publisher)}"
            )
            blocks.append(f"  {a.key_takeaway}")
        blocks.append("")

    if pm.interactive_or_docs:
        blocks.append("**Documentation & hands-on**\n")
        for d in pm.interactive_or_docs:
            blocks.append(f"- [{_escape_pipes(d.title)}]({d.url}) — {d.type.value}")
            if d.key_takeaway:
                blocks.append(f"  {d.key_takeaway}")
        blocks.append("")

    return "\n".join(blocks)


def render_phase(
    phase: Phase,
    courses: PhaseCourses | None,
    media: PhaseMedia | None,
    certs: list[Certification],
    flagged: frozenset[str] = frozenset(),
) -> str:
    lines = [
        f'<a id="{phase.phase_id.replace("_", "-")}"></a>',
        "",
        f"## {_heading_for(phase)}",
        "",
        f"`{phase.estimated_hours} hours`",
        "",
        phase.summary,
        "",
        "### What you will learn",
        "",
    ]
    lines += [f"- {c}" for c in phase.core_concepts]
    lines += [
        "",
        "### Milestone project",
        "",
        f"**{phase.milestone_project.title}**",
        "",
        phase.milestone_project.description,
        "",
        "### Courses",
        "",
        render_courses_table(courses, flagged),
    ]

    if cert_block := render_certifications(certs):
        lines.append(cert_block)

    if media_block := render_media(media):
        lines += ["### Supplementary resources", "", media_block]

    return "\n".join(lines)


def render_footer(roadmap: Roadmap, catalog: CourseCatalog, media: MediaLibrary) -> str:
    n_courses = sum(
        len(pc.free_courses) + len(pc.paid_courses) for pc in catalog.curated_courses
    )
    n_free = sum(len(pc.free_courses) for pc in catalog.curated_courses)
    n_media = sum(
        len(pm.articles_and_blogs) + len(pm.youtube_content) + len(pm.interactive_or_docs)
        for pm in media.media_resources
    )
    n_certs = len(
        [c for c in catalog.certifications if c.relevance is not Relevance.NOT_APPLICABLE]
    )

    return "\n".join(
        [
            "---",
            "",
            "## Summary",
            "",
            "| | |",
            "|---|---|",
            f"| Courses | {n_courses} ({n_free} free) |",
            f"| Certifications | {n_certs} |",
            f"| Supplementary resources | {n_media} |",
            f"| Total effort | {roadmap.total_estimated_hours} hours "
            f"over {roadmap.total_estimated_weeks} weeks |",
            "",
            "<sub>Generated by [Skill Forge]"
            "(https://github.com/saiaipm/Skill-Forge-Agent) — a multi-agent "
            "learning roadmap generator. Every URL was checked for reachability "
            "at generation time; links can still rot afterwards.</sub>",
            "",
        ]
    )


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def render_document(
    request: RoadmapRequest,
    roadmap: Roadmap,
    catalog: CourseCatalog,
    media: MediaLibrary,
    executive_summary: str = "",
    getting_started: str = "",
    flagged_urls: frozenset[str] | None = None,
) -> str:
    """Assemble the final Markdown document.

    Args:
        executive_summary: LLM-written prose. Omitted cleanly if empty, so the
            renderer stays usable when the synthesis agent fails.
        getting_started: Likewise.
        flagged_urls: URLs known to be unreachable that could not be removed
            without violating a schema guarantee. Marked in the output.
    """
    flagged = flagged_urls or frozenset()
    courses_by_phase = {pc.phase_id: pc for pc in catalog.curated_courses}
    media_by_phase = {pm.phase_id: pm for pm in media.media_resources}
    certs_by_phase: dict[str, list[Certification]] = {}
    for cert in catalog.certifications:
        certs_by_phase.setdefault(cert.phase_id, []).append(cert)

    parts = [render_header(roadmap, request)]

    if executive_summary.strip():
        parts.append("## Overview\n\n" + executive_summary.strip() + "\n")

    parts.append(render_contents(roadmap))

    if getting_started.strip():
        parts.append(
            "## Getting started\n\n" + _split_numbered_steps(getting_started) + "\n"
        )

    parts.append("---\n")

    for phase in roadmap.phases:
        parts.append(
            render_phase(
                phase,
                courses_by_phase.get(phase.phase_id),
                media_by_phase.get(phase.phase_id),
                certs_by_phase.get(phase.phase_id, []),
                flagged,
            )
        )

    parts.append(render_footer(roadmap, catalog, media))
    if flagged:
        parts.append(f"\n> {UNREACHABLE_LEGEND}\n")
    return "\n".join(parts)
