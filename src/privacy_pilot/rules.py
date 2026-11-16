"""High-recall candidates; patterns are deliberately NOT final labels."""
from __future__ import annotations

import re
import unicodedata

RULE_VERSION = "1.0.0"
PATTERNS = {
    "first_person": r"\b(?:i|my|mine|we|our|ours)\b",
    "age": r"\b\d{1,3}\s*(?:years?\s*old|[mf])\b|\b(?:teenager|twenties|thirties|forties)\b",
    "family_relationship": r"\b(?:daughter|son|mother|father|children|wife|husband|married|divorced|boyfriend|girlfriend)\b",
    "work_education": r"\b(?:unemployed|retired|laid off|degree|graduated|studying|student|salary)\b",
    "health": r"\b(?:diagnosed|medication|pregnant|asthma|depression|therapy|symptoms)\b",
    "finance": r"\b(?:income|in debt|paycheck|savings|bankrupt)\b|[$€£]\s*\d",
    "location": r"\b(?:live in|living in|moved to|born in|hometown|grew up in|visited)\b",
    "appearance": r"\b\d{2,3}\s*(?:cm|kg|lbs|pounds)\b|\b(?:hair|tall|height)\b",
}
COMPILED = {key: re.compile(pattern, re.I) for key, pattern in PATTERNS.items()}


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def candidate_rules(text: str) -> list[str]:
    return [key for key, pattern in COMPILED.items() if pattern.search(text)]


def invalid_text_reason(text) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return "empty_or_nontext"
    if text.strip().lower() in ("[deleted]", "[removed]", "null"):
        return "deleted_or_removed"
    if re.fullmatch(r"\s*https?://\S+\s*", text):
        return "url_only"
    # No claimed language detector: the official source is English; mixed text is retained.
    return None


def chunks(text: str, size: int, overlap: int) -> list[tuple[int, str]]:
    if size < 1 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap")
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        # Prefer a paragraph or sentence boundary without creating tiny chunks.
        if end < len(text):
            boundary = max(text.rfind("\n", start + size // 2, end),
                           text.rfind(". ", start + size // 2, end))
            if boundary >= start + max(size // 2, overlap):
                end = boundary + 1
        result.append((start, text[start:end]))
        if end == len(text):
            break
        start = end - overlap
    return result
