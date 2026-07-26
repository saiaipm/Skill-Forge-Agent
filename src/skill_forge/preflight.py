"""Preflight checks — run before burning credits on a full pipeline run.

The pipeline asks a lot of its model: nested JSON matching a strict schema,
plus tool calling for the search agents. Whether a given model can actually do
that is worth finding out in one cheap request rather than four expensive ones.

    uv run python -m skill_forge.preflight

Exits non-zero if a required capability is missing.
"""

from __future__ import annotations

import json
import sys
import time

from skill_forge.llm import LLMConfigError, build_llm, load_settings
from skill_forge.schemas import Roadmap

PASS = "  PASS"
FAIL = "  FAIL"
WARN = "  WARN"

# Deliberately terse: we are testing schema conformance, not curriculum quality.
PROBE_PROMPT = """\
Produce a 4-phase "Zero to Hero" learning roadmap for the topic: Git version control.

Return ONLY a JSON object. No prose, no markdown fences. It must match exactly:

{schema}

Rules:
- phases must have phase_id exactly "phase_1", "phase_2", "phase_3", "phase_4"
- core_concepts: 3-15 concrete named concepts (never "Advanced Topics" or "Learn More")
- summary and milestone_project.description must each be at least 20 characters
- include no keys beyond those in the schema
"""


def _compact_schema() -> str:
    """Trim pydantic's JSON schema to what a mid-size model can actually follow."""
    schema = Roadmap.model_json_schema()
    return json.dumps(schema, separators=(",", ":"))


def _strip_fences(text: str) -> str:
    """Models love wrapping JSON in markdown fences despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    return t[start : end + 1] if start != -1 and end != -1 else t


def check_config() -> object | None:
    print("\n[1/4] Configuration")
    try:
        settings = load_settings()
    except LLMConfigError as e:
        print(f"{FAIL} {e}")
        return None
    print(f"{PASS} {settings.describe()}")
    return settings


def check_connectivity(llm) -> bool:
    print("\n[2/4] Connectivity")
    try:
        t0 = time.monotonic()
        reply = llm.call("Reply with exactly the word: ready")
        dt = time.monotonic() - t0
    except Exception as e:
        print(f"{FAIL} {type(e).__name__}: {str(e)[:300]}")
        return False
    print(f"{PASS} responded in {dt:.1f}s -> {str(reply).strip()[:60]!r}")
    return True


def check_function_calling(llm) -> bool:
    print("\n[3/4] Function calling (needed by the search agents)")
    supported = llm.supports_function_calling()
    if supported:
        print(f"{PASS} provider reports tool-call support")
    else:
        print(f"{WARN} no tool-call support — Agents 2 and 3 cannot search")
    return bool(supported)


def check_structured_output(llm) -> bool:
    print("\n[4/4] Structured output against the real Roadmap schema")
    prompt = PROBE_PROMPT.format(schema=_compact_schema())
    try:
        t0 = time.monotonic()
        raw = llm.call(prompt)
        dt = time.monotonic() - t0
    except Exception as e:
        print(f"{FAIL} request failed: {type(e).__name__}: {str(e)[:300]}")
        return False

    try:
        payload = json.loads(_strip_fences(str(raw)))
    except json.JSONDecodeError as e:
        print(f"{FAIL} not valid JSON ({e}). First 300 chars:\n{str(raw)[:300]}")
        return False
    print(f"{PASS} returned parseable JSON in {dt:.1f}s")

    try:
        roadmap = Roadmap.model_validate(payload)
    except Exception as e:
        # This is the interesting failure: the model can do JSON but not *our*
        # JSON. Retries in the real pipeline may still recover it.
        print(f"{WARN} JSON did not satisfy the Roadmap contract:")
        for line in str(e).splitlines()[:12]:
            print(f"       {line}")
        print(
            "\n       Not necessarily fatal — CrewAI retries on validation "
            "failure.\n       But expect slower runs and higher credit burn."
        )
        return False

    print(f"{PASS} validated: {roadmap.roadmap_title!r}")
    print(f"       phases: {', '.join(roadmap.phase_ids)}")
    print(f"       total:  {roadmap.total_estimated_hours}h across "
          f"{roadmap.total_estimated_weeks} weeks")
    return True


def main() -> int:
    print("=" * 68)
    print("Skill Forge preflight")
    print("=" * 68)

    settings = check_config()
    if settings is None:
        return 1

    llm = build_llm()

    if not check_connectivity(llm):
        return 1

    tools_ok = check_function_calling(llm)
    schema_ok = check_structured_output(llm)

    print("\n" + "=" * 68)
    if tools_ok and schema_ok:
        print("All checks passed — this model can drive the full pipeline.")
        return 0
    if not tools_ok:
        print("Blocked: without tool calling the search agents cannot run.")
        print("Switch provider in .env, or pick a tool-capable model.")
        return 1
    print("Usable with caveats: structured output needed retries.")
    print("Consider a stronger model for Agents 2-4 if failures persist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
