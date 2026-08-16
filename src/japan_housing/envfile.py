"""Minimal dotenv reader that treats secret files strictly as data."""

from __future__ import annotations

import os
from pathlib import Path


def env_value(name: str, env_file: Path | None = Path(".env")) -> str:
    value = os.environ.get(name, "").strip()
    if value or env_file is None:
        return value
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ValueError("could not read the selected env file") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, candidate = line.partition("=")
        if separator and key.strip() == name:
            value = candidate.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return ""
