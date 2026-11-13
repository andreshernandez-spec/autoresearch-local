"""Read and check arl.toml, the machine-readable half of the contract."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import ArlError

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
    eval_command: str
    eval_timeout: float
    frozen: list[str]
    splits: list[str]
    metrics: list[Metric]
    max_experiments: int | None
    max_run_hours: float | None
    review_every: int
    review_stagnation: int
    review_max_days: float | None
    agent_may: list[str]
    max_loadavg: float | None
    min_free_ram_gb: float | None
    min_free_gpu_mb: float | None
    lock: str | None
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


def _need(table: dict[str, Any], section: str, key: str) -> Any:
    if key not in table or table[key] in ("", None):
        raise ArlError(f"arl.toml: [{section}] needs '{key}'")
    return table[key]


def load(path: Path) -> Config:
    if not path.exists():
        raise ArlError(f"no {path}. Run 'arl init' in the project first.")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ArlError(f"{path}: {e}") from e

    project, run, ev = _table(raw, "project"), _table(raw, "run"), _table(raw, "eval")
    budget, review = _table(raw, "budget"), _table(raw, "review")
    machine, env, data = _table(raw, "machine"), _table(raw, "env"), _table(raw, "data")

    metrics: list[Metric] = []
    for m in raw.get("metric", []):
        name = _need(m, "metric", "name")
        goal, role = m.get("goal", "max"), m.get("role", "info")
        if goal not in ("max", "min"):
            raise ArlError(f"arl.toml: metric {name}: goal must be 'max' or 'min'")
        if role not in ("primary", "constraint", "info"):
            raise ArlError(f"arl.toml: metric {name}: role must be primary, constraint or info")
        if role == "constraint" and m.get("limit") is None and m.get("max_regression") is None:
            raise ArlError(f"arl.toml: constraint {name} needs 'limit' or 'max_regression'")
        metrics.append(
            Metric(
                name=name,
                goal=goal,
                role=role,
                min_delta=float(m.get("min_delta", 0.0)),
                target=m.get("target"),
                limit=m.get("limit"),
                max_regression=m.get("max_regression"),
            )
        )
    if sum(m.role == "primary" for m in metrics) != 1:
        raise ArlError("arl.toml: exactly one [[metric]] must have role = \"primary\"")
    if len({m.name for m in metrics}) != len(metrics):
        raise ArlError("arl.toml: metric names must be unique")

    agent_may = list(review.get("agent_may", ["continue", "adjust", "branch", "pivot"]))
    unknown = [d for d in agent_may if d not in DECISIONS]
    if unknown:
        raise ArlError(f"arl.toml: [review] agent_may has unknown decisions {unknown}")

    splits = list(ev.get("splits", ["dev"]))
    if not splits:
        raise ArlError("arl.toml: [eval] splits must name at least one split")

    return Config(
        raw=raw,
        name=str(_need(project, "project", "name")),
        worktrees=str(project.get("worktrees", "../.worktrees")),
        home_branch=project.get("home_branch"),
        commit_prefix=str(project.get("commit_prefix", "research")),
        git_email=project.get("git_email"),
        run_command=str(_need(run, "run", "command")),
        run_timeout=float(run.get("timeout_s", 3600)),
        keep=list(run.get("keep", ["*.json", "*.txt", "*.csv", "*.md"])),
        keep_max_kb=int(run.get("keep_max_kb", 256)),
        max_attempts=int(run.get("max_attempts", 3)),
        eval_command=str(_need(ev, "eval", "command")),
        eval_timeout=float(ev.get("timeout_s", 1800)),
        frozen=list(_need(ev, "eval", "frozen")),
        splits=splits,
        metrics=metrics,
        max_experiments=budget.get("max_experiments"),
        max_run_hours=budget.get("max_run_hours"),
        review_every=int(review.get("every", 8)),
        review_stagnation=int(review.get("stagnation", 5)),
        review_max_days=review.get("max_days"),
        agent_may=agent_may,
        max_loadavg=machine.get("max_loadavg"),
        min_free_ram_gb=machine.get("min_free_ram_gb"),
        min_free_gpu_mb=machine.get("min_free_gpu_mb"),
        lock=machine.get("lock"),
        env_command=env.get("command"),
        data_files=list(data.get("files", [])),
    )
