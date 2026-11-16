from copy import deepcopy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import zipfile

from tests.support import annotation, config, raw_fixture, response, transport
from privacy_pilot.common import PilotError, canonical, jsonl, read_json, write_json, write_jsonl
from privacy_pilot.config import load_config, validate_config
from privacy_pilot.evaluation import compare_runs, evaluate, make_audit, split_prepared, verify_result
from privacy_pilot.model import AuthenticationError, BudgetExceeded, CacheMiss, ModelClient, ModelError
from privacy_pilot.pipeline import run
from privacy_pilot.rules import candidate_rules, chunks
from privacy_pilot.schema import AnnotationError, parse_annotation
from privacy_pilot.source import load_prepared, prepare, verify_sources


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config()

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self):
        raw_fixture(self.root / "raw.jsonl")
        prepare(self.root / "raw.jsonl", self.root / "prepared", self.cfg)
        return self.root / "prepared"

    def run_fixture(self, folder="run", mode="resume", transport_fn=transport):
        prepared = self.root / "prepared"
        if not prepared.exists():
            self.prepared()
        client = ModelClient(self.cfg["model"], self.root / "cache", mode, transport_fn)
        summary = run(prepared, self.root / folder, self.cfg, self.root / "cache", mode, client=client)
        return summary, client


class SchemaTests(unittest.TestCase):
    def test_unicode_offset(self):
        text = "🙂 Prefix. I am 27 years old."
        result = parse_annotation(json.dumps(annotation(text)), text, 100)
        f = result["findings"][0]
        self.assertEqual(f["start"], text.index("I am") + 100)
        self.assertEqual(text[f["start"] - 100:f["end"] - 100], f["evidence"])

    def test_hallucinated_evidence_rejected(self):
        with self.assertRaises(AnnotationError):
            parse_annotation(json.dumps(annotation("I am 27 years old.")), "I like music.")

    def test_relative_age_not_author(self):
        item = annotation("I am 27 years old.")
        item["findings"][0]["subject"] = "family_relation"
        with self.assertRaises(AnnotationError):
            parse_annotation(json.dumps(item), "I am 27 years old.")

    def test_unknown_and_inconsistent_labels(self):
        value = annotation("I am 27 years old.")
        for change in ("label", "flag", "type"):
            item = deepcopy(value)
            if change == "label":
                item["findings"][0]["tag"] = "PASSWORD"
            elif change == "flag":
                item["has_privacy"] = False
            else:
                item["review_required"] = "false"
            with self.subTest(change=change), self.assertRaises(AnnotationError):
                parse_annotation(json.dumps(item), "I am 27 years old.")

    def test_ambiguous_duplicate_span(self):
        text = "I am 27 years old. Again: I am 27 years old."
        with self.assertRaises(AnnotationError):
            parse_annotation(json.dumps(annotation(text)), text)

    def test_chunks_cover_all_text(self):
        text = "🙂 hello. " * 140
        covered = set()
        for start, piece in chunks(text, 100, 15):
            self.assertEqual(text[start:start + len(piece)], piece)
            covered.update(range(start, start + len(piece)))
        self.assertEqual(covered, set(range(len(text))))

    def test_short_text_candidate(self):
        self.assertTrue(candidate_rules("I'm 35"))


