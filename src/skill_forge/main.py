"""Command-line entry point.

    uv run skill-forge "Kubernetes" --level Beginner --hours 10
    uv run skill-forge "Rust" --output my-rust-plan.md --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pydantic import ValidationError

from skill_forge.crew import PipelineError, generate_roadmap
from skill_forge.llm import LLMConfigError, build_llm, load_settings
from skill_forge.schemas import ExperienceLevel, RoadmapRequest

OUTPUT_DIR = Path("output")


def slugify(text: str) -> str:
    """Turn a topic into a filename stem."""
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:60] or "roadmap"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-forge",
        description="Generate a Zero-to-Hero learning roadmap for any skill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  skill-forge "Kubernetes"\n'
            '  skill-forge "Rust" --level Intermediate --hours 6\n'
            '  skill-forge "Data engineering" --output plan.md --verbose\n'
        ),
    )
    parser.add_argument("topic", help="The skill to build a roadmap for.")
    parser.add_argument(
        "--level",
        choices=[e.value for e in ExperienceLevel],
        default=ExperienceLevel.BEGINNER.value,
        help="Learner's starting point (default: Beginner).",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=10,
        metavar="N",
        help="Study hours available per week (default: 10).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="Where to write the Markdown (default: output/<topic>.md).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream agent reasoning and tool calls.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        request = RoadmapRequest(
            topic=args.topic,
            user_experience_level=args.level,
            target_time_commitment_hrs_per_week=args.hours,
        )
    except ValidationError as e:
        print("Invalid input:", file=sys.stderr)
        for err in e.errors():
            field = ".".join(str(p) for p in err["loc"])
            print(f"  {field}: {err['msg']}", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        llm = build_llm()
    except LLMConfigError as e:
        print(f"Configuration problem:\n{e}", file=sys.stderr)
        return 1

    print(f"Skill Forge — {request.topic}")
    print(f"  model  : {settings.describe()}")
    print(
        f"  learner: {request.user_experience_level.value}, "
        f"{request.target_time_commitment_hrs_per_week} hrs/week"
    )
    print()

    try:
        report = generate_roadmap(request, llm=llm, verbose=args.verbose)
    except PipelineError as e:
        print(f"\nPipeline failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    destination = args.output or OUTPUT_DIR / f"{slugify(request.topic)}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report.markdown)

    roadmap = report.roadmap
    assert roadmap is not None  # generate_roadmap raises rather than return None
    course_total = sum(
        len(pc.free_courses) + len(pc.paid_courses)
        for pc in (report.catalog.curated_courses if report.catalog else [])
    )
    media_total = sum(
        len(pm.articles_and_blogs) + len(pm.youtube_content) + len(pm.interactive_or_docs)
        for pm in (report.media.media_resources if report.media else [])
    )

    print(f"\n{roadmap.roadmap_title}")
    print(
        f"  {roadmap.total_estimated_hours} hours over "
        f"{roadmap.total_estimated_weeks} weeks · {len(roadmap.phases)} phases"
    )
    print(f"  {course_total} courses · {media_total} supplementary resources")
    if report.links is not None:
        print(f"  links: {report.links.summary()}")
    print(f"  {report.summary()}")

    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)

    print(f"\nWritten to {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
