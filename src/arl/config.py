"""Read and check arl.toml, the machine-readable half of the contract.

Every value is checked here, when the file is loaded, so a wrong type or an
impossible number is named before the contract is frozen and not in the middle
of a run.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import ArlError, read_text

DECISIONS = ["continue", "adjust", "branch", "pivot", "stop", "escalate"]


@dataclass
class Metric:
    name: str
    goal: str  # "max" or "min"
    role: str  # "primary", "constraint" or "info"
    min_delta: float = 0.0
    target: float | None = None
    limit: float | None = None
    max_regression: float | None = None

    def gain(self, value: float, reference: float) -> float:
        """How much better value is than reference, positive is better."""
        return value - reference if self.goal == "max" else reference - value

    def meets(self, value: float, bound: float) -> bool:
        return value >= bound if self.goal == "max" else value <= bound


@dataclass
class Config:
    raw: dict[str, Any]
    name: str
    worktrees: str
    home_branch: str | None
    commit_prefix: str
    git_email: str | None
    run_command: str
    run_timeout: float
    keep: list[str]
    keep_max_kb: int
    max_attempts: int
    checkpoint_every: float
    pause_grace: float
    eval_command: str
    eval_timeout: float
    frozen: list[str]
    splits: list[str]
    metrics: list[Metric]
    max_experiments: int | None
    max_run_hours: float | None
    intro_every: int
    intro_stagnation: int
    intro_max_days: float | None
    agent_may: list[str]
    max_loadavg: float | None
    min_free_ram_gb: float | None
    min_free_gpu_mb: float | None
    lock: str | None
    max_run_rss_gb: float | None
    stop_below_free_ram_gb: float | None
    env_prefix: str | None
    env_packages: list[str]
    env_command: str | None
    data_files: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Metric:
        return next(m for m in self.metrics if m.role == "primary")

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ArlError(f"arl.toml: [{key}] must be a table")
    return value


def _text(table: dict[str, Any], section: str, key: str, default: str | None = None, required: bool = False) -> Any:
    value = table.get(key, default)
    if value in ("", None):
        if required:
            raise ArlError(f"arl.toml: [{section}] needs '{key}'")
        return None
    if not isinstance(value, str):
        raise ArlError(f"arl.toml: [{section}] {key} must be text in quotes, not {value!r}")
    return value


def _number(table: dict[str, Any], section: str, key: str, default: Any = None, least: float | None = None, whole: bool = False) -> Any:
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArlError(f"arl.toml: [{section}] {key} must be a number, not {value!r}")
    if whole and not isinstance(value, int):
        raise ArlError(f"arl.toml: [{section}] {key} must be a whole number, not {value!r}")
    if least is not None and value < least:
        raise ArlError(f"arl.toml: [{section}] {key} must be at least {least:g}, not {value!r}")
    return value


def _texts(table: dict[str, Any], section: str, key: str, default: list[str]) -> list[str]:
    value = table.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ArlError(f"arl.toml: [{section}] {key} must be a list of texts in quotes, not {value!r}")
    return list(value)


def load(path: Path) -> Config:
    if not path.exists():
        raise ArlError(f"no {path}. Run 'arl init' in the project first.")
    try:
        raw = tomllib.loads(read_text(path))
    except tomllib.TOMLDecodeError as e:
        raise ArlError(f"{path}: {e}") from e

    project, run, ev = _table(raw, "project"), _table(raw, "run"), _table(raw, "eval")
    budget, machine, env, data = _table(raw, "budget"), _table(raw, "machine"), _table(raw, "env"), _table(raw, "data")
    if "introspection" in raw and "review" in raw:
        raise ArlError("arl.toml: [review] is the old name of [introspection]. Keep one of them.")
    section = "introspection" if "introspection" in raw or "review" not in raw else "review"
    intro = _table(raw, section)

    entries = raw.get("metric", [])
    if not isinstance(entries, list) or not all(isinstance(m, dict) for m in entries):
        raise ArlError("arl.toml: every metric is its own [[metric]] table")
    metrics: list[Metric] = []
    for m in entries:
        name = _text(m, "metric", "name", required=True)
        where = f"metric {name}"
        goal, role = m.get("goal", "max"), m.get("role", "info")
        if goal not in ("max", "min"):
            raise ArlError(f"arl.toml: {where}: goal must be 'max' or 'min'")
        if role not in ("primary", "constraint", "info"):
            raise ArlError(f"arl.toml: {where}: role must be primary, constraint or info")
        limit, regression = _number(m, where, "limit"), _number(m, where, "max_regression", least=0)
        if role == "constraint" and limit is None and regression is None:
            raise ArlError(f"arl.toml: constraint {name} needs 'limit' or 'max_regression'")
        metrics.append(
            Metric(
                name=name,
                goal=goal,
                role=role,
                min_delta=float(_number(m, where, "min_delta", 0.0, least=0)),
                target=_number(m, where, "target"),
                limit=limit,
                max_regression=regression,
            )
        )
    if sum(m.role == "primary" for m in metrics) != 1:
        raise ArlError("arl.toml: exactly one [[metric]] must have role = \"primary\"")
    if len({m.name for m in metrics}) != len(metrics):
        raise ArlError("arl.toml: metric names must be unique")

    agent_may = _texts(intro, section, "agent_may", ["continue", "adjust", "branch", "pivot"])
    unknown = [d for d in agent_may if d not in DECISIONS]
    if unknown:
        raise ArlError(f"arl.toml: [{section}] agent_may has unknown decisions {unknown}")
    splits = _texts(ev, "eval", "splits", ["dev"])
    if not splits:
        raise ArlError("arl.toml: [eval] splits must name at least one split")
    frozen = _texts(ev, "eval", "frozen", [])
    if not frozen:
        raise ArlError("arl.toml: [eval] needs 'frozen', the files and folders of the evaluator")
    packages = _texts(env, "env", "packages", [])
    odd = [p for p in packages if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", p)]
    if odd:
        raise ArlError(f"arl.toml: [env] packages holds the names of installed packages, not {odd}")
    if len({p.lower() for p in packages}) != len(packages):
        raise ArlError("arl.toml: [env] packages names a package twice")

    return Config(
        raw=raw,
        name=_text(project, "project", "name", required=True),
        worktrees=_text(project, "project", "worktrees", "../.worktrees"),
        home_branch=_text(project, "project", "home_branch"),
        commit_prefix=_text(project, "project", "commit_prefix", "research"),
        git_email=_text(project, "project", "git_email"),
        run_command=_text(run, "run", "command", required=True),
        run_timeout=float(_number(run, "run", "timeout_s", 3600, least=1)),
        keep=_texts(run, "run", "keep", ["*.json", "*.txt", "*.csv", "*.md"]),
        keep_max_kb=int(_number(run, "run", "keep_max_kb", 256, least=0, whole=True)),
        max_attempts=int(_number(run, "run", "max_attempts", 3, least=1, whole=True)),
        checkpoint_every=float(_number(run, "run", "checkpoint_every_s", 600, least=0)),
        pause_grace=float(_number(run, "run", "pause_grace_s", 60, least=0)),
        eval_command=_text(ev, "eval", "command", required=True),
        eval_timeout=float(_number(ev, "eval", "timeout_s", 1800, least=1)),
        frozen=frozen,
        splits=splits,
        metrics=metrics,
        max_experiments=_number(budget, "budget", "max_experiments", least=1, whole=True),
        max_run_hours=_number(budget, "budget", "max_run_hours", least=0),
        intro_every=int(_number(intro, section, "every", 8, least=1, whole=True)),
        intro_stagnation=int(_number(intro, section, "stagnation", 5, least=1, whole=True)),
        intro_max_days=_number(intro, section, "max_days", least=0),
        agent_may=agent_may,
        max_loadavg=_number(machine, "machine", "max_loadavg", least=0),
        min_free_ram_gb=_number(machine, "machine", "min_free_ram_gb", least=0),
        min_free_gpu_mb=_number(machine, "machine", "min_free_gpu_mb", least=0),
        lock=_text(machine, "machine", "lock"),
        max_run_rss_gb=_number(machine, "machine", "max_run_rss_gb", least=0),
        stop_below_free_ram_gb=_number(machine, "machine", "stop_below_free_ram_gb", least=0),
        env_prefix=_text(env, "env", "prefix"),
        env_packages=packages,
        env_command=_text(env, "env", "command"),
        data_files=_texts(data, "data", "files", []),
    )
