"""Validation guardrails that give an agent a chance to fix its own output.

Why this module exists, in one paragraph:

CrewAI's ``output_pydantic`` looks like it validates agent output, and it does —
but a ``ValidationError`` there is *fatal*. ``handle_partial_json`` in
``crewai/utilities/converter.py`` re-raises it, and only malformed JSON gets a
repair attempt; a well-formed payload that violates the schema kills the run.
That makes every constraint in ``schemas.py`` a crash rather than a correction.

``Task.guardrail`` behaves differently. ``_execute_core`` skips ``_export_output``
entirely when a guardrail is present, and on failure feeds the guardrail's error
message back to the agent as context and re-runs it, up to
``guardrail_max_retries`` times. Same validation, but the agent is told what it
got wrong and given another attempt.

So the strict models stay exactly as they are, and this module adapts them into
guardrails. On success the guardrail returns the validated payload as a JSON
*string*, which CrewAI stores as ``task_output.raw``; `typed_output` turns that
back into a model at the end.

One further trap, and the reason guarded tasks here do **not** set
``output_pydantic`` at all. CrewAI's retry loop regenerates the agent's output
and then calls ``_export_output()`` on it directly (task.py ~line 1151) without
consulting the guardrail first. If ``output_pydantic`` is set, a second failed
attempt raises fatally there — so the guardrail protects attempt one and is
bypassed on every attempt after it, which is precisely when it is needed. Leave
``output_pydantic`` unset and that call becomes a no-op.
"""

# NOTE: deliberately no `from __future__ import annotations` in this module.
# CrewAI validates a guardrail by introspecting its return annotation with
# get_origin()/get_args(). Postponed evaluation turns that annotation into the
# *string* "tuple[bool, Any]", get_origin() returns None, and Task construction
# fails with "If return type is annotated, it must be Tuple[bool, Any]" — an
# error that points nowhere near the actual cause. Re-adding the import here
# will break every task that uses a guardrail.

import json
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from skill_forge.schemas import PhaseAlignmentError

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Greedy match from the first brace to the last — agents wrap JSON in prose
#: ("Here is the roadmap: {...} Let me know if...") far more often than they
#: emit two separate objects.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

#: Pydantic error types whose default message is unhelpfully terse for an LLM.
_FRIENDLY_HINTS = {
    "too_short": "add more items to this list",
    "too_long": "remove items until the list is within the limit",
    "string_too_short": "write a longer, more substantial value",
    "missing": "this required field was omitted entirely",
    "extra_forbidden": "remove this field; it is not part of the schema",
    "url_parsing": "provide a complete URL including https://",
}


def extract_json(raw: str) -> str | None:
    """Pull a JSON object out of an agent's reply.

    Handles the three shapes agents actually produce: bare JSON, JSON inside
    markdown fences, and JSON surrounded by conversational padding.
    """
    text = raw.strip()

    if "```" in text:
        # Take the largest fenced block; agents sometimes fence an example first.
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if blocks:
            text = max(blocks, key=len).strip()

    if text.startswith("{") and text.endswith("}"):
        return text

    match = _JSON_BLOCK.search(text)
    return match.group() if match else None


def format_errors(exc: ValidationError, limit: int = 8) -> str:
    """Turn a Pydantic error dump into instructions a mid-size model can follow.

    Pydantic's native rendering includes URLs, input echoes, and type codes that
    consume context without helping. This keeps the field path and the fix.
    """
    lines: list[str] = []
    errors = exc.errors()
    for err in errors[:limit]:
        location = ".".join(str(p) for p in err["loc"]) or "(root)"
        message = err.get("msg", "invalid")
        hint = _FRIENDLY_HINTS.get(err.get("type", ""))
        lines.append(f"  - {location}: {message}" + (f" — {hint}" if hint else ""))

    if len(errors) > limit:
        lines.append(f"  - ...and {len(errors) - limit} more problems")
    return "\n".join(lines)


