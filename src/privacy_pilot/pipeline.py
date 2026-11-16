from __future__ import annotations

from collections import Counter
import csv
from pathlib import Path
import platform
import sys
import time

from . import __version__
from .common import ROOT, TAGS, PilotError, canonical, digest, file_hash, new_directory, write_json
from .model import AuthenticationError, BudgetExceeded, ModelClient, ModelError
from .rules import RULE_VERSION, candidate_rules, chunks
from .schema import AnnotationError, merge_findings, parse_annotation, without_offsets
from .source import load_prepared


def annotate(client: ModelClient, text: str, offset: int, system: str,
             stage: str, attempts: int, trace: list, candidate: dict | None = None) -> dict:
    data = {"text": text}
    if candidate is not None:
        data["candidate_annotation"] = without_offsets(candidate)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": canonical(data)}]
    for attempt in range(attempts + 1):
        content, cache_key = client.complete(messages, stage)
        entry = {"stage": stage, "chunk_offset": offset, "attempt": attempt, "cache_key": cache_key}
        trace.append(entry)
        try:
            annotation = parse_annotation(content, text, offset)
            entry["validation"] = "passed"
            return annotation
        except AnnotationError as e:
            entry.update(validation="failed", error=str(e))
            if attempt == attempts:
                raise
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": "Repair the annotation. Validation error: " + str(e) +
                          ". Return the complete JSON object; use only the ORIGINAL text."}]
    raise AssertionError("unreachable")


def run(prepared: Path, output: Path, cfg: dict, cache: Path, mode: str,
        client: ModelClient | None = None, prompts_dir: Path | None = None,
        progress=None) -> dict:
    records, manifest = load_prepared(prepared)
    if not records:
        raise PilotError("Prepared input contains no valid records")
    client = client or ModelClient(cfg["model"], cache, mode)
    prompts_dir = prompts_dir or ROOT / "prompts"
    extract_prompt = (prompts_dir / "extract.txt").read_text(encoding="utf-8")
    review_prompt = extract_prompt + "\n\n" + (prompts_dir / "review.txt").read_text(encoding="utf-8")
    p = cfg["pipeline"]
    fingerprint = digest(canonical({"version": __version__, "config": cfg,
                                   "input": manifest["input_sha256"], "extract": extract_prompt,
                                   "review": review_prompt, "rules_version": RULE_VERSION}))
    new_directory(output)
    write_json(output / "config.snapshot.json", cfg)
    write_json(output / "source_manifest.json", manifest)
    (output / "prompts").mkdir()
    for name in ("extract.txt", "review.txt"):
        (output / "prompts" / name).write_text((prompts_dir / name).read_text(encoding="utf-8"), encoding="utf-8")
    counts, categories, all_keys = Counter(), Counter(), set()
    started, stopped = time.monotonic(), None
    with (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as predictions, \
         (output / "privacy_dataset.jsonl").open("w", encoding="utf-8", newline="\n") as final, \
         (output / "privacy_dataset.csv").open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=("record_id", "text", "link", "privacy_tag"))
        writer.writeheader()
        for index, record in enumerate(records):
            trace, preliminary, annotations = [], [], []
            pred = {**record, "privacy_tag": [], "evidence": [], "trace": trace,
                    "run_fingerprint": fingerprint, "rule_hits": candidate_rules(record["text"])}
            if stopped:
                pred["status"] = "unprocessed_" + stopped
            elif p["rules"] and not pred["rule_hits"]:
                pred["status"] = "rule_negative"
            else:
                segments = chunks(record["text"], p["chunk_chars"], p["overlap_chars"])
                if len(segments) > p["max_chunks"]:
                    pred["status"] = "skipped_too_long"
                else:
                    try:
                        for offset, text in segments:
                            first = annotate(client, text, offset, extract_prompt, "extract", p["repair_attempts"], trace)
                            preliminary.append(first)
                            reviewed = first
                            if p["review"] and (first["has_privacy"] or first["review_required"]):
                                reviewed = annotate(client, text, offset, review_prompt, "review",
                                                    p["repair_attempts"], trace, first)
                            annotations.append(reviewed)
                        pred["pre_review_evidence"] = merge_findings(preliminary)
                        if any(a["review_required"] for a in annotations):
                            pred["status"] = "needs_review"
                            pred["proposed_evidence"] = merge_findings(annotations)
                        else:
                            evidence = merge_findings(annotations)
                            pred.update(evidence=evidence, privacy_tag=sorted({f["tag"] for f in evidence}))
                            pred["status"] = "accepted" if evidence else "model_negative"
                    except (AuthenticationError, BudgetExceeded) as e:
                        pred.update(status="error", error=str(e), error_type=type(e).__name__)
                        stopped = "authentication" if isinstance(e, AuthenticationError) else "budget"
                    except (ModelError, AnnotationError) as e:
                        pred.update(status="error", error=str(e), error_type=type(e).__name__)
                    # A failed chunk excludes the entire source record; never publish partial analysis.
            counts[pred["status"]] += 1
            all_keys.update(t["cache_key"] for t in trace)
            predictions.write(canonical(pred) + "\n")
            predictions.flush()
            if pred["status"] == "accepted":
                final.write(canonical(pred) + "\n")
                final.flush()
                categories.update(pred["privacy_tag"])
                writer.writerow({"record_id": record["record_id"], "text": record["text"],
                                 "link": record["link"], "privacy_tag": ";".join(pred["privacy_tag"])})
                if p["target_count"] and counts["accepted"] >= p["target_count"] and \
                   sum(n >= p["min_per_class"] for n in categories.values()) >= p["min_classes"]:
                    stopped = "target"
            if progress and ((index + 1) % 25 == 0 or index + 1 == len(records)):
                progress(f"Processed {index + 1}/{len(records)}, accepted {counts['accepted']}, errors {counts['error']}")
    synthetic = manifest["source"]["synthetic"]
    summary = {"schema_version": 1, "version": __version__, "run_fingerprint": fingerprint,
               "input_count": len(records), "counts": dict(counts),
               "category_counts": {tag: categories[tag] for tag in TAGS},
               "category_coverage": len(categories), "synthetic": synthetic,
               "count_thresholds_met": counts["accepted"] >= 50 and len(categories) >= 3 and not synthetic,
               "grade_claim": "Counts only; semantic accuracy and real-source authenticity require audit.",
               "complete_analysis": not any(k.startswith("unprocessed_") or k in ("error", "skipped_too_long", "needs_review")
                                            for k, v in counts.items() if v),
               "stopped_reason": stopped, "model_stats": dict(client.stats),
               "duration_seconds": round(time.monotonic() - started, 3), "mode": mode,
               "python": sys.version.split()[0], "platform": platform.platform(),
               "output_sha256": file_hash(output / "privacy_dataset.jsonl")}
    write_json(output / "summary.json", summary)
    write_json(output / "cache_index.json", sorted(all_keys | client.cache_keys))
    return summary
