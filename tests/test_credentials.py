from copy import deepcopy
import os
from unittest.mock import patch

from tests.test_core import TemporaryCase
from privacy_pilot.common import ROOT, PilotError
from privacy_pilot.config import DEFAULTS, load_config
from privacy_pilot.credentials import read_api_key
from privacy_pilot.model import ModelClient


class DeepSeekCredentialTests(TemporaryCase):
    def test_default_deepseek_payload(self):
        cfg = load_config(ROOT / "config/default.toml")
        self.assertEqual(cfg["model"], DEFAULTS["model"])
        client = ModelClient(cfg["model"], self.root / "cache")
        payload = client._request([{"role": "user", "content": "Return JSON"}])
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("api_key", payload)
        other = deepcopy(cfg["model"])
        other["provider"] = "chat_completions"
        self.assertNotIn("thinking", ModelClient(other, self.root / "other")._request([]))

    def test_dotenv_and_environment_precedence(self):
        path = self.root / ".env"
        path.write_text('# comment\nIGNORED=other\nDEEPSEEK_API_KEY="file-test-key"\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch("privacy_pilot.credentials.getpass.getpass") as prompt:
            self.assertEqual(read_api_key("DEEPSEEK_API_KEY", path), "file-test-key")
            prompt.assert_not_called()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-test-key"}):
            self.assertEqual(read_api_key("DEEPSEEK_API_KEY", path), "environment-test-key")

    def test_hidden_prompt_does_not_write_file_or_environment(self):
        path = self.root / ".env"
        with patch.dict(os.environ, {}, clear=True), \
             patch("privacy_pilot.credentials.sys.stdin.isatty", return_value=True), \
             patch("privacy_pilot.credentials.getpass.getpass", return_value="temporary-test-key"):
            self.assertEqual(read_api_key("DEEPSEEK_API_KEY", path), "temporary-test-key")
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        self.assertFalse(path.exists())

    def test_noninteractive_missing_key_fails_without_prompt(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("privacy_pilot.credentials.sys.stdin.isatty", return_value=False), \
             patch("privacy_pilot.credentials.getpass.getpass") as prompt:
            with self.assertRaisesRegex(PilotError, "DEEPSEEK_API_KEY"):
                read_api_key("DEEPSEEK_API_KEY", self.root / ".env")
            prompt.assert_not_called()

    def test_unsafe_key_is_rejected_without_echoing_secret(self):
        secret = "test-secret\r\nHeader: injected"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            with self.assertRaises(PilotError) as result:
                read_api_key("DEEPSEEK_API_KEY", self.root / ".env")
            self.assertNotIn("test-secret", str(result.exception))
