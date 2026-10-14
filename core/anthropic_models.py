"""Gemeinsame Schranke für alle direkten Anthropic-Modellaufrufe."""

import config


def ensure_anthropic_model_allowed(model: str) -> None:
    """Bricht ab, bevor ein nicht freigegebenes Modell den Anthropic-Client erreicht."""
    allowed = {
        entry.strip()
        for entry in config.ANTHROPIC_ALLOWED_MODELS
        if isinstance(entry, str) and entry.strip()
    }
    if model not in allowed:
        raise ValueError(
            f"Anthropic-Modell {model!r} ist nicht freigegeben; "
            "prüfe ANTHROPIC_ALLOWED_MODELS."
        )
