from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tomllib
from urllib.parse import urlparse

from .common import PilotError

DEFAULTS = {
    "source": {"dataset": "Webis-TLDR-17", "version": "zenodo-1043504-v1",
               "url": "https://zenodo.org/records/1043504", "kind": "public_dataset",
               "text_field": "body", "id_field": "id", "subreddits": [],
               "start": 0, "limit": 5000, "synthetic": False},
    "model": {"provider": "deepseek", "endpoint": "https://api.deepseek.com/chat/completions",
              "name": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY", "temperature": 0.0,
              "max_tokens": 2048, "json_mode": True, "timeout": 90,
              "retries": 2, "max_calls": 1500},
    "pipeline": {"review": True, "repair_attempts": 1, "chunk_chars": 8000,
                 "overlap_chars": 600, "max_chunks": 20, "rules": True,
                 "target_count": 0, "min_classes": 3, "min_per_class": 10},
}


def load_config(path: Path) -> dict:
    if path.suffix == ".json":
        from .common import read_json
        supplied = read_json(path)
    else:
        with path.open("rb") as f:
            supplied = tomllib.load(f)
    cfg = deepcopy(DEFAULTS)
    for section, values in supplied.items():
        if section not in cfg or not isinstance(values, dict):
            raise PilotError(f"Unknown configuration section: {section}")
        unknown = set(values) - set(cfg[section])
        if unknown:
            raise PilotError(f"Unknown keys in {section}: {sorted(unknown)}")
        cfg[section].update(values)
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    for section, defaults in DEFAULTS.items():
        for key, default in defaults.items():
            value = cfg[section][key]
            if type(default) is float:
                valid = type(value) in (int, float)
            else:
                valid = type(value) is type(default)
            if not valid:
                raise PilotError(f"Invalid type for {section}.{key}")
    s, m, p = cfg["source"], cfg["model"], cfg["pipeline"]
    if s["start"] < 0 or s["limit"] < 1 or not all(isinstance(x, str) for x in s["subreddits"]):
        raise PilotError("source.start must be >=0, limit >=1, subreddits a string list")
    if not s["dataset"] or not s["version"] or urlparse(s["url"]).scheme not in ("https", "http"):
        raise PilotError("Source requires dataset, fixed version and HTTP(S) URL")
    if s["kind"] not in ("public_dataset", "self_collected"):
        raise PilotError("source.kind must be public_dataset or self_collected")
    if s["text_field"] in ("summary", "normalizedBody", "content"):
        raise PilotError("Use the original body/selftext field, not a summary or normalized field")
    if m["provider"] not in ("deepseek", "chat_completions", "ollama"):
        raise PilotError("model.provider must be deepseek, chat_completions or ollama")
    url = urlparse(m["endpoint"])
    if url.username or url.password or url.query or url.fragment:
        raise PilotError("Model endpoint must not embed credentials, queries or fragments")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
        raise PilotError("Use HTTPS for remote endpoints; HTTP is allowed only on loopback")
    if not 0 <= m["temperature"] <= 2 or not 0 < m["timeout"] <= 300:
        raise PilotError("Invalid model temperature or timeout")
    if not 0 <= m["retries"] <= 5 or m["max_calls"] < 1 or m["max_tokens"] < 64:
        raise PilotError("Invalid retry, token or call budget")
    if not 0 <= p["repair_attempts"] <= 3 or not 0 <= p["overlap_chars"] < p["chunk_chars"]:
        raise PilotError("Invalid repair or overlap settings")
    if p["chunk_chars"] < 100 or p["max_chunks"] < 1 or p["target_count"] < 0:
        raise PilotError("Invalid chunk or target settings")
    if not 1 <= p["min_classes"] <= 12 or p["min_per_class"] < 1:
        raise PilotError("Invalid class coverage settings")
