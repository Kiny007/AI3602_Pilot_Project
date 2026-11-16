from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from .common import PilotError, canonical, digest, read_json, write_json
from .credentials import read_api_key


class ModelError(PilotError):
    pass


class CacheMiss(ModelError):
    pass


class BudgetExceeded(ModelError):
    pass


class AuthenticationError(ModelError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authorization or private text to a redirect target.
        return None


class ModelClient:
    def __init__(self, cfg: dict, cache: Path, mode: str = "resume", transport=None, sleeper=time.sleep):
        self.cfg, self.cache, self.mode = cfg, cache, mode
        self.transport = transport or self._http
        self.sleep = sleeper
        self.stats = Counter()
        self.cache_keys = set()
        self._api_key = None
        if mode not in ("resume", "replay", "fresh"):
            raise PilotError("Unknown model mode")
        if not cfg["name"].strip():
            raise PilotError("Set model.name to the exact model identifier in your TOML configuration")
        if mode == "fresh" and cache.exists() and any(cache.iterdir()):
            raise PilotError("fresh mode requires a new/empty cache directory to preserve prior responses")
        if mode != "replay":
            cache.mkdir(parents=True, exist_ok=True)

    def _request(self, messages: list[dict]) -> dict:
        cfg = self.cfg
        if cfg["provider"] == "responses":
            return {"model": cfg["name"], "input": messages, "stream": False,
                    "store": False, "max_output_tokens": cfg["max_tokens"],
                    "reasoning": {"effort": cfg.get("reasoning_effort", "low")},
                    "text": {"format": {"type": "json_object"}}}
        request = {"model": cfg["name"], "messages": messages, "stream": False}
        if cfg["provider"] == "ollama":
            request["options"] = {"temperature": cfg["temperature"], "num_predict": cfg["max_tokens"]}
            if cfg["json_mode"]:
                request["format"] = "json"
        else:
            request.update(temperature=cfg["temperature"], max_tokens=cfg["max_tokens"])
            if cfg["provider"] == "deepseek":
                # Explicit non-thinking extraction avoids spending the small output budget on reasoning.
                request["thinking"] = {"type": "disabled"}
            if cfg["json_mode"]:
                request["response_format"] = {"type": "json_object"}
        return request

    def complete(self, messages: list[dict], stage: str) -> tuple[str, str]:
        request = self._request(messages)
        identity = {"cache_schema": 1, "provider": self.cfg["provider"],
                    "endpoint": self.cfg["endpoint"], "stage": stage, "request": request}
        key = digest(canonical(identity))
        path = self.cache / f"{key}.json"
        if path.exists():
            envelope = read_json(path)
            if envelope.get("identity") != identity or envelope.get("response_sha256") != digest(canonical(envelope.get("response"))):
                raise ModelError(f"Cache integrity mismatch: {key}")
            response = envelope["response"]
            self.stats["cache_hits"] += 1
        else:
            if self.mode == "replay":
                raise CacheMiss(f"Missing cached response for {stage}: {key}; replay never calls network")
            response = None
            for attempt in range(self.cfg["retries"] + 1):
                if self.stats["network_attempts"] >= self.cfg["max_calls"]:
                    raise BudgetExceeded("Network call budget exhausted (including HTTP retries)")
                self.stats["network_attempts"] += 1
                try:
                    response = self.transport(request)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        raise AuthenticationError(f"Provider authentication/access failed (HTTP {e.code})") from None
                    if e.code not in (408, 429, 500, 502, 503, 504) or attempt == self.cfg["retries"]:
                        raise ModelError(f"Provider HTTP {e.code}; check endpoint, model and supported JSON parameters") from None
                    self.stats["http_retries"] += 1
                    self.sleep(min(2 ** attempt, 10))
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                    if attempt == self.cfg["retries"]:
                        raise ModelError(f"Provider transport failed ({type(e).__name__}); check network/service") from None
                    self.stats["http_retries"] += 1
                    self.sleep(min(2 ** attempt, 10))
            # Preserve even malformed/truncated provider responses for reproducible failure analysis.
            write_json(path, {"identity": identity, "response": response,
                              "response_sha256": digest(canonical(response))})
        self.cache_keys.add(key)
        self.stats["completions"] += 1
        content = self._content(response)
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        self.stats["referenced_input_tokens"] += usage.get("input_tokens", usage.get("prompt_tokens", response.get("prompt_eval_count", 0))) or 0
        self.stats["referenced_output_tokens"] += usage.get("output_tokens", usage.get("completion_tokens", response.get("eval_count", 0))) or 0
        return content, key

    def _http(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": "AI3602-PrivacyPilot/0.1"}
        if self.cfg.get("actor_authorization"):
            headers["x-openai-actor-authorization"] = self.cfg["actor_authorization"]
        variable = self.cfg["api_key_env"]
        if variable and self._api_key is None:
            try:
                self._api_key = read_api_key(variable)
            except PilotError as e:
                raise AuthenticationError(str(e)) from None
        key = self._api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(self.cfg["endpoint"], data=canonical(payload).encode("utf-8"), headers=headers, method="POST")
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(req, timeout=self.cfg["timeout"]) as response:
            data = response.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ModelError("Provider response exceeded 10 MiB")
        try:
            return json.loads(data)
        except ValueError:
            raise ModelError("Provider returned non-JSON HTTP response") from None

    def _content(self, response: dict) -> str:
        try:
            if self.cfg["provider"] == "responses":
                if response.get("status") != "completed" or response.get("error") or response.get("incomplete_details"):
                    raise ModelError("Responses API response incomplete or failed; no partial labels accepted")
                parts = []
                for item in response["output"]:
                    if item.get("type") != "message":
                        continue
                    if item.get("status") not in (None, "completed"):
                        raise ModelError("Responses API message incomplete")
                    for part in item.get("content", []):
                        if part.get("type") == "refusal":
                            raise ModelError("Judge refused the annotation")
                        if part.get("type") == "output_text":
                            parts.append(part["text"])
                content = "".join(parts)
            elif self.cfg["provider"] == "ollama":
                if response.get("done") is not True or response.get("done_reason") == "length":
                    raise ModelError("Ollama response incomplete; adjust output budget")
                content = response["message"]["content"]
            else:
                choice = response["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ModelError("Model response did not finish normally; no partial labels accepted")
                content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ModelError("Model returned no textual annotation")
            return content
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("Unrecognized provider response structure") from None