def pydantic_guardrail(
    model: type[ModelT],
    *,
    expected_phase_ids: list[str] | None = None,
    extra_check: Callable[[ModelT], str | None] | None = None,
) -> Callable[[Any], tuple[bool, Any]]:
    """Build a guardrail that validates task output against ``model``.

    Args:
        model: The Pydantic contract the output must satisfy.
        expected_phase_ids: When given, the payload's phase references are
            checked against these IDs (design doc QA rule #2). Catches an agent
            inventing ``phase_5`` while there is still budget to retry, rather
            than at final assembly when the run is already paid for.
        extra_check: Optional semantic check run after schema validation.
            Returns an error string to reject, or None to accept.

    Returns:
        A callable matching CrewAI's guardrail contract: it receives the
        ``TaskOutput`` and returns ``(ok, payload_or_error)``.
    """

    def guardrail(output: Any) -> tuple[bool, Any]:
        raw = getattr(output, "raw", None) or str(output)

        payload = extract_json(raw)
        if payload is None:
            return False, (
                "No JSON object found in the response. Return ONLY a single JSON "
                f"object matching the {model.__name__} schema — no explanation "
                "before or after it, no markdown fences."
            )

        try:
            instance = model.model_validate_json(payload)
        except ValidationError as e:
            return False, (
                f"The JSON does not satisfy the {model.__name__} schema. Fix "
                f"these problems and return the corrected object:\n"
                f"{format_errors(e)}"
            )
        except json.JSONDecodeError as e:
            return False, (
                f"The response is not parseable JSON ({e.msg} at position "
                f"{e.pos}). Return a single well-formed JSON object."
            )

        if expected_phase_ids is not None:
            try:
                _check_phase_ids(instance, expected_phase_ids)
            except PhaseAlignmentError as e:
                return False, str(e)

        if extra_check is not None and (problem := extra_check(instance)):
            return False, problem

        # A JSON string, not the model: CrewAI feeds this back through
        # _export_output, which is what produces task_output.pydantic.
        return True, instance.model_dump_json()

    return guardrail


def typed_output(task: Any, model: type[ModelT]) -> ModelT | None:
    """Recover the validated model from a completed guarded task.

    Guarded tasks do not set ``output_pydantic`` (see the note in agents.py), so
    ``task.output.pydantic`` is always None. What they do have is
    ``task.output.raw``, which CrewAI replaces with whatever the guardrail
    returned on success — the validated payload as JSON. Parsing it here cannot
    fail for a task that completed, because the guardrail already validated the
    identical string.

    Returns None if the task never produced output.
    """
    output = getattr(task, "output", None)
    if output is None:
        return None

    raw = getattr(output, "raw", None)
    if not raw:
        return None

    payload = extract_json(str(raw))
    if payload is None:
        return None

    try:
        return model.model_validate_json(payload)
    except (ValidationError, json.JSONDecodeError):
        # Only reachable if CrewAI returned a task as successful without the
        # guardrail having passed — worth surfacing as "no output" rather than
        # crashing the run at the assembly step.
        return None


def _check_phase_ids(instance: BaseModel, expected: list[str]) -> None:
    """Verify a curator/miner payload references exactly the architect's phases."""
    from skill_forge.schemas import CourseCatalog, MediaLibrary, validate_phase_alignment
    from skill_forge.schemas import Roadmap

    # A stand-in roadmap carrying only the phase IDs, so the existing alignment
    # logic can be reused without duplicating its error formatting.
    class _PhaseIdsOnly:
        phase_ids = expected

    if isinstance(instance, CourseCatalog):
        validate_phase_alignment(_PhaseIdsOnly(), catalog=instance)  # type: ignore[arg-type]
    elif isinstance(instance, MediaLibrary):
        validate_phase_alignment(_PhaseIdsOnly(), media=instance)  # type: ignore[arg-type]
    elif isinstance(instance, Roadmap):
        if instance.phase_ids != expected:
            raise PhaseAlignmentError(
                f"phase IDs must be exactly {expected}, got {instance.phase_ids}"
            )
