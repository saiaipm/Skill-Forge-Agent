"""Enforced link verification (design doc QA rule #1).

The agents are given a `verify_links` tool and told to use it. That is not a
guarantee, and the difference matters. When CrewAI cuts an agent off with
"Maximum iterations reached. Requesting final answer.", the agent produces its
best remaining guess — and an agent that has not finished searching will invent
resources rather than return fewer. An observed run produced nine dead links
across phases 3 and 4, including `martinfowler.com/articles/scalable-ai.html`
and `hbr.org/2024/01/leading-ai-strategy`, neither of which has ever existed.
The verification step was simply skipped, because nothing required it.

So verification happens here instead, after the agents are done, on every URL
in the payload, whether or not the agent claims to have checked it. Dead links
are removed; the caller is told what went.

Two deliberate choices:

**UNVERIFIED is kept.** A 403 means the checker was blocked by bot protection,
not that the page is missing. Udemy, Coursera, and Cloudflare-fronted sites all
do this, and dropping them would strip exactly the reputable platforms the
design doc requires.

**Pruning never breaks the contract.** If removing dead links would leave a
phase without the free course the schema demands, the phase is left intact and
a loud warning is raised instead. A roadmap that fails validation is worse than
one with a flagged bad link, and silently emitting an invalid payload is worse
than both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from skill_forge.schemas import CourseCatalog, MediaLibrary
from skill_forge.tools.link_verifier import (
    LinkStatus,
    check_urls,
    verification_enabled,
)


@dataclass
class VerificationReport:
    checked: int = 0
    alive: int = 0
    unverified: int = 0
    dead_urls: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Dead links that could not be removed without making the payload invalid —
    #: a phase's only free course, for instance. The renderer marks these in the
    #: document, because silently shipping a link known to be broken is worse
    #: than shipping a visibly flagged one.
    kept_dead: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    def summary(self) -> str:
        if self.skipped:
            return "link verification skipped (SKIP_LINK_VERIFY=1)"
        parts = [f"{self.checked} links checked", f"{self.alive} alive"]
        if self.unverified:
            parts.append(f"{self.unverified} unverified")
        if self.dead_urls:
            parts.append(f"{len(self.dead_urls)} dead")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        return ", ".join(parts)


def collect_urls(catalog: CourseCatalog, media: MediaLibrary) -> list[str]:
    """Every URL that would appear in the final document."""
    urls: list[str] = []
    for pc in catalog.curated_courses:
        urls += [c.url for c in (*pc.free_courses, *pc.paid_courses)]
    urls += [c.official_url for c in catalog.certifications if c.official_url]
    for pm in media.media_resources:
        urls += [r.url for r in pm.articles_and_blogs]
        urls += [r.url for r in pm.youtube_content]
        urls += [r.url for r in pm.interactive_or_docs]
    return list(dict.fromkeys(urls))


def _prune_media(media: MediaLibrary, dead: set[str]) -> tuple[MediaLibrary, list[str]]:
    """Drop dead resources. A phase emptied entirely is reported, not dropped."""
    removed: list[str] = []
    phases: list[dict] = []

    for pm in media.media_resources:
        data = pm.model_dump()
        for key in ("articles_and_blogs", "youtube_content", "interactive_or_docs"):
            kept = [item for item in data[key] if item["url"] not in dead]
            removed += [item["url"] for item in data[key] if item["url"] in dead]
            data[key] = kept
        phases.append(data)

    try:
        return MediaLibrary.model_validate({"media_resources": phases}), removed
    except ValidationError:
        # Pruning emptied a phase completely; the original is the lesser evil.
        return media, []


def _prune_catalog(
    catalog: CourseCatalog, dead: set[str]
) -> tuple[CourseCatalog, list[str], list[str], list[str]]:
    """Drop dead courses and certifications, preserving schema minimums."""
    removed: list[str] = []
    warnings: list[str] = []
    kept_dead: list[str] = []
    phases: list[dict] = []

    for pc in catalog.curated_courses:
        data = pc.model_dump()
        free = [c for c in data["free_courses"] if c["url"] not in dead]
        paid = [c for c in data["paid_courses"] if c["url"] not in dead]

        # min_length=1 on both lists is the design doc's hardest guarantee.
        # Removing the last one would make the payload unconstructable, so the
        # dead link stays and is flagged loudly instead.
        if not free:
            stranded = data["free_courses"][0]["url"]
            kept_dead.append(stranded)
            warnings.append(
                f"{pc.phase_id}: the only free course is unreachable "
                f"({stranded}) — kept and flagged in the document"
            )
            free = data["free_courses"]
        else:
            removed += [c["url"] for c in data["free_courses"] if c["url"] in dead]

        if not paid:
            stranded = data["paid_courses"][0]["url"]
            kept_dead.append(stranded)
            warnings.append(
                f"{pc.phase_id}: the only paid course is unreachable "
                f"({stranded}) — kept and flagged in the document"
            )
            paid = data["paid_courses"]
        else:
            removed += [c["url"] for c in data["paid_courses"] if c["url"] in dead]

        data["free_courses"], data["paid_courses"] = free, paid
        phases.append(data)

    certs = []
    for cert in catalog.certifications:
        if cert.official_url and cert.official_url in dead:
            removed.append(cert.official_url)
            warnings.append(
                f"dropped certification {cert.cert_name!r} — its official URL "
                f"does not resolve, so it cannot be confirmed to exist"
            )
            continue
        certs.append(cert.model_dump())

    try:
        pruned = CourseCatalog.model_validate(
            {"curated_courses": phases, "certifications": certs}
        )
    except ValidationError as e:
        warnings.append(f"could not prune catalog safely, left unchanged: {e}")
        return catalog, [], sorted(dead), warnings

    return pruned, removed, kept_dead, warnings


def verify_and_prune(
    catalog: CourseCatalog, media: MediaLibrary
) -> tuple[CourseCatalog, MediaLibrary, VerificationReport]:
    """Check every URL and remove the dead ones.

    This runs regardless of whether the agents used their `verify_links` tool,
    because whether they did cannot be known and, when they are cut off mid-run,
    they demonstrably do not.
    """
    report = VerificationReport()

    if not verification_enabled():
        report.skipped = True
        return catalog, media, report

    urls = collect_urls(catalog, media)
    if not urls:
        return catalog, media, report

    results = check_urls(urls)
    report.checked = len(results)
    report.alive = sum(1 for r in results if r.status is LinkStatus.ALIVE)
    report.unverified = sum(1 for r in results if r.status is LinkStatus.UNVERIFIED)
    dead = {r.url for r in results if r.status is LinkStatus.DEAD}
    report.dead_urls = sorted(dead)

    if not dead:
        return catalog, media, report

    pruned_catalog, cat_removed, kept_dead, cat_warnings = _prune_catalog(catalog, dead)
    pruned_media, media_removed = _prune_media(media, dead)

    report.removed = cat_removed + media_removed
    report.kept_dead = kept_dead
    report.warnings = cat_warnings
    return pruned_catalog, pruned_media, report