class SourceTests(TemporaryCase):
    def test_prepare_dedup_and_source_check(self):
        folder = self.prepared()
        records, manifest = load_prepared(folder)
        self.assertEqual(len(records), 14)
        self.assertEqual(manifest["counts"]["duplicate_text"], 1)
        self.assertTrue(verify_sources(records, self.root)["ok"])
        self.assertEqual(records[0]["text"], "I am 27 years old.")
        self.assertEqual(records[0]["source_ref"]["record_type"], "unknown")
        self.assertEqual(records[0]["link_type"], "dataset")

    def test_zip_and_gzip_input(self):
        body = canonical({"id": "one", "body": "I am 27 years old."}) + "\n"
        with zipfile.ZipFile(self.root / "raw.zip", "w") as z:
            z.writestr("a/part-00000", body)
            z.writestr("_SUCCESS", "")
        with gzip.open(self.root / "raw.gz", "wt", encoding="utf-8") as f:
            f.write(body)
        for suffix in ("zip", "gz"):
            prepare(self.root / f"raw.{suffix}", self.root / suffix, self.cfg)
            records, _ = load_prepared(self.root / suffix)
            self.assertEqual(len(records), 1)
            self.assertTrue(verify_sources(records, self.root)["ok"])

    def test_source_tampering(self):
        folder = self.prepared()
        records, _ = load_prepared(folder)
        with (self.root / "raw.jsonl").open("a", encoding="utf-8") as f:
            f.write("\n")
        self.assertFalse(verify_sources(records, self.root)["ok"])

    def test_prepared_tampering(self):
        folder = self.prepared()
        with (folder / "input.jsonl").open("a", encoding="utf-8") as f:
            f.write("\n")
        with self.assertRaises(PilotError):
            load_prepared(folder)

    def test_no_overwrite(self):
        self.prepared()
        with self.assertRaises(PilotError):
            prepare(self.root / "raw.jsonl", self.root / "prepared", self.cfg)

    def test_malformed_labelled_synthetic_and_conflicting_records(self):
        rows = [{"id": "x", "body": "I am 27 years old."},
                {"id": "x", "body": "I am 28 years old."},
                {"id": "y", "body": "I am 29 years old.", "privacy_tag": ["AGE"]},
                {"id": "z", "body": "I am 30 years old.", "synthetic": True}]
        write_jsonl(self.root / "raw.jsonl", rows)
        with (self.root / "raw.jsonl").open("a") as f:
            f.write("not JSON\n")
        self.cfg["source"]["synthetic"] = False
        m = prepare(self.root / "raw.jsonl", self.root / "prepared", self.cfg)
        self.assertEqual(m["counts"]["accepted"], 1)
        for reason in ("source_id_conflict", "preannotated_privacy_input", "synthetic_input", "malformed_json"):
            self.assertEqual(m["counts"][reason], 1)

    def test_fixed_start_and_limit(self):
        raw_fixture(self.root / "raw.jsonl")
        self.cfg["source"].update(start=2, limit=3)
        prepare(self.root / "raw.jsonl", self.root / "prepared", self.cfg)
        records, _ = load_prepared(self.root / "prepared")
        self.assertEqual([r["source_ref"]["line"] for r in records], [3, 4, 5])


class ModelTests(TemporaryCase):
    def messages(self):
        return [{"role": "user", "content": "test"}]

    def test_replay_never_calls_network(self):
        def forbidden(_):
            self.fail("Replay called network")
        client = ModelClient(self.cfg["model"], self.root / "cache", "replay", forbidden)
        with self.assertRaises(CacheMiss):
            client.complete(self.messages(), "extract")

    def test_cache_invalidated_by_prompt_model_and_parameters(self):
        client = ModelClient(self.cfg["model"], self.root / "cache", transport=lambda _: response(annotation("")))
        _, key = client.complete(self.messages(), "extract")
        for change in ("name", "temperature", "endpoint"):
            cfg = deepcopy(self.cfg["model"])
            cfg[change] = {"name": "other-model", "temperature": 0.5, "endpoint": "https://other.example/chat"}[change]
            with self.subTest(change=change), self.assertRaises(CacheMiss):
                ModelClient(cfg, self.root / "cache", "replay").complete(self.messages(), "extract")
        with self.assertRaises(CacheMiss):
            ModelClient(self.cfg["model"], self.root / "cache", "replay").complete([], "extract")
        entry = read_json(self.root / "cache" / f"{key}.json")
        entry["response"]["choices"][0]["message"]["content"] = "tampered"
        write_json(self.root / "cache" / f"{key}.json", entry)
        with self.assertRaises(ModelError):
            client.complete(self.messages(), "extract")

    def test_retry_and_call_budget(self):
        cfg = deepcopy(self.cfg["model"])
        cfg.update(retries=3, max_calls=2)
        def limited(_):
            raise urllib.error.HTTPError("https://example.com", 429, "limit", {}, None)
        client = ModelClient(cfg, self.root / "cache", transport=limited, sleeper=lambda _: None)
        with self.assertRaises(BudgetExceeded):
            client.complete(self.messages(), "extract")
        self.assertEqual(client.stats["network_attempts"], 2)

    def test_auth_not_retried(self):
        def denied(_):
            raise urllib.error.HTTPError("https://example.com", 401, "denied", {}, None)
        client = ModelClient(self.cfg["model"], self.root / "cache", transport=denied)
        with self.assertRaises(AuthenticationError):
            client.complete(self.messages(), "extract")
        self.assertEqual(client.stats["network_attempts"], 1)

    def test_truncated_not_accepted(self):
        item = response(annotation(""))
        item["choices"][0]["finish_reason"] = "length"
        client = ModelClient(self.cfg["model"], self.root / "cache", transport=lambda _: item)
        with self.assertRaises(ModelError):
            client.complete(self.messages(), "extract")
        self.assertEqual(len(client.cache_keys), 1)

    def test_ollama_shape(self):
        cfg = deepcopy(self.cfg["model"])
        cfg["provider"] = "ollama"
        def local(request):
            self.assertFalse(request["stream"])
            self.assertEqual(request["format"], "json")
            return {"done": True, "done_reason": "stop", "message": {"content": json.dumps(annotation(""))}}
        client = ModelClient(cfg, self.root / "cache", transport=local)
        content, _ = client.complete(self.messages(), "extract")
        self.assertFalse(json.loads(content)["has_privacy"])


