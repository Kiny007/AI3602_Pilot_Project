from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

TAGS = ("SCH", "OCC", "MAR", "LOC", "INC", "HEA", "FAM", "APP", "SEX", "POB", "AGE", "NAM")
ROOT = Path(__file__).resolve().parents[2]


class PilotError(Exception):
    """An actionable input, provider or integrity error."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Atomic replacement, including on Windows; never leave a partial cache entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def jsonl(path: Path):
    with path.open(encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise PilotError(f"Invalid JSON at {path}:{line_no}: {e.msg}") from e
            if not isinstance(row, dict):
                raise PilotError(f"Expected object at {path}:{line_no}")
            yield row


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(canonical(row) + "\n")


def new_directory(path: Path) -> None:
    if path.exists():
        raise PilotError(f"Output already exists: {path}. Choose a new directory; existing work is preserved.")
    path.mkdir(parents=True)


def safe_child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PilotError(f"Path escapes source root: {relative}")
    return path
