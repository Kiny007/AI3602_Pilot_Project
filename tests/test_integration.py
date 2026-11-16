"""Real local HTTP and portable CLI tests; no external model or Reddit requests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import sys
import threading
from unittest.mock import patch

from tests.support import annotation, response
from tests.test_core import TemporaryCase
from privacy_pilot.common import ROOT, write_json
from privacy_pilot.model import ModelClient, ModelError


@contextmanager
def server(handler):
    service = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}"
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=5)


class HTTPIntegrationTests(TemporaryCase):
    def test_real_http_payload_headers_and_utf8(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append({"body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                 "authorization": self.headers.get("Authorization"), "path": self.path})
                raw = json.dumps(response(annotation(""))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        with server(Handler) as base:
            self.cfg["model"].update(endpoint=base + "/chat/completions", api_key_env="TEST_PILOT_KEY")
            with patch.dict("os.environ", {"TEST_PILOT_KEY": "constructed-test-key"}):
                client = ModelClient(self.cfg["model"], self.root / "cache")
                client.complete([{"role": "user", "content": "中文🙂"}], "extract")
        self.assertEqual(received[0]["body"]["messages"][0]["content"], "中文🙂")
        self.assertEqual(received[0]["authorization"], "Bearer constructed-test-key")
        self.assertEqual(received[0]["path"], "/chat/completions")
        for cached in (self.root / "cache").glob("*.json"):
            self.assertNotIn("constructed-test-key", cached.read_text(encoding="utf-8"))

    def test_redirect_is_not_followed(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)
                self.send_header("Location", "/unexpected")
                self.end_headers()

            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        with server(Handler) as base:
            self.cfg["model"]["endpoint"] = base + "/chat"
            client = ModelClient(self.cfg["model"], self.root / "cache")
            with self.assertRaises(ModelError):
                client.complete([{"role": "user", "content": "sensitive text"}], "extract")
        self.assertEqual(seen, ["/chat"])


class CLIIntegrationTests(TemporaryCase):
    def command(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, str(cwd / "run.py"), *map(str, args)],
                              cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_cli_utf8_and_invalid_model_config(self):
        help_result = self.command("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("隐私", help_result.stdout)
        self.prepared()
        bad_cfg = self.cfg.copy()
        bad_cfg["model"] = {**self.cfg["model"], "name": ""}
        write_json(self.root / "config.json", bad_cfg)
        result = self.command("run", "--prepared", self.root / "prepared", "--output", self.root / "out",
                              "--config", self.root / "config.json", "--cache", self.root / "cache")
        self.assertEqual(result.returncode, 2)
        self.assertIn("model.name", result.stderr)
        self.assertFalse((self.root / "out").exists())

    def test_cli_replay_verify_and_evaluate_unfilled(self):
        self.run_fixture()
        result = self.command("run", "--prepared", self.root / "prepared", "--config", self.root / "run/config.snapshot.json",
                              "--prompts", self.root / "run/prompts", "--output", self.root / "replay",
                              "--cache", self.root / "cache", "--mode", "replay")
        self.assertEqual(result.returncode, 0, result.stderr)
        compared = self.command("compare", self.root / "run", self.root / "replay")
        self.assertTrue(json.loads(compared.stdout)["identical_bytes"])
        verified = self.command("verify", "--run", self.root / "run", "--raw-root", self.root)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["source_verification"]["ok"])
        minimum = self.command("verify", "--run", self.root / "run", "--require-minimum")
        self.assertEqual(minimum.returncode, 2)  # artificial corpus is never a submission
