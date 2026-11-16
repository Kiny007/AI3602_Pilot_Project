"""Local key entry without putting secrets in configuration snapshots or caches."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import sys
import warnings

from .common import ROOT, PilotError


def _validate_key(value: str) -> str:
    key = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise PilotError("API key contains a control character; enter it on one line")
    return key


def read_api_key(variable: str, env_file: Path | None = None) -> str | None:
    """Environment > project .env > hidden terminal input. Never persist prompted keys."""
    if not variable:
        return None
    value = os.environ.get(variable, "").strip()
    if value:
        return _validate_key(value)
    path = env_file if env_file is not None else ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, separator, value = stripped.partition("=")
            if separator and name.strip() == variable:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if value.strip():
                    return _validate_key(value)
    if sys.stdin.isatty():
        try:
            # Do not fall back to visible input if the terminal cannot hide characters.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(f"请输入 {variable}（输入不显示，仅本次运行使用）: ")
        except (EOFError, getpass.GetPassWarning):
            value = ""
        if value.strip():
            return _validate_key(value)
    raise PilotError(f"请在项目根目录 .env 中填写 {variable}=你的APIKey，或设置同名环境变量。")
