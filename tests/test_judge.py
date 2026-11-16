"""Responses protocol and blind model evaluation; no external API calls."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler
import json
import subprocess
import sys
from unittest.mock import patch

from tests.support import annotation
from tests.test_core import TemporaryCase
from tests.test_integration import server
from privacy_pilot.common import ROOT, PilotError, jsonl, write_json, write_jsonl
from privacy_pilot.source import prepare
from privacy_pilot.evaluation import evaluate
from privacy_pilot.judge import JUDGE_DEFAULTS, judge, load_judge_config
from privacy_pilot.model import ModelClient, ModelError


def response(value, status="completed"):
    return {"status": status, "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "status": "completed", "content": [
            {"type": "output_text", "text": json.dumps(value)}]}],
        "usage": {"input_tokens": 12, "output_tokens": 8}}


def transport(request):
    data = json.loads(request["input"][1]["content"])
    return response(annotation(data["text"]))


class JudgeTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.jcfg = {**JUDGE_DEFAULTS, "api_key_env": "", "retries": 0}

    def judged(self, folder="judge", scope="holdout", mode="resume", transport_fn=transport):
        if not (self.root / "run").exists():
            self.run_fixture()
        source = self.root / ("prepared/input.jsonl" if scope == "holdout" else "run/privacy_dataset.jsonl")
        return judge(source, self.root / folder, self.jcfg, self.root / "judge-cache", mode,
                     100, 3602, scope, transport=transport_fn)

    def test_config_and_response_payload(self):
        cfg = load_judge_config(ROOT / "config/judge.toml")
        self.assertEqual(cfg, JUDGE_DEFAULTS)
        model = {**self.cfg["model"], "provider": "responses", "name": cfg["model"],
                 "reasoning_effort": cfg["reasoning_effort"]}
        request = ModelClient(model, self.root / "cache")._request([{"role": "user", "content": "JSON"}])
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(request["reasoning"]["effort"], "high")
        self.assertFalse(request["store"])
        self.assertFalse(request["stream"])
        self.assertEqual(request["text"]["format"]["type"], "json_object")
        for key in ("messages", "temperature", "max_tokens", "tools", "api_key"):
            self.assertNotIn(key, request)
        for change in ({"api_key": "dummy"}, {"endpoint": "https://example.com/responses?key=dummy"},
                       {"actor_authorization": "bad\r\nHeader: injected"}, {"max_calls": 0}):
            write_json(self.root / "bad.json", {"judge": {**cfg, **change}})
            with self.subTest(change=change), self.assertRaises(PilotError):
                load_judge_config(self.root / "bad.json")

    def test_responses_parser_rejects_refusal_incomplete_and_malformed(self):
        client = ModelClient({**self.cfg["model"], "provider": "responses"}, self.root / "cache")
        self.assertEqual(json.loads(client._content(response(annotation("")))), annotation(""))
        refusal = response(annotation(""))
        refusal["output"][1]["content"] = [{"type": "refusal", "refusal": "No"}]
        for value in (refusal, response(annotation(""), "incomplete"), {"status": "completed", "output": [None]},
                      {"status": "completed", "output": []}, response(annotation(""), "failed")):
            with self.subTest(value=value), self.assertRaises(ModelError):
                client._content(value)

    def test_blind_judgment_and_model_relative_metrics(self):
        seen = []
        def capture(request):
            data = json.loads(request["input"][1]["content"])
            self.assertLessEqual(set(data), {"text", "candidate_annotation"})
            seen.append(data)
            return transport(request)
        summary = self.judged(transport_fn=capture)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["counts"]["ok"], 14)
        self.assertEqual(len(seen), 28)  # Judge + review for negatives as well as positives.
        rows = list(jsonl(self.root / "judge/judgments.jsonl"))
        self.assertTrue(all(r["source_verified"] is None and r["annotation_seconds"] is None for r in rows))
        self.assertEqual(sum(not r["gold_labels"] for r in rows), 2)
        score = evaluate(self.root / "run/predictions.jsonl", self.root / "judge/judgments.jsonl")
        self.assertEqual(score["reference_kind"], "llm_judge")
        self.assertFalse(score["reference_is_human"])
        self.assertEqual(score["micro"]["f1"], 1)
        self.assertNotIn("annotation_seconds", score)
        self.assertNotIn("source_verification_rate", score)
        self.assertEqual(summary["model_stats"]["referenced_input_tokens"], 28 * 12)

    def test_final_audit_has_no_population_recall(self):
        self.judged(scope="final")
        score = evaluate(self.root / "run/predictions.jsonl", self.root / "judge/judgments.jsonl")
        self.assertEqual(score["text_validity_rate"], 1)
        self.assertNotIn("micro", score)

    def test_partial_judgments_report_coverage_and_score_only_resolved_rows(self):
        self.judged()
        rows = list(jsonl(self.root / "judge/judgments.jsonl"))
        excluded = [r for r in rows if r["gold_labels"]][:2]
        for row in excluded:
            row.update(judge_status="needs_review", gold_labels=None)
        path = self.root / "partial.jsonl"
        write_jsonl(path, rows)
        score = evaluate(self.root / "run/predictions.jsonl", path)
        self.assertEqual(score["sample_size"], 14)
        self.assertEqual(score["evaluated_count"], 12)
        self.assertEqual(score["judge_abstained_count"], 2)
        self.assertEqual(score["judge_coverage"], 12 / 14)
        self.assertEqual(score["judge_abstention_rate"], 2 / 14)
        self.assertEqual(score["gold_positive_count"], 10)
        self.assertEqual(score["positive_fraction"], 10 / 12)
        self.assertEqual(score["exact_match"], 1)
        self.assertEqual(score["micro"]["f1"], 1)
        self.assertEqual(score["abstained_count"], 0)
        self.assertEqual({r["record_id"] for r in score["judge_abstentions"]},
                         {r["record_id"] for r in excluded})
        self.assertEqual(score["metrics_scope"], "resolved_judgments_only")
        self.assertIn("do not represent the entire sample", score["note"])
        # Exercise the public command with the unchanged, full judgment file.
        result = subprocess.run([sys.executable, str(ROOT / "run.py"), "evaluate", "--predictions",
                                 str(self.root / "run/predictions.jsonl"), "--gold", str(path),
                                 "--output", str(self.root / "metrics.json")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "metrics.json").read_text(encoding="utf-8")), score)
        self.assertEqual(list(jsonl(path)), rows)

    def test_all_judgments_abstain_produces_null_scores(self):
        self.judged(scope="final")
        rows = list(jsonl(self.root / "judge/judgments.jsonl"))
        for row in rows:
            row.update(judge_status="needs_review", gold_labels=None)
        path = self.root / "abstained.jsonl"
        write_jsonl(path, rows)
        score = evaluate(self.root / "run/predictions.jsonl", path)
        self.assertEqual(score["evaluated_count"], 0)
        self.assertEqual(score["judge_coverage"], 0)
        self.assertEqual(score["judge_abstained_count"], len(rows))
        for key in ("label_precision", "exact_match", "positive_fraction", "text_validity_rate"):
            self.assertIsNone(score[key])

    def test_partial_scoring_rejects_errors_and_unprocessed_judgments(self):
        self.judged()
        original = list(jsonl(self.root / "judge/judgments.jsonl"))
        for status in ("error", "unprocessed_budget", "unprocessed_authentication", "unknown"):
            rows = deepcopy(original)
            rows[0].update(judge_status=status, gold_labels=None)
            write_jsonl(self.root / "failed.jsonl", rows)
            with self.subTest(status=status), self.assertRaises(PilotError):
                evaluate(self.root / "run/predictions.jsonl", self.root / "failed.jsonl")

    def test_abstaining_rows_still_require_valid_identity_and_evidence(self):
        self.judged()
        original = list(jsonl(self.root / "judge/judgments.jsonl"))
        for case in ("labels", "text", "source", "evidence", "missing_prediction", "provenance"):
            rows = deepcopy(original)
            row = next(r for r in rows if r["gold_labels"])
            row.update(judge_status="needs_review", gold_labels=None)
            if case == "labels":
                row["gold_labels"] = []
            elif case == "text":
                row["text_sha256"] = "wrong"
            elif case == "source":
                row["source_ref"] = {}
            elif case == "evidence":
                row["judge_evidence"][0]["end"] -= 1
            elif case == "missing_prediction":
                row["record_id"] = "missing"
            else:
                row["judge"]["model"] = "other"
            write_jsonl(self.root / "tampered.jsonl", rows)
            with self.subTest(case=case), self.assertRaises(PilotError):
                evaluate(self.root / "run/predictions.jsonl", self.root / "tampered.jsonl")

    def test_replay_and_cli_use_no_key_or_network(self):
        self.judged()
        with patch("privacy_pilot.model.read_api_key", side_effect=AssertionError("read key")):
            summary = self.judged("replay", mode="replay", transport_fn=lambda _: self.fail("network"))
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["model_stats"].get("network_attempts", 0), 0)
        first = list(jsonl(self.root / "judge/judgments.jsonl"))
        second = list(jsonl(self.root / "replay/judgments.jsonl"))
        for a, b in zip(first, second):
            a.pop("judge_seconds")
            b.pop("judge_seconds")
            self.assertEqual(a, b)
        # Supply a syntactically valid unused key variable for the public CLI loader.
        cfg = {**self.jcfg, "api_key_env": "JUDGE_TEST_UNUSED"}
        write_json(self.root / "cli.json", {"judge": cfg})
        result = subprocess.run([sys.executable, str(ROOT / "run.py"), "judge", "--input", str(self.root / "prepared/input.jsonl"),
                                 "--output", str(self.root / "cli"), "--scope", "holdout", "--count", "100",
                                 "--config", str(self.root / "cli.json"), "--cache", str(self.root / "judge-cache"),
                                 "--mode", "replay"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["complete"])

    def test_shared_request_budget_and_missing_cache_fail_closed(self):
        self.jcfg["max_calls"] = 1
        result = self.judged()
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped_reason"], "budget")
        self.assertEqual(result["model_stats"]["network_attempts"], 1)
        with self.assertRaises(PilotError):
            evaluate(self.root / "run/predictions.jsonl", self.root / "judge/judgments.jsonl")
        self.jcfg["max_calls"] = 1500
        result = self.judged("missing", mode="replay", transport_fn=lambda _: self.fail("network"))
        self.assertFalse(result["complete"])
        self.assertEqual(result["counts"]["error"], 14)

    def test_ambiguity_and_invalid_evidence_do_not_become_negative(self):
        for name, ambiguous in (("ambiguous", True), ("invalid", False)):
            self.jcfg["model"] = "test-" + name  # Separate cache identities for distinct faulty responses.
            def bad(request):
                value = annotation("I am 27 years old.")
                value["review_required"] = ambiguous
                if not ambiguous:
                    value["findings"][0]["evidence"] = "fabricated evidence"
                return response(value)
            summary = self.judged(name, transport_fn=bad)
            self.assertFalse(summary["complete"])
            self.assertTrue(all(r["gold_labels"] is None for r in jsonl(self.root / name / "judgments.jsonl")))
            with self.assertRaises(PilotError):
                evaluate(self.root / "run/predictions.jsonl", self.root / name / "judgments.jsonl")

    def test_scoring_rejects_mixed_provenance_and_tampered_evidence(self):
        self.judged()
        original = list(jsonl(self.root / "judge/judgments.jsonl"))
        for case in ("kind", "provenance", "evidence", "human_claim"):
            rows = deepcopy(original)
            row = next(r for r in rows if r["gold_labels"])
            if case == "kind":
                row["reference_kind"] = "human"
            elif case == "provenance":
                row["judge"]["model"] = "other"
            elif case == "evidence":
                row["judge_evidence"][0]["end"] -= 1
            else:
                row["source_verified"] = True
            write_jsonl(self.root / "tampered.jsonl", rows)
            with self.subTest(case=case), self.assertRaises(PilotError):
                evaluate(self.root / "run/predictions.jsonl", self.root / "tampered.jsonl")

    def test_judge_reviews_long_text_without_rule_filtering(self):
        text = "I am 27 years old. " + "plain sentence. " * 30 + "I work as a nurse."
        write_jsonl(self.root / "long.jsonl", [{"id": "long", "body": text},
                                             {"id": "negative", "body": "The software release is ready."}])
        prepare(self.root / "long.jsonl", self.root / "prepared", self.cfg)
        self.jcfg.update(chunk_chars=100, overlap_chars=20, max_chunks=100)
        summary = self.judged()
        self.assertEqual(summary["counts"]["ok"], 2)
        rows = list(jsonl(self.root / "judge/judgments.jsonl"))
        self.assertTrue(any(not r["gold_labels"] for r in rows))
        positive = next(r for r in rows if r["gold_labels"])
        self.assertEqual(positive["gold_labels"], ["AGE", "OCC"])
        for f in positive["judge_evidence"]:
            self.assertEqual(text[f["start"]:f["end"]], f["evidence"])

    def test_actual_responses_http_headers_and_secret_not_cached(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, dict(self.headers), payload))
                raw = json.dumps(response(annotation(""))).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *_):
                pass
        with server(Handler) as base, patch.dict("os.environ", {"JUDGE_FAKE_KEY": "fake-judge-key"}):
            cfg = {**self.cfg["model"], "provider": "responses", "endpoint": base + "/v1/responses",
                   "api_key_env": "JUDGE_FAKE_KEY", "actor_authorization": "local-image-extension"}
            client = ModelClient(cfg, self.root / "http-cache")
            client.complete([{"role": "user", "content": "JSON"}], "judge")
        path, headers, payload = received[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer fake-judge-key")
        self.assertEqual({k.lower(): v for k, v in headers.items()}["x-openai-actor-authorization"], "local-image-extension")
        self.assertFalse(payload["store"])
        for cached in (self.root / "http-cache").glob("*.json"):
            self.assertNotIn("fake-judge-key", cached.read_text(encoding="utf-8"))
