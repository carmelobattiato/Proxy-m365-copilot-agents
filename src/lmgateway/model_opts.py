"""
Model name option parser.

The model name encodes options as colon-separated tokens, e.g.:
  "lm-gateway:sonnet5:persist"
  "lm-gateway:auto:fresh"

Supported tokens
----------------
persist  — reuse the same upstream conversation across turns (multi-turn memory)
fresh    — force a brand-new conversation UUID even when incognito mode is active
auto     — select the upstream "automatic" model picker
sonnet5.5 — Claude Sonnet 5.5 (default)
sonnet5   — Claude Sonnet 5
"""

from __future__ import annotations

from dataclasses import dataclass

# Map of model suffix alias → upstream tone value
TONE_ALIASES: dict[str, str] = {
    "auto": "Magic",
    "sonnet5.5": "Claude_Sonnet",
    "sonnet5": "Claude_Sonnet_5",
}

_PERSIST = "persist"
_FRESH = "fresh"


@dataclass(frozen=True)
class ModelOptions:
    persist: bool = False   # enable multi-turn persistent session
    fresh: bool = False     # force new conversation, bypass incognito
    tone: str | None = None # upstream model selector override


def parse_model_options(model: str) -> ModelOptions:
    """Parse options encoded in the model name string."""
    persist = False
    fresh = False
    tone: str | None = None

    for token in model.split(":")[1:]:
        key = token.strip().lower()
        if key == _PERSIST:
            persist = True
        elif key == _FRESH:
            fresh = True
        elif key in TONE_ALIASES:
            tone = TONE_ALIASES[key]

    return ModelOptions(persist=persist, fresh=fresh, tone=tone)


def model_names(base: str) -> list[str]:
    """Return all model names to advertise in GET /v1/models."""
    return [base] + [f"{base}:{alias}" for alias in TONE_ALIASES]