from __future__ import annotations

from collections import Counter
from pathlib import Path
import random

from .common import TAGS, PilotError, canonical, digest, file_hash, jsonl, new_directory, write_json, write_jsonl
from .rules import normalized
from .source import load_prepared, verify_sources


def split_prepared(prepared: Path, output: Path, dev: int, test: int, seed: int) -> dict:
    records, manifest = load_prepared(prepared)
    if dev < 0 or test < 1 or dev + test >= len(records):
        raise PilotError("Split requires dev>=0, test>=1 and at least one remaining collection record")
    ordered = sorted(records, key=lambda r: r["record_id"])
    random.Random(seed).shuffle(ordered)
    groups = {"dev": ordered[:dev], "test": ordered[dev:dev + test], "collection": ordered[dev + test:]}
    new_directory(output)
    for name, rows in groups.items():
        folder = output / name
        folder.mkdir()
        write_jsonl(folder / "input.jsonl", sorted(rows, key=lambda r: r["record_id"]))
        updated = {**manifest, "counts": {"accepted": len(rows)},
                   "input_sha256": file_hash(folder / "input.jsonl"),
                   "parent_input_sha256": manifest["input_sha256"],
                   "split": {"name": name, "seed": seed, "dev": dev, "test": test},
                   "selection": "deterministic shuffle of sorted record IDs after cleaning, before rules"}
        write_json(folder / "source_manifest.json", updated)
    result = {k: len(v) for k, v in groups.items()}
    write_json(output / "split.json", {"seed": seed, "counts": result})
    return result


def make_audit(input_file: Path, output: Path, count: int, seed: int, scope: str) -> dict:
    if output.exists():
        raise PilotError("Audit output exists; annotations will not be overwritten")
    if count < 1 or scope not in ("final", "holdout", "rule_negative"):
        raise PilotError("Invalid audit size or scope")
    records = list(jsonl(input_file))
    if scope == "final" and any(r.get("status") != "accepted" for r in records):
        raise PilotError("final audit expects privacy_dataset.jsonl with accepted records only")
    if scope == "rule_negative":
        records = [r for r in records if r.get("status") == "rule_negative"]
    if scope == "holdout" and any("status" in r for r in records):
        raise PilotError("holdout audit must sample prepared input, before model/rule predictions")
    if len({r["record_id"] for r in records}) != len(records):
        raise PilotError("Audit input has duplicate IDs")
    records.sort(key=lambda r: r["record_id"])
    sample = random.Random(seed).sample(records, min(count, len(records)))
    rows = [{"record_id": r["record_id"], "text": r["text"], "text_sha256": r["text_sha256"],
             "source_ref": r["source_ref"], "scope": scope, "gold_labels": None,
             "source_verified": None, "annotation_seconds": None, "notes": ""} for r in sample]
    write_jsonl(output, rows)
    result = {"scope": scope, "seed": seed, "requested": count, "sampled": len(rows),
              "input_sha256": file_hash(input_file), "blind": True}
    write_json(output.with_suffix(".manifest.json"), result)
    return result


def ratio(a: int, b: int):
    return a / b if b else None


def metrics(tp: int, fp: int, fn: int) -> dict:
    return {"tp": tp, "fp": fp, "fn": fn, "precision": ratio(tp, tp + fp),
            "recall": ratio(tp, tp + fn), "f1": ratio(2 * tp, 2 * tp + fp + fn)}


def evaluate(predictions_file: Path, gold_file: Path) -> dict:
    predictions = {}
    for row in jsonl(predictions_file):
        if row["record_id"] in predictions:
            raise PilotError("Duplicate prediction ID")
        predictions[row["record_id"]] = row
    gold = list(jsonl(gold_file))
    if not gold or len({r["record_id"] for r in gold}) != len(gold):
        raise PilotError("Gold annotations must be nonempty and have unique IDs")
    scopes = {r["scope"] for r in gold}
    if len(scopes) != 1:
        raise PilotError("Do not mix final-set audits and independent holdout evaluation")
    scope = scopes.pop()
    if scope not in ("final", "holdout", "rule_negative"):
        raise PilotError("Unknown annotation scope")
    per_class = {tag: Counter() for tag in TAGS}
    exact = positive = verified = reviewed_sources = abstained = 0
    total_seconds = 0.0
    for row in gold:
        labels = row.get("gold_labels")
        if not isinstance(labels, list) or any(t not in TAGS for t in labels) or len(set(labels)) != len(labels):
            raise PilotError(f"Finish gold_labels using a unique label list ([] for negative): {row['record_id']}")
        elapsed = row.get("annotation_seconds")
        if type(elapsed) not in (int, float) or elapsed < 0:
            raise PilotError("Fill annotation_seconds with the actual nonnegative time")
        total_seconds += elapsed
        pred = predictions.get(row["record_id"])
        if pred is None:
            raise PilotError(f"Missing prediction: {row['record_id']}; missing results are not negatives")
        if pred.get("status") not in ("accepted", "model_negative", "rule_negative", "needs_review"):
            raise PilotError(f"Unfinished/error prediction: {row['record_id']}")
        if digest(row["text"]) != row["text_sha256"] or row["text_sha256"] != pred["text_sha256"]:
            raise PilotError("Gold/prediction text mismatch")
        if row.get("source_ref") != pred.get("source_ref"):
            raise PilotError("Gold/prediction source reference mismatch")
        predicted = pred.get("privacy_tag")
        if not isinstance(predicted, list) or any(t not in TAGS for t in predicted) or len(predicted) != len(set(predicted)):
            raise PilotError("Invalid predicted label list")
        target, actual = set(labels), set(predicted)
        exact += target == actual
        positive += bool(target)
        abstained += pred["status"] == "needs_review"
        if row.get("source_verified") is not None:
            if type(row["source_verified"]) is not bool:
                raise PilotError("source_verified must be true/false/null")
            reviewed_sources += 1
            verified += row["source_verified"]
        for tag in TAGS:
            if tag in target and tag in actual:
                per_class[tag]["tp"] += 1
            elif tag in actual:
                per_class[tag]["fp"] += 1
            elif tag in target:
                per_class[tag]["fn"] += 1
    sums = Counter()
    for value in per_class.values():
        sums.update(value)
    all_metrics = metrics(sums["tp"], sums["fp"], sums["fn"])
    result = {"scope": scope, "sample_size": len(gold), "gold_positive_count": positive,
              "positive_fraction": positive / len(gold), "exact_match": exact / len(gold),
              "abstained_count": abstained, "annotation_seconds": total_seconds,
              "source_verified": verified, "source_reviewed": reviewed_sources,
              "source_verification_rate": ratio(verified, reviewed_sources),
              "label_precision": all_metrics["precision"],
              "predictions_sha256": file_hash(predictions_file), "gold_sha256": file_hash(gold_file)}
    if scope == "holdout":
        result["micro"] = all_metrics
        result["per_class"] = {tag: metrics(c["tp"], c["fp"], c["fn"]) for tag, c in per_class.items()}
        result["note"] = "Abstentions count as no predicted labels; holdout must not have been used for tuning."
    elif scope == "final":
        result["text_validity_rate"] = positive / len(gold)
        result["note"] = "Final-positive audit only; no population recall or F1 is claimed."
    else:
        result["note"] = "Positive fraction estimates missed disclosures within rule-negative stratum, not population recall."
    return result


