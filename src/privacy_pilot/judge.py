"""Blind model reference labeling, with separate provenance and replayable responses."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import re
import time
import tomllib
from urllib.parse import urlparse

from .common import ROOT, PilotError, canonical, digest, file_hash, jsonl, new_directory, read_json, write_json
from .config import DEFAULTS
from .evaluation import make_audit
from .model import AuthenticationError, BudgetExceeded, ModelClient, ModelError
from .pipeline import annotate
from .rules import chunks
from .schema import AnnotationError, merge_findings


JUDGE_DEFAULTS = {
    "endpoint": "https://api.sjtuagent.com/v1/responses", "model": "gpt-6.1-sol",
    "review_model": "gpt-6.1-sol", "api_key_env": "JUDGE_API_KEY",
    "actor_authorization": "local-image-extension", "reasoning_effort": "high",
    "max_output_tokens": 8192, "timeout": 180, "retries": 2, "max_calls": 1500,
    "repair_attempts": 1, "chunk_chars": 8000, "overlap_chars": 600, "max_chunks": 20,
}


def load_judge_config(path: Path) -> dict:
    if path.suffix == ".json":
        supplied = read_json(path)
    else:
        with path.open("rb") as f:
            supplied = tomllib.load(f)
    if not isinstance(supplied, dict) or set(supplied) != {"judge"} or not isinstance(supplied["judge"], dict):
        raise PilotError("Judge configuration must contain only a [judge] section")
    if set(supplied["judge"]) - set(JUDGE_DEFAULTS):
        raise PilotError("Unknown judge setting; keep API keys in .env")
    cfg = {**JUDGE_DEFAULTS, **supplied["judge"]}
    for key, default in JUDGE_DEFAULTS.items():
        if type(cfg[key]) is not type(default):
            raise PilotError(f"Invalid type for judge.{key}")
    url = urlparse(cfg["endpoint"])
    if url.username or url.password or url.query or url.fragment or not url.path.endswith("/responses"):
        raise PilotError("Judge endpoint must end in /responses with no credentials or query")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
        raise PilotError("Judge endpoint requires HTTPS except on loopback")
    if not cfg["model"].strip() or not cfg["review_model"].strip():
        raise PilotError("Judge and review model names are required")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg["api_key_env"]):
        raise PilotError("Judge api_key_env must be an environment variable name")
    if any(ord(c) < 32 or ord(c) > 126 for c in cfg["actor_authorization"]):
        raise PilotError("Invalid actor authorization header")
    if cfg["reasoning_effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise PilotError("Invalid judge reasoning effort")
    if not 0 < cfg["timeout"] <= 300 or not 0 <= cfg["retries"] <= 5 or cfg["max_calls"] < 1 or cfg["max_output_tokens"] < 64:
        raise PilotError("Invalid judge timeout, retry or budget settings")
    if not 0 <= cfg["repair_attempts"] <= 3 or not 0 <= cfg["overlap_chars"] < cfg["chunk_chars"] or cfg["chunk_chars"] < 100 or cfg["max_chunks"] < 1:
        raise PilotError("Invalid judge chunk or repair settings")
    return cfg


def judge(input_file: Path, output: Path, cfg: dict, cache: Path, mode: str,
          count: int, seed: int, scope: str, prompts_dir: Path | None = None,
          transport=None, progress=None) -> dict:
    prompts_dir = prompts_dir or ROOT / "prompts"
    prompt_files = {name: (prompts_dir / name).read_text(encoding="utf-8")
                    for name in ("extract.txt", "judge.txt", "judge_review.txt")}
    system = prompt_files["extract.txt"] + "\n\n" + prompt_files["judge.txt"]
    review_system = system + "\n\n" + prompt_files["judge_review.txt"]
    model_cfg = {**deepcopy(DEFAULTS["model"]), "provider": "responses", "endpoint": cfg["endpoint"],
                 "name": cfg["model"], "api_key_env": cfg["api_key_env"], "max_tokens": cfg["max_output_tokens"],
                 **{k: cfg[k] for k in ("timeout", "retries", "max_calls", "reasoning_effort", "actor_authorization")}}
    primary = ModelClient(model_cfg, cache, mode, transport=transport)
    reviewer = ModelClient({**model_cfg, "name": cfg["review_model"]}, cache, mode, transport=transport)
    reviewer.stats = primary.stats  # One shared per-execution request budget, including repairs/retries.
    new_directory(output)
    make_audit(input_file, output / "sample.jsonl", count, seed, scope)
    records = list(jsonl(output / "sample.jsonl"))
    if not records:
        raise PilotError("Judge sample is empty")
    for row in records:
        if digest(row["text"]) != row["text_sha256"]:
            raise PilotError("Judge input text hash mismatch")
    write_json(output / "config.snapshot.json", {"judge": cfg})
    (output / "prompts").mkdir()
    for name, prompt in prompt_files.items():
        (output / "prompts" / name).write_text(prompt, encoding="utf-8")
    provenance = {"method": "llm_as_a_judge", "provider": "responses", "endpoint": cfg["endpoint"],
                  "model": cfg["model"], "review_model": cfg["review_model"], "blind": True,
                  "store": False, "web_search": False,
                  "config_sha256": digest(canonical(cfg)), "prompts_sha256": digest(canonical(prompt_files))}
    counts, stopped = Counter(), None
    started = time.monotonic()
    with (output / "judgments.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for index, source in enumerate(records):
            row = {**source, "reference_kind": "llm_judge", "judge": provenance,
                   "judge_status": "error", "judge_evidence": [], "trace": []}
            begin = time.monotonic()
            if stopped:
                row["judge_status"] = "unprocessed_" + stopped
            else:
                try:
                    segments = chunks(row["text"], cfg["chunk_chars"], cfg["overlap_chars"])
                    if len(segments) > cfg["max_chunks"]:
                        raise AnnotationError("Judge text exceeds max_chunks; no partial reference accepted")
                    annotations = []
                    for offset, text in segments:
                        first = annotate(primary, text, offset, system, "judge", cfg["repair_attempts"], row["trace"])
                        annotations.append(annotate(reviewer, text, offset, review_system, "judge_review",
                                                    cfg["repair_attempts"], row["trace"], first))
                    row["judge_evidence"] = merge_findings(annotations)
                    if any(a["review_required"] for a in annotations):
                        row["judge_status"] = "needs_review"
                    else:
                        row["judge_status"] = "ok"
                        row["gold_labels"] = sorted({finding["tag"] for finding in row["judge_evidence"]})
                except (ModelError, AnnotationError) as e:
                    row.update(error=str(e), error_type=type(e).__name__)
                    if isinstance(e, (AuthenticationError, BudgetExceeded)):
                        stopped = "authentication" if isinstance(e, AuthenticationError) else "budget"
            row["judge_seconds"] = round(time.monotonic() - begin, 6)
            counts[row["judge_status"]] += 1
            f.write(canonical(row) + "\n")
            f.flush()
            if progress:
                progress(f"Judged {index + 1}/{len(records)}, ok {counts['ok']}, unresolved {index + 1 - counts['ok']}")
    summary = {"reference_kind": "llm_judge", "judge": provenance, "scope": scope,
               "sample_size": len(records), "counts": dict(counts), "complete": counts["ok"] == len(records),
               "stopped_reason": stopped, "mode": mode, "model_stats": dict(primary.stats),
               "input_sha256": file_hash(input_file), "judgments_sha256": file_hash(output / "judgments.jsonl"),
               "duration_seconds": round(time.monotonic() - started, 3)}
    write_json(output / "cache_index.json", sorted(primary.cache_keys | reviewer.cache_keys))
    write_json(output / "summary.json", summary)
    return summary
