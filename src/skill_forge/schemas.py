"""Inter-agent data contracts for Skill Forge.

Every payload passed between agents is validated against these models. The
design goal is that a guardrail stated in the system design doc becomes a
*hard* constraint here rather than a polite request in a prompt — an LLM will
ignore "MUST include a free course" often enough to matter, but it cannot
produce a `PhaseCourses` without one.

Three families of rules live here:

1. **Shape** — field types, URL well-formedness, required keys.
2. **Guardrails** — the explicit MUSTs from the design doc (a free option per
   phase, no vague topic titles, certifications either real or marked N/A).
3. **Cross-payload alignment** — `validate_phase_alignment` checks that the
   phase IDs invented by the Curriculum Architect are the exact set referenced
   by the Course Curator and Media Miner. See QA rule #2.
"""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    field_validator,
    model_validator,
)

_HTTP_URL = TypeAdapter(HttpUrl)


def _validate_url(value: str) -> str:
    """Validate as a URL but keep the value a plain ``str``.

    Using ``HttpUrl`` as a field type means the parsed model holds ``HttpUrl``
    objects, and those are not JSON-serialisable by ``json.dumps``. Any code
    path that reaches for ``model_dump()`` without ``mode="json"`` — including
    several inside CrewAI's output handling — then dies with
    "Object of type HttpUrl is not JSON serializable", far from the cause.

    Validating through a ``TypeAdapter`` and returning the original string keeps
    the strictness while making every payload serialisable by construction.
    """
    _HTTP_URL.validate_python(value)
    return value


#: A URL: validated as strictly as ``HttpUrl``, stored as ``str``.
Url = Annotated[str, AfterValidator(_validate_url)]

# --------------------------------------------------------------------------- #
# Shared vocabulary
# --------------------------------------------------------------------------- #

PHASE_ID_PATTERN = re.compile(r"^phase_[1-9]\d*$")

#: Placeholder titles that indicate the model punted instead of committing to a
#: concrete concept. Enforces the Curriculum Architect's "no vague titles"
#: guardrail. Matched case-insensitively against the whole trimmed string.
VAGUE_TITLES = frozenset(
    {
        "advanced stuff",
        "advanced topics",
        "and more",
        "best practices",
        "etc",
        "fundamentals",
        "learn more",
        "misc",
        "miscellaneous",
        "more topics",
        "other",
        "others",
        "tbd",
    }
)

#: Shapes of words that *look* specific but name no actual concept. Added after
#: observing a model emit "Understanding Git's performance optimization and
#: caching" and "Advanced Git branching strategies" — both slipped past an
#: exact-match blocklist while teaching the reader nothing about what to study.
#:
#: These are deliberately narrow. Over-broad patterns reject legitimate topics
#: ("Understanding the Git object model" is a fine thing to learn) and send the
#: agent into a retry loop, which costs more than the occasional weak concept.
VAGUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Advanced X strategies", "Basic Y concepts", "Intermediate Z things"
    re.compile(
        r"^(advanced|basic|intermediate|general)\b.*\b"
        r"(topics?|stuff|things|concepts?|strategies|techniques|practices)$",
        re.I,
    ),
    # "Understanding X's features", "Learning Y basics"
    re.compile(
        r"^(understanding|learning|exploring|introduction to)\b.*\b"
        r"(features?|basics?|fundamentals?|concepts?|essentials?)$",
        re.I,
    ),
    # Trailing hedges: "... and more", "... etc."
    re.compile(r"\b(and more|and others|etc\.?)$", re.I),
)


class ExperienceLevel(str, Enum):
    BEGINNER = "Beginner"
    INTERMEDIATE = "Intermediate"
    ADVANCED = "Advanced"


class CostType(str, Enum):
    FREE = "Free"
    PAID = "Paid"