def verify_result(run_dir: Path, raw_root: Path | None = None) -> dict:
    rows = list(jsonl(run_dir / "privacy_dataset.jsonl"))
    errors, ids, hashes, categories = [], set(), set(), Counter()
    for row in rows:
        record_id = row.get("record_id")
        try:
            text = row["text"]
            h = digest(normalized(text))
            if record_id in ids or h in hashes:
                raise ValueError("duplicate_record_or_text")
            ids.add(record_id)
            hashes.add(h)
            if digest(text) != row["text_sha256"] or digest(canonical(row["source_ref"])) != record_id:
                raise ValueError("text_or_source_hash_mismatch")
            tags, evidence = row["privacy_tag"], row["evidence"]
            if row["status"] != "accepted" or not tags or len(set(tags)) != len(tags) or any(t not in TAGS for t in tags):
                raise ValueError("invalid_labels_or_status")
            if set(tags) != {f["tag"] for f in evidence}:
                raise ValueError("tag_evidence_mismatch")
            for f in evidence:
                if type(f["start"]) is not int or type(f["end"]) is not int or not 0 <= f["start"] < f["end"] <= len(text):
                    raise ValueError("invalid_span")
                if text[f["start"]:f["end"]] != f["evidence"]:
                    raise ValueError("evidence_mismatch")
                if f["subject"] not in ("self", "family_relation") or (f["subject"] == "family_relation" and f["tag"] != "FAM"):
                    raise ValueError("invalid_subject")
                if f["time"] not in ("present", "past", "unspecified") or f["assertion"] not in ("affirmative", "negative"):
                    raise ValueError("invalid_assertion")
            categories.update(tags)
        except (KeyError, TypeError, ValueError) as e:
            errors.append({"record_id": record_id, "error": str(e)})
    from .common import read_json
    summary = read_json(run_dir / "summary.json")
    if summary["output_sha256"] != file_hash(run_dir / "privacy_dataset.jsonl"):
        errors.append({"error": "output_checksum_mismatch"})
    if summary["counts"].get("accepted", 0) != len(rows):
        errors.append({"error": "summary_count_mismatch"})
    if summary["category_counts"] != {tag: categories[tag] for tag in TAGS}:
        errors.append({"error": "summary_category_mismatch"})
    sources = verify_sources(rows, raw_root) if raw_root is not None else None
    eligible = bool(rows) and not any(r.get("synthetic", True) for r in rows) and not summary["synthetic"]
    return {"records": len(rows), "categories": dict(categories), "errors": errors,
            "structurally_valid": not errors, "source_verification": sources,
            "count_thresholds_met": eligible and not errors and len(rows) >= 50 and len(categories) >= 3,
            "note": "Structural checks do not establish semantic correctness; source checks require --raw-root."}


def compare_runs(first: Path, second: Path) -> dict:
    left = {r["record_id"]: r for r in jsonl(first / "privacy_dataset.jsonl")}
    right = {r["record_id"]: r for r in jsonl(second / "privacy_dataset.jsonl")}
    common = left.keys() & right.keys()
    mismatches = [k for k in common if left[k] != right[k]]
    return {"identical_bytes": file_hash(first / "privacy_dataset.jsonl") == file_hash(second / "privacy_dataset.jsonl"),
            "left_only": sorted(left.keys() - right.keys()), "right_only": sorted(right.keys() - left.keys()),
            "changed_records": sorted(mismatches)}