class PipelineTests(TemporaryCase):
    def test_end_to_end_and_identical_replay(self):
        summary, client = self.run_fixture()
        self.assertEqual(summary["counts"]["accepted"], 12)
        self.assertEqual(summary["category_coverage"], 12)
        self.assertEqual(summary["counts"]["rule_negative"], 1)
        self.assertEqual(summary["counts"]["model_negative"], 1)
        self.assertFalse(summary["count_thresholds_met"])
        self.assertTrue(verify_result(self.root / "run", self.root)["source_verification"]["ok"])
        def no_network(_):
            self.fail("Replay attempted model call")
        replay, client = self.run_fixture("replay", "replay", no_network)
        self.assertEqual(client.stats["network_attempts"], 0)
        self.assertTrue(compare_runs(self.root / "run", self.root / "replay")["identical_bytes"])
        self.assertTrue(replay["complete_analysis"])

    def test_invalid_output_repair(self):
        def repairing(request):
            if len(request["messages"]) == 2:
                return response({"bad": "schema"})
            return transport(request)
        summary, _ = self.run_fixture(transport_fn=repairing)
        self.assertEqual(summary["counts"]["accepted"], 12)
        rows = list(jsonl(self.root / "run/predictions.jsonl"))
        self.assertEqual(rows[0]["trace"][0]["validation"], "failed")
        self.assertEqual(rows[0]["trace"][1]["validation"], "passed")

    def test_failed_calls_are_not_negatives(self):
        summary, _ = self.run_fixture(transport_fn=lambda _: response({"bad": "schema"}))
        self.assertEqual(summary["counts"]["error"], 13)
        self.assertEqual(summary["counts"].get("model_negative", 0), 0)
        self.assertFalse(summary["complete_analysis"])

    def test_review_can_remove_false_positive(self):
        def reviewing(request):
            data = json.loads(request["messages"][1]["content"])
            return response(annotation("")) if "candidate_annotation" in data else transport(request)
        summary, _ = self.run_fixture(transport_fn=reviewing)
        self.assertEqual(summary["counts"].get("accepted", 0), 0)

    def test_ambiguity_is_not_exported(self):
        def uncertain(request):
            data = json.loads(request["messages"][1]["content"])
            item = annotation(data["text"])
            item["review_required"] = True
            return response(item)
        summary, _ = self.run_fixture(transport_fn=uncertain)
        self.assertEqual(summary["counts"]["needs_review"], 13)
        self.assertEqual(summary["counts"].get("accepted", 0), 0)

    def test_long_text_counts_one_record_and_full_offsets(self):
        text = "I am 27 years old. " + "Unrelated sentence. " * 40 + "I work as a nurse."
        write_jsonl(self.root / "raw.jsonl", [{"id": "long", "body": text}])
        self.cfg["pipeline"].update(chunk_chars=150, overlap_chars=40)
        prepare(self.root / "raw.jsonl", self.root / "prepared", self.cfg)
        summary, _ = self.run_fixture()
        self.assertEqual(summary["counts"]["accepted"], 1)
        row = next(jsonl(self.root / "run/privacy_dataset.jsonl"))
        self.assertEqual(row["privacy_tag"], ["AGE", "OCC"])
        for finding in row["evidence"]:
            self.assertEqual(text[finding["start"]:finding["end"]], finding["evidence"])

    def test_budget_marks_unprocessed(self):
        self.cfg["model"]["max_calls"] = 1
        summary, _ = self.run_fixture()
        self.assertEqual(summary["counts"]["error"], 1)
        self.assertGreater(summary["counts"]["unprocessed_budget"], 0)
        self.assertFalse(summary["complete_analysis"])

    def test_partial_chunk_failure_does_not_publish(self):
        text = "I am 27 years old. " + "plain words " * 100
        write_jsonl(self.root / "raw.jsonl", [{"id": "long", "body": text}])
        self.cfg["pipeline"].update(chunk_chars=150, overlap_chars=20, review=False)
        prepare(self.root / "raw.jsonl", self.root / "prepared", self.cfg)
        def broken(request):
            data = json.loads(request["messages"][1]["content"])
            return transport(request) if "27" in data["text"] else response({"bad": True})
        summary, _ = self.run_fixture(transport_fn=broken)
        self.assertEqual(summary["counts"]["error"], 1)
        self.assertEqual(summary["counts"].get("accepted", 0), 0)


