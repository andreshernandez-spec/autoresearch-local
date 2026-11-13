"""The contract: what the agent may not change.

PROGRAM.md, arl.toml and the evaluator files are hashed into frozen.lock, and
every run checks the hashes first. The lock carries two digests:

- digest: every contract file. Any mismatch blocks runs.
- eval_digest: the evaluator files plus the [run], [eval] and [[metric]]
  tables. Scores are compared only within one eval_digest, so raising the
  budget does not ask for a new baseline, and touching the evaluator does.

The first freeze can be done by anyone. Once an experiment exists, a new freeze
needs a person at a terminal.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ledger import CONTRACT_FILES, Ledger
from .util import ArlError, canonical, now, read_json, require_filled, sha256_file, sha256_text, write_json

_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ipynb_checkpoints"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


def _leftover(name: str) -> bool:
    """Swap, backup and lock files an editor leaves beside the file it has open."""
    return (
        name.endswith("~")
        or name.startswith(".#")
        or (name.startswith("#") and name.endswith("#"))
        or bool(re.fullmatch(r"\..*\.sw[a-p]", name))
        or name.endswith(".arltmp")
        or name == ".DS_Store"
    )


def _files_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    out = []
    for p in sorted(path.rglob("*")):
        if p.is_file() and not (set(p.parts) & _SKIP_DIRS) and p.suffix not in _SKIP_SUFFIXES and not _leftover(p.name):
            out.append(p)
    return out


def eval_files(ledger: Ledger) -> list[Path]:
    files: list[Path] = []
    for entry in ledger.cfg.frozen:
        path = (ledger.home / entry).resolve()
        if ledger.home not in path.parents:
            raise ArlError(f"arl.toml: frozen path '{entry}' is outside the project")
        if not path.exists():
            raise ArlError(f"arl.toml: frozen path '{entry}' does not exist")
        files += _files_under(path)
    if not files:
        raise ArlError("arl.toml: [eval] frozen names no files")
    return files


def current(ledger: Ledger) -> dict[str, Any]:
    """Hashes of the contract as it is on disk now."""
    cfg = ledger.cfg
    evals = {ledger.rel(p): sha256_file(p) for p in eval_files(ledger)}
    files = {ledger.rel(ledger.dir / f): sha256_file(ledger.dir / f) for f in CONTRACT_FILES}
    files.update(evals)
    raw = cfg.raw
    scoring = {"files": evals, "run": raw.get("run"), "eval": raw.get("eval"), "metric": raw.get("metric")}
    return {
        "files": dict(sorted(files.items())),
        "digest": sha256_text(canonical(files)),
        "eval_digest": sha256_text(canonical(scoring)),
    }


def load(ledger: Ledger) -> dict[str, Any] | None:
    return read_json(ledger.lock_path) if ledger.lock_path.exists() else None


def problems(ledger: Ledger) -> list[str]:
    """Empty when the files on disk are the frozen ones."""
    lock = load(ledger)
    if lock is None:
        return ["the contract is not frozen yet (run 'arl freeze')"]
    try:
        now_files = current(ledger)["files"]
    except ArlError as e:
        return [str(e)]
    out = []
    for path, digest in lock["files"].items():
        if path not in now_files:
            out.append(f"missing: {path}")
        elif now_files[path] != digest:
            out.append(f"changed: {path}")
    out += [f"new file: {path}" for path in now_files if path not in lock["files"]]
    return out


def require(ledger: Ledger) -> dict[str, Any]:
    bad = problems(ledger)
    if bad:
        raise ArlError(
            "The frozen contract does not match the files on disk:\n  "
            + "\n  ".join(bad)
            + "\nThe contract is not the agent's to change. Put the files back, or ask the person who"
            + "\nowns the program to change them and run 'arl freeze' in a terminal."
        )
    lock = load(ledger)
    assert lock is not None
    return lock


def freeze(ledger: Ledger, reason: str, interactive: bool) -> dict[str, Any]:
    """Hash the contract and write frozen.lock. The caller commits."""
    ledger.check_home()
    require_filled(ledger.program_path, "PROGRAM.md")
    old = load(ledger)
    if old is not None and ledger.experiments() and not interactive:
        raise ArlError(
            "Experiments already depend on this contract, so a new freeze needs a person:\n"
            "run 'arl freeze --reason \"...\"' yourself in a terminal."
        )
    state = current(ledger)
    if old is not None and old["digest"] == state["digest"]:
        raise ArlError(f"nothing changed since contract v{old['version']}")
    history = list(old.get("history", [])) if old else []
    if old:
        history.append({k: old[k] for k in ("version", "digest", "eval_digest", "frozen_at", "reason", "interactive")})
    lock = {
        "version": (old["version"] + 1) if old else 1,
        "frozen_at": now(),
        "interactive": interactive,
        "reason": reason,
        **state,
        "history": history,
    }
    write_json(ledger.lock_path, lock)
    return lock


def commit_paths(ledger: Ledger) -> list[str]:
    """What a freeze commits: all of research/ and any evaluator file kept outside it."""
    outside = [p.rstrip("/") for p in ledger.cfg.frozen if not p.startswith(f"{ledger.name}/")]
    return [ledger.name, *outside]
