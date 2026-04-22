"""
LLM Client Abstraction — supports OpenAI and Anthropic (Claude).

Usage:
  from llm_client import get_client, LLM_PROVIDER

  client = get_client()
  # client is an OpenAI() instance — works with both OpenAI and Anthropic
  # (Anthropic via their OpenAI-compatible endpoint)

Provider selection (in priority order):
  1. LLM_PROVIDER env var:  "openai" or "anthropic"
  2. Auto-detect:           whichever API key is set in .env

Environment variables:
  OPENAI_API_KEY      — required for OpenAI provider
  ANTHROPIC_API_KEY   — required for Anthropic provider
  LLM_PROVIDER        — optional override: "openai" | "anthropic"
  LLM_MODEL           — optional model override (default: gpt-4o / claude-sonnet-4-20250514)
"""

import os
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))

# ── Defaults per provider ────────────────────────────────────────────────────
_DEFAULTS = {
    "openai":    {"model": "gpt-4o",                "base_url": None},
    "anthropic": {"model": "claude-sonnet-4-20250514", "base_url": "https://api.anthropic.com/v1/"},
}


def _detect_provider() -> str:
    """Pick provider based on LLM_PROVIDER env var, or whichever key exists."""
    explicit = os.getenv("LLM_PROVIDER", "").lower().strip()
    if explicit in _DEFAULTS:
        return explicit

    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"

    raise EnvironmentError(
        "No LLM API key found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in your .env file."
    )


def get_provider() -> str:
    """Return the active provider name: 'openai' or 'anthropic'."""
    return _detect_provider()


def get_model() -> str:
    """Return the model string for the active provider (or LLM_MODEL override)."""
    override = os.getenv("LLM_MODEL", "").strip()
    if override:
        return override
    return _DEFAULTS[_detect_provider()]["model"]


def get_client():
    """
    Return an OpenAI-compatible client for the active provider.

    For OpenAI:    standard OpenAI() client
    For Anthropic: OpenAI() pointed at Anthropic's OpenAI-compatible endpoint
    """
    from openai import OpenAI

    provider = _detect_provider()

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set in .env")
        return OpenAI(
            api_key=api_key,
            base_url=_DEFAULTS["anthropic"]["base_url"],
        )
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY not set in .env")
        return OpenAI(api_key=api_key)


# ── Convenience: module-level provider info ──────────────────────────────────
LLM_PROVIDER = _detect_provider() if (os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")) else None
LLM_MODEL = get_model() if LLM_PROVIDER else None


# ── Secrets masking utility ──────────────────────────────────────────────────
import re


def mask_secrets(text: str) -> str:
    """
    Mask API keys and other sensitive patterns in text.

    Replaces patterns like:
      - sk-... (OpenAI keys)
      - anthropic-... (Anthropic keys)
      - 32+ character alphanumeric strings (potential API keys)

    Args:
        text: The text to sanitize

    Returns:
        Text with secrets replaced by ***REDACTED***
    """
    if not isinstance(text, str):
        return text
    pattern = r'(sk-[a-zA-Z0-9]{20,}|anthropic-[a-zA-Z0-9]{20,}|[a-zA-Z0-9]{32,})'
    return re.sub(pattern, '***REDACTED***', text)