class EvaluationTests(TemporaryCase):
    def completed_audit(self, scope="final"):
        self.run_fixture()
        source = self.root / ("run/privacy_dataset.jsonl" if scope == "final" else "prepared/input.jsonl")
        gold = self.root / "gold.jsonl"
        make_audit(source, gold, 100, 3602, scope)
        rows = list(jsonl(gold))
        for r in rows:
            r.update(gold_labels=[f["tag"] for f in annotation(r["text"])["findings"]], annotation_seconds=1)
        write_jsonl(gold, rows)
        return gold

    def test_final_audit_does_not_claim_recall(self):
        gold = self.completed_audit()
        result = evaluate(self.root / "run/predictions.jsonl", gold)
        self.assertEqual(result["label_precision"], 1)
        self.assertNotIn("micro", result)
        self.assertIsNone(result["source_verification_rate"])

    def test_holdout_metrics_with_false_negative(self):
        gold = self.completed_audit("holdout")
        rows = list(jsonl(gold))
        negative = next(r for r in rows if not r["gold_labels"])
        negative["gold_labels"] = ["AGE"]  # Deliberate metric test, NOT a real annotation.
        write_jsonl(gold, rows)
        result = evaluate(self.root / "run/predictions.jsonl", gold)
        self.assertEqual(result["micro"]["fn"], 1)
        self.assertAlmostEqual(result["micro"]["recall"], 12 / 13)

    def test_missing_annotation_and_prediction_are_errors(self):
        self.run_fixture()
        gold = self.root / "gold.jsonl"
        make_audit(self.root / "run/privacy_dataset.jsonl", gold, 2, 1, "final")
        with self.assertRaises(PilotError):
            evaluate(self.root / "run/predictions.jsonl", gold)
        write_jsonl(self.root / "empty.jsonl", [])
        rows = list(jsonl(gold))
        for r in rows:
            r.update(gold_labels=[], annotation_seconds=1)
        write_jsonl(gold, rows)
        with self.assertRaises(PilotError):
            evaluate(self.root / "empty.jsonl", gold)

    def test_disjoint_reproducible_splits(self):
        self.prepared()
        for name in ("split1", "split2"):
            split_prepared(self.root / "prepared", self.root / name, 2, 3, 3602)
        sets = [set(r["record_id"] for r in load_prepared(self.root / "split1" / part)[0])
                for part in ("dev", "test", "collection")]
        self.assertEqual(len(set.union(*sets)), 14)
        self.assertFalse(sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
        self.assertEqual((self.root / "split1/test/input.jsonl").read_bytes(), (self.root / "split2/test/input.jsonl").read_bytes())

    def test_config_snapshot_roundtrip_and_validation(self):
        write_json(self.root / "snapshot.json", self.cfg)
        self.assertEqual(load_config(self.root / "snapshot.json"), self.cfg)
        for key, value in (("overlap_chars", 9000), ("review", "false"), ("min_classes", 13)):
            bad = deepcopy(self.cfg)
            bad["pipeline"][key] = value
            with self.subTest(key=key), self.assertRaises(PilotError):
                validate_config(bad)


if __name__ == "__main__":
    unittest.main()
