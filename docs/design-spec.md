# Skill Roadmap Agentic System — Architecture & Agent Specifications

> The original design specification for Skill Forge, written before
> implementation. Preserved here as the reference the build was measured
> against. A record of where the implementation deliberately departed from it
> follows at the end.

This document defines the multi-agent system architecture, agent
configurations, workflow execution model, and data schemas for a
**Zero-to-Hero Skill Roadmap Generator**.

## 1. System architecture overview

A **plan-and-execute DAG** topology with parallel execution where appropriate.
An orchestrator validates requirements, dispatches to domain-specialised
sub-agents, merges state, and hands the synthesised payload to a document
synthesis agent.

```
                    User input / request
                             |
                             v
                  Orchestrator / supervisor
                             |
                             v
                  Curriculum Architect Agent
                     (Zero to Hero phases)
                             |
                             v  [structured skill phase plan]
              +--------------+--------------+
              |                             |
              v                             v
   Course & Cert Curator Agent    Resource & Media Mining Agent
   - paid / free courses          - articles, blogs, docs
   - industry certifications      - YouTube videos & playlists
              |                             |
              +--------------+--------------+
                             |
                             v  [enriched resource payload]
                  Document Synthesis Agent
                     Markdown / PDF / HTML
                             |
                             v
                  Final downloadable artifact
```

## 2. Agent specifications

### Agent 1 — Curriculum Architect

**Role.** Senior Instructional Designer and Technical Curriculum Director with
15+ years in computer science, enterprise training, and adult learning pedagogy
(Bloom's Taxonomy).

**Goal.** Deconstruct any input skill into a structured, pedagogical four-stage
Zero-to-Hero learning path with measurable outcomes, prerequisites, and
realistic time estimates.

**Tools.** None — reasoning only.

**Guardrails.**
- MUST NOT skip fundamental concepts even if the user claims prior knowledge,
  unless explicitly instructed.
- MUST include concrete, actionable project ideas for every phase.
- MUST avoid vague topic titles such as "Learn More" or "Advanced Stuff".

**Input schema**

```json
{
  "topic": "Kubernetes & Cloud Native Engineering",
  "user_experience_level": "Beginner",
  "target_time_commitment_hrs_per_week": 10
}
```

**Output schema**

```json
{
  "roadmap_title": "String",
  "target_domain": "String",
  "total_estimated_weeks": "Number",
  "phases": [
    {
      "phase_id": "phase_1",
      "phase_name": "Foundations",
      "estimated_hours": 30,
      "summary": "String",
      "core_concepts": ["Concept 1", "Concept 2"],
      "milestone_project": { "title": "String", "description": "String" }
    }
  ]
}
```

### Agent 2 — Course & Certification Curator

**Role.** EdTech Specialist and Enterprise Credential Consultant. Expert in
course quality, syllabus relevance, pricing models, and certification standards
(AWS, CNCF, PMI, CompTIA, Microsoft, Google).

**Goal.** Search, vet, and select high-quality free and paid courses plus
industry-leading credentials, mapped strictly to each curriculum phase.

**Tools.** `web_search_api`, `course_evaluator_tool`.

**Guardrails.**
- EVERY phase MUST have at least one verified free resource and one
  high-quality paid option.
- Do NOT recommend unverified or spammy course sites. Stick to reputable
  platforms.
- Certifications MUST be official vendor- or body-recognised, NOT generic
  course-completion certificates.
- Where no credential exists, mark "N/A" with a brief justification.

### Agent 3 — Resource & Media Mining

**Role.** Senior Developer Relations Specialist and Content Curator.

**Goal.** Discover, filter, and summarise supplementary learning materials —
articles, blogs, YouTube content, and interactive repositories — per phase.

**Tools.** `youtube_search_api`, `web_search_api`.

**Guardrails.**
- Reject outdated material; prefer content from the last 2–3 years for
  fast-moving technology.
- Video links must point to reliable creators or official channels.
- Limit to 3–5 high-impact resources per category per phase, to prevent
  content overload.
- Annotate each recommendation with a one-sentence "why read/watch this".

### Agent 4 — Document Synthesis & Export

**Role.** Principal Technical Writer and Data Formatting Engineer.

**Goal.** Combine outputs from all previous agents into a formatted, readable,
export-ready Markdown / HTML document.

**Guardrails.**
- MUST NOT drop any course, video, or certification produced by prior agents.
- Markdown MUST be clean and compatible with standard renderers (GitHub
  Flavored Markdown, Pandoc, HTML converters).

## 3. Quality assurance rules

1. **Dead-link guardrail.** Search results must be verified for HTTP 200
   before entering the final payload.
2. **Phase consistency.** Every `phase_id` generated by Agent 1 must be
   accurately mapped across Agents 2 and 3 with no missing references.
3. **No hallucinated certifications.** Credentials must be verifiable against
   official provider catalogues.
4. **Structured JSON validation.** All inter-agent communication must adhere
   strictly to JSON schema contracts, using zod or pydantic.

---

## Implementation notes — where the build departed from this spec

Documented deliberately: each departure was a response to observed behaviour,
not a shortcut.

**The orchestrator is code, not an agent.** The spec shows a supervisor agent
coordinating the others. The implementation uses a plain Python function
([`crew.py`](../src/skill_forge/crew.py)). Coordination here is entirely
deterministic — run one agent, validate, fan out, merge — and none of it
benefits from a model's judgement. Making it an agent would add cost, latency,
and a failure mode for no gain.

**Two stages, not one.** Rather than a single crew, the pipeline splits after
the Architect so the roadmap is validated before any search quota is spent. A
malformed roadmap costs one cheap call instead of a full run.

**Parallelism uses threads, not CrewAI's `async_execution`.** CrewAI rejects any
crew ending in more than one asynchronous task; they must be consumed by a
trailing synchronous one, and the only candidate — the Synthesist — cannot be
constructed until the branch it would consume has finished. Threads also let one
branch fail without collapsing the other.

**Agent 4 was split in two.** The spec's "MUST NOT drop any resource" rule
cannot be reliably enforced by instructing a model to reformat forty items.
Assembly is done by [`render.py`](../src/skill_forge/render.py) with a `for`
loop; the model writes only the executive summary and getting-started guide, and
is never shown the resource list. It cannot drop what it never received.

**Validation runs through guardrails, not `output_pydantic`.** A
`ValidationError` under `output_pydantic` is fatal in CrewAI — only malformed
JSON is retried, never a schema violation. `Task.guardrail` feeds the error back
to the agent and re-runs it, which is what makes the strict contracts useful
rather than merely fatal. See [`guardrails.py`](../src/skill_forge/guardrails.py).

**`weekly_hours` was added to the Roadmap schema.** Not in the original output
schema, but a week count cannot be checked without the pace it assumes — a model
reported 100 hours at 10 hrs/week as "26 weeks" and nothing caught it.

**Media minimums are looser than 3–5.** The cap is enforced at 5; the floor is
1, not 3. An obscure topic may genuinely have only two good videos, and failing
an expensive run over that is worse than a slightly thin section.

**Recency filtering stayed a prompt rule.** Serper returns relative dates
("2 years ago") that cannot be parsed into a reliable filter, so this guardrail
is instruction-level only — the one spec rule with no structural enforcement.
