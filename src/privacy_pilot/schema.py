from __future__ import annotations

import json
import re

from .common import PilotError, TAGS


class AnnotationError(PilotError):
    pass


def parse_annotation(content: str, text: str, offset: int = 0) -> dict:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except (ValueError, TypeError) as e:
        raise AnnotationError("Response must be one valid JSON object") from e
    if not isinstance(value, dict) or set(value) != {"has_privacy", "review_required", "findings"}:
        raise AnnotationError("Expected exactly has_privacy, review_required and findings")
    if type(value["has_privacy"]) is not bool or type(value["review_required"]) is not bool:
        raise AnnotationError("Privacy/review flags must be booleans")
    findings = value["findings"]
    if not isinstance(findings, list) or len(findings) > 100:
        raise AnnotationError("findings must be a list with at most 100 entries")
    if value["has_privacy"] != bool(findings):
        raise AnnotationError("has_privacy must equal bool(findings)")
    result = []
    fields = {"tag", "evidence", "subject", "time", "assertion", "reason"}
    for item in findings:
        if not isinstance(item, dict) or set(item) != fields or not all(isinstance(v, str) for v in item.values()):
            raise AnnotationError("Each finding must have exactly the six required string fields")
        if item["tag"] not in TAGS:
            raise AnnotationError("Unknown privacy category")
        if item["subject"] not in ("self", "family_relation"):
            raise AnnotationError("Subject must be self or family_relation")
        if item["subject"] == "family_relation" and item["tag"] != "FAM":
            raise AnnotationError("A relative's attributes cannot be assigned to the author")
        if item["time"] not in ("present", "past", "unspecified") or item["assertion"] not in ("affirmative", "negative"):
            raise AnnotationError("Invalid time or assertion")
        evidence = item["evidence"]
        if not evidence.strip() or not item["reason"].strip():
            raise AnnotationError("Evidence and reason must not be empty")
        start = text.find(evidence)
        if start < 0:
            raise AnnotationError("Evidence is not an exact substring; copy original text verbatim")
        if text.find(evidence, start + 1) >= 0:
            raise AnnotationError("Evidence occurs multiple times; include more context to make it unique")
        result.append({**item, "start": start + offset, "end": start + offset + len(evidence)})
    return {**value, "findings": result}


def without_offsets(annotation: dict) -> dict:
    return {**annotation, "findings": [{k: v for k, v in f.items() if k not in ("start", "end")}
                                      for f in annotation["findings"]]}


def merge_findings(annotations: list[dict]) -> list[dict]:
    seen, result = set(), []
    for annotation in annotations:
        for finding in annotation["findings"]:
            key = tuple(finding[k] for k in ("tag", "start", "end", "subject", "time", "assertion"))
            if key not in seen:
                seen.add(key)
                result.append(finding)
    return sorted(result, key=lambda f: (f["start"], f["end"], f["tag"]))
