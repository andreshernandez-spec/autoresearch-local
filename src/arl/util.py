"""Small helpers shared by every module."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any


class ArlError(Exception):
    """A gate refused or the input was wrong. The message is shown as is."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# Exit codes of `arl exp run` beyond the plain 1.
EXIT_CRASH = 2  # the run or the evaluator failed
EXIT_BUSY = 75  # the machine is busy or another run holds the lock


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(obj: Any) -> str:
    """JSON with a fixed key order, for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def slugify(text: str, max_len: int = 36) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "x"


def write_text(path: Path, text: str) -> None:
    """Write through a temporary file, so a reader never sees half a file."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.arltmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def write_json(path: Path, obj: Any) -> None:
    write_text(path, json.dumps(obj, indent=2, default=str) + "\n")


# --- what a person or an agent types ---------------------------------------------

_IDS = {"E": (r"E\d{4}", "an experiment id like E0007"), "H": (r"H\d{3}", "a hypothesis id like H003"),
        "R": (r"R\d{3}", "a review id like R002"), "I": (r"I\d{3}", "an idea id like I012")}


def check_id(kind: str, value: str) -> str:
    pattern, what = _IDS[kind]
    if not re.fullmatch(pattern, value or ""):
        raise ArlError(f"'{value}' is not {what}")
    return value


def check_text(value: str, what: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ArlError(f"{what} has characters that are not valid UTF-8") from e
    return value


def one_line(value: str, what: str) -> str:
    """A title: valid text, on one line, not empty."""
    value = " ".join(check_text(value, what).split())
    if not value:
        raise ArlError(f"{what} is empty")
    return value


# --- placeholders ----------------------------------------------------------------
# Templates mark what the agent must write with a quoted line "> FILL: ...".
# A file that still has one is not done.

_FILL = re.compile(r"^\s*>?\s*FILL:.*$", re.M)


def unfilled(text: str) -> list[str]:
    return [m.group(0).strip() for m in _FILL.finditer(text)]


def require_filled(path: Path, what: str) -> None:
    if not path.exists():
        raise ArlError(f"{what} is missing: {path}")
    left = unfilled(path.read_text())
    if left:
        lines = "\n  ".join(left[:6])
        raise ArlError(f"{what} is not written yet ({path}). Still to fill:\n  {lines}")


# --- front matter ----------------------------------------------------------------
# Markdown files carry a small TOML block between +++ lines. arl owns the block,
# the agent owns the text below it.


def split_front(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("+++\n"):
        return {}, text
    end = text.find("\n+++\n", 4)
    if end < 0:
        raise ArlError("front matter is not closed with +++")
    return tomllib.loads(text[4:end]), text[end + 5 :].lstrip("\n")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # A JSON string is a valid TOML basic string once DEL is escaped.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def join_front(meta: dict[str, Any], body: str) -> str:
    lines = [f"{key} = {_toml_value(value)}" for key, value in meta.items() if value is not None]
    return "+++\n" + "\n".join(lines) + "\n+++\n\n" + body.lstrip("\n")


def read_front(path: Path) -> tuple[dict[str, Any], str]:
    return split_front(Path(path).read_text())


def write_front(path: Path, meta: dict[str, Any], body: str) -> None:
    write_text(path, join_front(meta, body))


def fmt(value: Any) -> str:
    """A metric value for a table."""
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)
