"""Provider-agnostic LLM construction.

Skill Forge is deliberately not tied to one model vendor. The default target is
Groq's free tier so that anyone cloning this repo can run the full pipeline
without a paid API key; switching to Anthropic, OpenAI, Gemini, NVIDIA NIM, or
Cerebras is a one-variable change in `.env`.

Why Groq is the default, and why not the alternatives — all measured on this
project rather than taken from documentation:

- **NVIDIA NIM** served the tool-free Curriculum Architect in 5.4s, but the
  Course Curator and Media Miner — whose prompts are roughly four times larger
  and carry tool schemas — never returned at all. Ten minutes, zero tool calls,
  process idle on network I/O. The free endpoint queues rather than throttles,
  so there is nothing to tune. A two-token reply was separately measured at
  85-204 seconds.
- **Gemini Flash** authenticated fine and listed 56 models, but every
  generateContent call returned 429 with ``limit: 0`` — the free tier was not
  provisioned for that key's project.

Providers reached over the OpenAI wire protocol (Groq, NIM, Cerebras) ride
CrewAI's *native* OpenAI client rather than the LiteLLM fallback. That matters
here: CrewAI only ships native providers for openai/anthropic/azure/gemini/
bedrock, and would otherwise require LiteLLM as an extra dependency.

One subtlety worth knowing if you extend this: CrewAI validates `model` against
a hardcoded list of known model names when it infers the provider from a
``provider/model`` prefix. ``meta/llama-3.3-70b-instruct`` is not on that list,
so inference would fail over to LiteLLM. Passing ``provider=`` explicitly skips
that validation and keeps the full model string intact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from crewai import LLM
from dotenv import load_dotenv

load_dotenv()

#: Providers that speak the OpenAI wire protocol at a non-OpenAI address.
#: They all ride CrewAI's native OpenAI client; only the base URL differs.
OPENAI_COMPATIBLE_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "nvidia_nim": "https://integrate.api.nvidia.com/v1",
    "cerebras": "https://api.cerebras.ai/v1",
}

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
    "nvidia_nim": "meta/llama-3.1-8b-instruct",
    "cerebras": "llama-3.3-70b",
    "anthropic": "claude-sonnet-4-5",
    # mini rather than full gpt-4.1: roughly a tenth the cost, and strong enough
    # at nested JSON and tool calling that it triggers fewer guardrail retries —
    # which makes it cheaper again. Swap to "gpt-4.1" via MODEL for a showcase run.
    "openai": "gpt-4.1-mini",
}

API_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "nvidia_nim": "NVIDIA_NIM_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

SIGNUP_URL = {
    "groq": "https://console.groq.com/keys (free tier)",
    "gemini": "https://aistudio.google.com/apikey (free tier)",
    "nvidia_nim": "https://build.nvidia.com (free tier)",
    "cerebras": "https://cloud.cerebras.ai (free tier)",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
}

#: Placeholder fragments from .env.example. Treated as "no key set" so users get
#: setup guidance instead of a confusing 401 from the provider.
PLACEHOLDER_MARKERS = ("xxx", "your-", "<your")


class LLMConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable LLM."""


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    temperature: float

    def describe(self) -> str:
        """Human-readable summary. Never includes the key itself."""
        where = f" via {self.base_url}" if self.base_url else ""
        return f"{self.provider}:{self.model}{where} (temp={self.temperature})"


def load_settings(
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> LLMSettings:
    """Resolve LLM configuration from arguments, then environment, then defaults."""
    provider = (provider or os.getenv("LLM_PROVIDER") or "groq").strip().lower()

    if provider not in DEFAULT_MODELS:
        raise LLMConfigError(
            f"Unknown LLM_PROVIDER {provider!r}. "
            f"Supported: {', '.join(sorted(DEFAULT_MODELS))}."
        )

    # MODEL in .env belongs to the provider named alongside it. Applying it to a
    # different provider sends e.g. "llama-3.3-70b-versatile" to Anthropic, which
    # fails in a way that looks like a credentials problem rather than a config
    # one. So the env override is honoured only for the env's own provider.
    env_provider = (os.getenv("LLM_PROVIDER") or "groq").strip().lower()
    env_model = os.getenv("MODEL", "").strip() if provider == env_provider else ""
    model = model or env_model or DEFAULT_MODELS[provider]

    key_var = API_KEY_ENV[provider]
    api_key = os.getenv(key_var, "").strip()
    if not api_key or any(m in api_key.lower() for m in PLACEHOLDER_MARKERS):
        raise LLMConfigError(
            f"{key_var} is not set (or still holds the placeholder from "
            f".env.example).\n"
            f"  1. cp .env.example .env\n"
            f"  2. Add your key from {SIGNUP_URL[provider]}\n"
            f"  Alternatively set LLM_PROVIDER to one of: "
            f"{', '.join(sorted(DEFAULT_MODELS))}."
        )

    if temperature is None:
        temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))

    return LLMSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        # OpenAI-compatible providers need an explicit endpoint; native
        # providers (anthropic, gemini, openai) resolve their own.
        base_url=OPENAI_COMPATIBLE_BASE_URLS.get(provider),
        temperature=temperature,
    )


def build_llm(
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> LLM:
    """Construct the CrewAI LLM every agent shares.

    Args:
        provider: Override ``LLM_PROVIDER``. One of nvidia_nim, anthropic, openai.
        model: Override ``MODEL``. Defaults to the provider's flagship.
        temperature: Override ``LLM_TEMPERATURE``. Low by default — this
            pipeline wants schema conformance, not creativity.

    Raises:
        LLMConfigError: if the provider is unknown or its API key is absent.
    """
    settings = load_settings(provider=provider, model=model, temperature=temperature)

    kwargs: dict[str, object] = {
        "model": settings.model,
        "api_key": settings.api_key,
        "temperature": settings.temperature,
    }

    if settings.base_url:
        # Explicit provider bypasses CrewAI's model-name allowlist; see module docstring.
        kwargs["provider"] = "openai"
        kwargs["base_url"] = settings.base_url
    else:
        kwargs["provider"] = settings.provider

    return LLM(**kwargs)  # type: ignore[arg-type]