class Relevance(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    NOT_APPLICABLE = "N/A"


class MediaFormat(str, Enum):
    PLAYLIST = "Playlist"
    FULL_COURSE = "Full Course"
    SPECIFIC_VIDEO = "Specific Video"


class ResourceKind(str, Enum):
    DOCUMENTATION = "Documentation"
    GITHUB_REPO = "GitHub Repo"
    INTERACTIVE_SANDBOX = "Interactive Sandbox"


class StrictModel(BaseModel):
    """Base model that refuses unknown fields.

    Silently dropping an unexpected key is how a payload quietly loses data
    between agents. Better to fail loudly and let the orchestrator retry.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _reject_vague(value: str, field_name: str) -> str:
    cleaned = value.strip().rstrip(".")
    if cleaned.lower() in VAGUE_TITLES:
        raise ValueError(
            f"{field_name} {value!r} is a placeholder, not a concrete topic. "
            "State the actual concept being learned."
        )
    for pattern in VAGUE_PATTERNS:
        if pattern.search(cleaned):
            raise ValueError(
                f"{field_name} {value!r} names no specific concept. Replace the "
                "generic wording with what is actually studied — e.g. "
                "'Git object model and SHA-1 addressing' rather than "
                "'Understanding Git internals'."
            )
    return value


# --------------------------------------------------------------------------- #
# Agent 1 — Curriculum Architect
# --------------------------------------------------------------------------- #


class RoadmapRequest(StrictModel):
    """User-supplied input that kicks off the whole pipeline."""

    topic: str = Field(min_length=2, max_length=200)
    user_experience_level: ExperienceLevel = ExperienceLevel.BEGINNER
    target_time_commitment_hrs_per_week: int = Field(default=10, ge=1, le=80)


class MilestoneProject(StrictModel):
    """The hands-on deliverable that proves a phase was actually absorbed."""

    title: str = Field(min_length=3, max_length=200)
    #: 40 rather than 20: a description shorter than this is invariably the
    #: title restated, which tells a learner nothing about what to build.
    description: str = Field(min_length=40, max_length=2000)

    @field_validator("title")
    @classmethod
    def _no_vague_title(cls, v: str) -> str:
        return _reject_vague(v, "Milestone project title")


class Phase(StrictModel):
    """One of the four Zero-to-Hero stages."""

    phase_id: str
    phase_name: str = Field(min_length=3, max_length=120)
    estimated_hours: int = Field(ge=1, le=1000)
    #: 60 rather than 20. At 20 characters a model will happily return the phase
    #: name reworded ("Develop intermediate competence in Git"), which passes
    #: validation while conveying nothing.
    summary: str = Field(min_length=60, max_length=2000)
    #: Floor raised from 3 to 5: three concepts cannot cover a phase of a
    #: multi-week curriculum, and models settle at whatever minimum is allowed.
    core_concepts: list[str] = Field(min_length=5, max_length=15)
    milestone_project: MilestoneProject

    @field_validator("phase_id")
    @classmethod
    def _phase_id_format(cls, v: str) -> str:
        if not PHASE_ID_PATTERN.match(v):
            raise ValueError(f"phase_id must look like 'phase_1', got {v!r}")
        return v

    @field_validator("core_concepts")
    @classmethod
    def _concepts_are_concrete(cls, v: list[str]) -> list[str]:
        for concept in v:
            _reject_vague(concept, "Core concept")
        if len({c.lower() for c in v}) != len(v):
            raise ValueError("core_concepts contains duplicates")
        return v


class Roadmap(StrictModel):
    """Agent 1's output — the spine every downstream agent hangs work off."""

    roadmap_title: str = Field(min_length=5, max_length=200)
    target_domain: str = Field(min_length=2, max_length=200)
    total_estimated_weeks: int = Field(ge=1, le=520)
    #: Not in the original design doc's output schema. Added because a week
    #: count is meaningless without the pace it assumes, and without this field
    #: the roadmap cannot check its own arithmetic — a model returned "26 weeks"
    #: for 100 hours at 10 hrs/week and nothing caught it.
    weekly_hours: int = Field(ge=1, le=80)
    phases: list[Phase] = Field(min_length=4, max_length=4)

    @field_validator("phases")
    @classmethod
    def _sequential_unique_ids(cls, v: list[Phase]) -> list[Phase]:
        got = [p.phase_id for p in v]
        expected = [f"phase_{i}" for i in range(1, len(v) + 1)]
        if got != expected:
            raise ValueError(
                f"phases must be ordered and contiguous; expected {expected}, got {got}"
            )
        return v

    @model_validator(mode="after")
    def _weeks_match_the_hours(self) -> Roadmap:
        """Reject a schedule that contradicts its own numbers.

        Tolerance of one week absorbs legitimate rounding disagreements without
        letting through the kind of drift that makes a plan untrustworthy.
        """
        expected = math.ceil(self.total_estimated_hours / self.weekly_hours)
        if abs(self.total_estimated_weeks - expected) > 1:
            raise ValueError(
                f"total_estimated_weeks={self.total_estimated_weeks} contradicts "
                f"the phase hours: {self.total_estimated_hours}h at "
                f"{self.weekly_hours}h/week is {expected} weeks. Either correct "
                f"the week count or adjust the per-phase estimated_hours."
            )
        return self

    @property
    def phase_ids(self) -> list[str]:
        return [p.phase_id for p in self.phases]

    @property
    def total_estimated_hours(self) -> int:
        return sum(p.estimated_hours for p in self.phases)


# --------------------------------------------------------------------------- #
# Agent 2 — Course & Certification Curator
# --------------------------------------------------------------------------- #


#: Hosts that serve videos, not courses. A curator that returns these has
#: confused its job with the Media Miner's — and, observed in practice, will
#: invent a price for a free YouTube video ("$15-$25") to satisfy the paid-course
#: requirement. Rejecting the URL outright is the only reliable stop.
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com")


class Course(StrictModel):
    title: str = Field(min_length=3, max_length=300)
    provider: str = Field(min_length=2, max_length=120)
    url: Url
    cost_type: CostType
    #: Free courses omit this; paid courses must state something ("$15-$25",
    #: "Subscription", "$49/mo"). Kept as free text because platform pricing
    #: is wildly inconsistent and a float would imply precision we don't have.
    price_range: str | None = Field(default=None, max_length=80)
    instructor: str | None = Field(default=None, max_length=200)
    rating_note: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _not_a_video_link(cls, v: str) -> str:
        if any(host in v.lower() for host in VIDEO_HOSTS):
            raise ValueError(
                f"{v} is a video, not a course. Videos belong in the media "
                "resources, never in free_courses or paid_courses. Find a real "
                "course on a learning platform (freeCodeCamp, edX, Coursera, "
                "Udemy, Pluralsight, or an official vendor academy)."
            )
        return v

    @model_validator(mode="after")
    def _paid_courses_state_a_price(self) -> Course:
        if self.cost_type is CostType.PAID and not self.price_range:
            raise ValueError("paid courses must include a price_range")
        return self


class Certification(StrictModel):
    """An official, vendor-recognised credential — or an explicit N/A.

    The design doc forbids passing off course-completion certificates as
    industry certifications, and requires an explicit justification when a
    topic genuinely has none (niche hobbies, emerging tech).
    """

    phase_id: str
    cert_name: str = Field(min_length=1, max_length=200)
    issuing_body: str = Field(min_length=1, max_length=200)
    relevance: Relevance
    official_url: Url | None = None
    prerequisites: str | None = Field(default=None, max_length=1000)
    #: Required when the credential is N/A — explains *why* none applies.
    justification: str | None = Field(default=None, max_length=1000)

    @field_validator("phase_id")
    @classmethod
    def _phase_id_format(cls, v: str) -> str:
        if not PHASE_ID_PATTERN.match(v):
            raise ValueError(f"phase_id must look like 'phase_1', got {v!r}")
        return v

    @model_validator(mode="after")
    def _real_cert_or_explained_absence(self) -> Certification:
        """An entry is either a real credential or an explicit absence — never both.

        The half-way state is the one that actually occurs: an agent wanting to
        cite something adjacent to a certification (an observed run tried the
        NIST AI Risk Management Framework, which is a framework, not a
        credential) uses the N/A escape hatch while still attaching a URL and a
        non-N/A relevance. That renders as a hyperlink captioned "N/A".
        """
        na_fields = {
            "cert_name": self.cert_name == "N/A",
            "issuing_body": self.issuing_body == "N/A",
            "relevance": self.relevance is Relevance.NOT_APPLICABLE,
        }

        if any(na_fields.values()):
            if not all(na_fields.values()):
                stated = [k for k, v in na_fields.items() if v]
                missing = [k for k, v in na_fields.items() if not v]
                raise ValueError(
                    f"this entry is N/A in {stated} but not in {missing}. An entry "
                    "is either a real credential — with a real cert_name, "
                    "issuing_body, relevance, and official_url — or an explicit "
                    "absence with all of cert_name, issuing_body, and relevance "
                    "set to 'N/A'. To cite a framework, standard, or reading that "
                    "is not a certification, leave it out of certifications "
                    "entirely; it belongs in the media resources."
                )
            if not self.justification:
                raise ValueError(
                    "a certification marked N/A must carry a justification"
                )
            if self.official_url is not None:
                raise ValueError(
                    f"an N/A entry cannot have an official_url ({self.official_url}). "
                    "If a real credential exists at that URL, name it properly; if "
                    "the link is a framework or guide rather than a credential, it "
                    "belongs in the media resources instead."
                )
        elif self.official_url is None:
            raise ValueError(
                f"certification {self.cert_name!r} needs an official_url; if no real "
                "credential exists, mark cert_name, issuing_body, and relevance all "
                "as 'N/A' with a justification instead"
            )
        return self


class PhaseCourses(StrictModel):
    """Curated courses for a single phase.

    The min_length=1 on both lists is the design doc's hardest guardrail:
    *every* phase must be completable for free, and must also offer a
    high-quality paid path.
    """

    phase_id: str
    free_courses: list[Course] = Field(min_length=1, max_length=6)
    paid_courses: list[Course] = Field(min_length=1, max_length=6)

    @field_validator("phase_id")
    @classmethod
    def _phase_id_format(cls, v: str) -> str:
        if not PHASE_ID_PATTERN.match(v):
            raise ValueError(f"phase_id must look like 'phase_1', got {v!r}")
        return v

    @model_validator(mode="after")
    def _cost_types_match_their_bucket(self) -> PhaseCourses:
        for course in self.free_courses:
            if course.cost_type is not CostType.FREE:
                raise ValueError(f"{course.title!r} sits in free_courses but is Paid")
        for course in self.paid_courses:
            if course.cost_type is not CostType.PAID:
                raise ValueError(f"{course.title!r} sits in paid_courses but is Free")
        return self

    @model_validator(mode="after")
    def _no_url_is_both_free_and_paid(self) -> PhaseCourses:
        """Catch the same link filed under both buckets.

        Observed: one video listed as free in one phase and $15-$25 in another.
        Within a phase that contradiction is checkable, so it is checked.
        """
        free_urls = {c.url for c in self.free_courses}
        clashes = sorted(free_urls & {c.url for c in self.paid_courses})
        if clashes:
            raise ValueError(
                f"the same URL appears as both free and paid in {self.phase_id}: "
                f"{clashes}. A course is one or the other — decide which."
            )
        return self


class CourseCatalog(StrictModel):
    """Agent 2's complete output."""

    curated_courses: list[PhaseCourses] = Field(min_length=1)
    certifications: list[Certification] = Field(default_factory=list)

    @model_validator(mode="after")
    def _phases_are_not_carbon_copies(self) -> CourseCatalog:
        """Reject a phase whose entire course set repeats another phase's.

        Partial overlap is legitimate — one comprehensive course can serve two
        phases. An identical set is not curation, it is the agent running out of
        effort and pasting its previous answer, which was observed for phases 3
        and 4 on a real run.
        """
        seen: dict[frozenset[str], str] = {}
        for pc in self.curated_courses:
            urls = frozenset(
                c.url for c in (*pc.free_courses, *pc.paid_courses)
            )
            if (earlier := seen.get(urls)) is not None:
                raise ValueError(
                    f"{pc.phase_id} recommends exactly the same courses as "
                    f"{earlier}. Each phase covers different material, so find "
                    f"resources specific to {pc.phase_id}'s concepts."
                )
            seen[urls] = pc.phase_id
        return self


# --------------------------------------------------------------------------- #
# Agent 3 — Resource & Media Mining
# --------------------------------------------------------------------------- #


class Article(StrictModel):
    title: str = Field(min_length=3, max_length=300)
    author_publisher: str = Field(min_length=2, max_length=200)
    url: Url
    key_takeaway: str = Field(min_length=10, max_length=500)


#: A YouTube video ID is exactly 11 characters of [A-Za-z0-9_-]. This is the
#: only structural check available for these links, and it matters more than it
#: looks: YouTube answers an invalid ID with HTTP 200 and a "video unavailable"
#: page, so a fabricated watch URL passes link verification cleanly. An observed
#: run emitted watch?v=1a2b3c4d5e6f and watch?v=policy123456 — both 12
#: characters, both "reachable", both invented.
YOUTUBE_WATCH = re.compile(r"youtube\.com/watch\?(?:.*&)?v=([^&#]+)", re.I)
YOUTUBE_SHORT = re.compile(r"youtu\.be/([^?&#/]+)", re.I)
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class YoutubeContent(StrictModel):
    title: str = Field(min_length=3, max_length=300)
    channel_name: str = Field(min_length=1, max_length=200)
    url: Url
    format: MediaFormat
    duration: str | None = Field(default=None, max_length=60)
    key_takeaway: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _plausible_youtube_id(cls, v: str) -> str:
        for pattern in (YOUTUBE_WATCH, YOUTUBE_SHORT):
            if match := pattern.search(v):
                video_id = match.group(1)
                if not YOUTUBE_ID.match(video_id):
                    raise ValueError(
                        f"{video_id!r} is not a valid YouTube video ID (must be "
                        f"exactly 11 characters of letters, digits, hyphen, or "
                        f"underscore; this is {len(video_id)}). Use a real URL "
                        "returned by the video_search tool — do not construct or "
                        "guess one."
                    )
        return v


class InteractiveResource(StrictModel):
    title: str = Field(min_length=3, max_length=300)
    url: Url
    type: ResourceKind
    key_takeaway: str | None = Field(default=None, max_length=500)


class PhaseMedia(StrictModel):
    """Supplementary material for one phase.

    Caps are the design doc's anti-overload rule (3-5 per category). The
    minimum is deliberately looser than the maximum: a genuinely obscure topic
    may only have two good videos, and failing an expensive run over that would
    be worse than shipping a slightly thin section.
    """

    phase_id: str
    articles_and_blogs: list[Article] = Field(default_factory=list, max_length=5)
    youtube_content: list[YoutubeContent] = Field(default_factory=list, max_length=5)
    interactive_or_docs: list[InteractiveResource] = Field(
        default_factory=list, max_length=5
    )

    @field_validator("phase_id")
    @classmethod
    def _phase_id_format(cls, v: str) -> str:
        if not PHASE_ID_PATTERN.match(v):
            raise ValueError(f"phase_id must look like 'phase_1', got {v!r}")
        return v

    @model_validator(mode="after")
    def _phase_is_not_empty(self) -> PhaseMedia:
        if not (
            self.articles_and_blogs or self.youtube_content or self.interactive_or_docs
        ):
            raise ValueError(f"{self.phase_id} has no media resources at all")
        return self


class MediaLibrary(StrictModel):
    """Agent 3's complete output."""

    media_resources: list[PhaseMedia] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Agent 4 — final assembled payload
# --------------------------------------------------------------------------- #


#: Openings that signal filler rather than information. The synthesis agent is
#: told to avoid these; this is the enforcement.
BANNED_OPENINGS = (
    "embark on",
    "dive into",
    "unlock",
    "in today's fast-paced",
    "in today's world",
    "whether you are a beginner",
    "whether you're a beginner",
    "buckle up",
    "let's get started on your journey",
)


class DocumentPreamble(StrictModel):
    """The only prose Agent 4 writes.

    Everything structural is rendered from data by ``skill_forge.render``, so
    the model cannot drop a course by forgetting to mention it. What is left is
    genuinely generative: orientation, and a first concrete action.
    """

    executive_summary: str = Field(min_length=150, max_length=1500)
    getting_started: str = Field(min_length=150, max_length=2000)

    @field_validator("executive_summary", "getting_started")
    @classmethod
    def _no_filler_openings(cls, v: str) -> str:
        opening = v.strip().lower()
        for phrase in BANNED_OPENINGS:
            if opening.startswith(phrase):
                raise ValueError(
                    f"opens with {phrase!r}, which conveys nothing. Begin with a "
                    "concrete statement about what the learner will be able to do."
                )
        return v


class SkillForgeBundle(StrictModel):
    """Everything Agent 4 needs, already proven mutually consistent."""

    request: RoadmapRequest
    roadmap: Roadmap
    catalog: CourseCatalog
    media: MediaLibrary

    @model_validator(mode="after")
    def _aligned(self) -> SkillForgeBundle:
        validate_phase_alignment(self.roadmap, self.catalog, self.media)
        return self


# --------------------------------------------------------------------------- #
# Cross-payload validation (QA rule #2)
# --------------------------------------------------------------------------- #


class PhaseAlignmentError(ValueError):
    """Raised when downstream agents drift from the Architect's phase IDs."""


def _check_coverage(
    expected: list[str], got: list[str], *, source: str
) -> list[str]:
    problems: list[str] = []

    duplicates = {pid for pid in got if got.count(pid) > 1}
    if duplicates:
        problems.append(f"{source} has duplicate entries for {sorted(duplicates)}")

    missing = [pid for pid in expected if pid not in got]
    if missing:
        problems.append(f"{source} is missing {missing}")

    unknown = [pid for pid in got if pid not in expected]
    if unknown:
        problems.append(
            f"{source} references unknown phase IDs {sorted(set(unknown))} "
            f"(valid: {expected})"
        )

    return problems


def validate_phase_alignment(
    roadmap: Roadmap,
    catalog: CourseCatalog | None = None,
    media: MediaLibrary | None = None,
) -> None:
    """Assert downstream payloads reference exactly the Architect's phases.

    Catches the failure the design doc's QA rule #2 is aimed at: an agent
    inventing ``phase_5``, skipping a phase, or silently renumbering — any of
    which produces a final document with orphaned or missing sections.

    Certifications are checked for *validity* but not *coverage*: the doc maps
    certs only to the phases where they make sense (often just phase 3+), so a
    phase without one is expected.

    Raises:
        PhaseAlignmentError: with every problem found, not just the first.
    """
    expected = roadmap.phase_ids
    problems: list[str] = []

    if catalog is not None:
        problems += _check_coverage(
            expected,
            [pc.phase_id for pc in catalog.curated_courses],
            source="CourseCatalog.curated_courses",
        )
        stray_certs = sorted(
            {c.phase_id for c in catalog.certifications if c.phase_id not in expected}
        )
        if stray_certs:
            problems.append(
                f"CourseCatalog.certifications references unknown phase IDs "
                f"{stray_certs} (valid: {expected})"
            )

    if media is not None:
        problems += _check_coverage(
            expected,
            [pm.phase_id for pm in media.media_resources],
            source="MediaLibrary.media_resources",
        )

    if problems:
        raise PhaseAlignmentError(
            "Phase IDs are not aligned across agents:\n  - "
            + "\n  - ".join(problems)
        )
