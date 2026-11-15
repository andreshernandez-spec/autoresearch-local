"""What the ledger keeps about an experiment, a track, a hypothesis and an
introspection, as types.

A file of the ledger is read into one of these classes and written back from it.
A field misspelt in the code then fails where it is written, and a file that holds
something this arl does not know stops the command that reads it, before it
changes anything. The names of the fields are the keys in the files. A field that
is None is not written.
"""

from __future__ import annotations

import dataclasses
import functools
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from .util import ArlError

R = TypeVar("R")


# --- an experiment: experiments/E0007-slug/record.json ----------------------------


@dataclass(slots=True, kw_only=True)
class Part:
    """One stretch of a run: from a start to its end, a pause or an interruption."""

    started: str
    env: dict[str, Any] | None = None  # the environment this part ran in
    ended: str | None = None
    ended_by: str | None = None  # finished, failed, paused, interrupted
    seconds: float | None = None  # how long the job ran in this part
    note: str | None = None


@dataclass(slots=True, kw_only=True)
class Attempt:
    """One try at running an experiment. A failed one counts, and the next starts from nothing."""

    n: int
    started: str
    code_commit: str | None = None  # not there when the attempt never got as far as starting
    segments: list[Part] = field(default_factory=list, metadata={"omit_empty": True})
    exit_code: int | None = None
    last_checkpoint: str | None = None  # when the job last saved its state
    warning: str | None = None
    run_seconds: float | None = None  # over all its parts
    run_done: bool | None = None  # the job itself finished, so only the evaluation can be left
    eval_seconds: float | None = None
    paused: str | None = None  # why it stopped early, while it can still be continued
    ended: str | None = None
    failed: str | None = None  # why it failed, once it has

    def ran(self, parts: list[Part] | None = None) -> float:
        """Seconds the job ran, over all the parts or over the ones given."""
        return sum(p.seconds or 0 for p in (self.segments if parts is None else parts))


@dataclass(slots=True, kw_only=True)
class Stamp:
    """The contract an experiment ran under."""

    version: int
    digest: str  # every contract file
    eval_digest: str  # what a score depends on

    @classmethod
    def of(cls, lock: dict[str, Any]) -> "Stamp":
        return cls(version=lock["version"], digest=lock["digest"], eval_digest=lock["eval_digest"])


@dataclass(slots=True, kw_only=True)
class Conditions:
    """What a run was given."""

    command: str
    split: str
    worktree: str
    data: list[dict[str, Any]] = field(default_factory=list)  # each data file: path, bytes, sha256
    data_digest: str | None = None  # None in a record from before the data files were written down
    env: dict[str, Any] | None = None  # the environment of its first part


@dataclass(slots=True, kw_only=True)
class Design:
    """The design as it was when the first run started. It is locked from then on."""

    design_sha256: str
    params_sha256: str
    locked_at: str


@dataclass(slots=True, kw_only=True)
class Comparison:
    """A result against the reference of its track."""

    metric: str
    value: float
    gate_failures: list[str]
    target_met: bool
    reference: str | None = None  # None: this result is the reference
    reference_value: float | None = None
    gain: float | None = None
    min_delta: float | None = None
    outcome: str  # reference, repeat, improve, neutral, worse, gate_fail


@dataclass(slots=True, kw_only=True)
class HeadMove:
    """Where the head of the track stood before a verdict, and after it."""

    before: str
    after: str | None = None  # None while the verdict is still being applied


@dataclass(slots=True, kw_only=True)
class Verdict:
    decision: str  # keep, discard, finding, crash, abandoned
    reason: str
    at: str
    hypotheses: dict[str, str] | None = None  # what the run did to each hypothesis
    track_head: HeadMove | None = None
    simpler: bool | None = None  # a neutral result kept because it simplifies


@dataclass(slots=True, kw_only=True)
class EnvChange:
    """How the environment of a rerun differs from the one the experiment first ran in."""

    count: int
    first: list[str]  # the first twenty differences


@dataclass(slots=True, kw_only=True)
class Rerun:
    """A recorded experiment run again, on any split. Never used for a verdict."""

    n: int
    at: str
    split: str
    eval_digest: str | None = None  # the evaluator it was scored under
    data_digest: str | None = None  # the data files it read
    env: dict[str, Any] | None = None
    data: list[dict[str, Any]] | None = None  # the data files, when they are not the ones the experiment read
    env_changed: EnvChange | None = None
    run_seconds: float | None = None
    eval_seconds: float | None = None
    interrupted: bool | None = None  # stopped from outside: not a failure of the experiment
    failed: str | None = None
    metrics: dict[str, float] | None = None
    train_metrics: dict[str, float] | None = None
    difference: float | None = None  # from the recorded primary metric
    reproduced: bool | None = None  # only on the first split, the same evaluator and the same data
    meets_target: bool | None = None


@dataclass(slots=True, kw_only=True)
class Going:
    """A rerun that is going. It becomes a Rerun when it ends, or when a later arl finds it left behind."""

    n: int
    split: str
    started: str
    pid: int | None = None
    pid_start: int | None = None
    tree: str | None = None  # its throwaway worktree
    eval_digest: str | None = None
    data_digest: str | None = None
    child: dict[str, Any] | None = None  # what it takes to find its job again


@dataclass(slots=True, kw_only=True)
class Record:
    """An experiment: what was planned, what ran, what came of it, and the verdict."""

    id: str
    title: str
    type: str  # baseline, tune, feature, diagnose
    track: str
    hypotheses: list[str]
    status: str  # draft, running, paused, evaluated, judging, judged, crashed, abandoned
    created: str
    base_commit: str  # the head of the track when it was drafted
    branch: str
    command: str = ""  # diagnose only: what runs instead of the command in arl.toml
    attempts: list[Attempt] = field(default_factory=list)
    duplicate_of: str | None = None
    duplicate_reason: str | None = None
    contract: Stamp | None = None
    conditions: Conditions | None = None
    pid: int | None = None  # the arl that is running it, while it is
    pid_start: int | None = None
    code_commit: str | None = None  # what ran
    dedup: str | None = None  # one hash for code, params, data, command and evaluator
    design: Design | None = None
    child: dict[str, Any] | None = None  # what it takes to find its job again
    artifacts: list[dict[str, Any]] | None = None  # what the run left: path, bytes, sha256
    train_metrics: dict[str, float] | None = None  # what the run says about itself: kept, never compared
    metrics: dict[str, float] | None = None  # the evaluator's numbers
    comparison: Comparison | None = None
    kept: list[str] | None = None  # small result files copied into the ledger
    verdict: Verdict | None = None
    rerun: Going | None = None
    reruns: list[Rerun] = field(default_factory=list, metadata={"omit_empty": True})

    @property
    def outcome(self) -> str | None:
        return self.comparison.outcome if self.comparison else None

    @property
    def decision(self) -> str | None:
        return self.verdict.decision if self.verdict else None

    @property
    def eval_digest(self) -> str | None:
        """The evaluator its result was scored under."""
        return self.contract.eval_digest if self.contract else None

    @property
    def data_digest(self) -> str | None:
        """The data files it read. None before it ran, and in a record from before they were written down."""
        return self.conditions.data_digest if self.conditions else None

    @property
    def env(self) -> dict[str, Any] | None:
        return self.conditions.env if self.conditions else None

    def forget_job(self) -> None:
        """No process is running for it any more."""
        self.pid = self.pid_start = self.child = None


# --- the block between the +++ lines of a track, a hypothesis, an introspection ----


@dataclass(slots=True, kw_only=True)
class Track:
    name: str
    status: str  # active, parked, closed
    branch: str
    created: str | None = None
    from_ref: str | None = None
    base_commit: str | None = None
    head_commit: str
    head_experiment: str = ""  # the kept experiment that measured the head: the reference
    opened_by: str | None = None  # setup, an introspection, or a person
    params_sha256: str | None = None


@dataclass(slots=True, kw_only=True)
class Hypothesis:
    id: str
    title: str
    status: str  # open, supported, refuted, inconclusive, dropped
    created: str | None = None
    updated: str | None = None
    source: str = ""


@dataclass(slots=True, kw_only=True)
class Introspection:
    id: str
    status: str  # open, waiting (for a person), closed
    trigger: str | None = None
    opened: str | None = None
    window: list[str] = field(default_factory=list)  # the experiments judged since the one before
    unconfirmed: list[str] = field(default_factory=list)  # results that met the target and did not hold
    activity_upto: int | None = None  # how far into activity.jsonl its facts go
    knowledge_sha256: str | None = None  # KNOWLEDGE.md when it was opened
    lessons_general: int | None = None  # lessons on file when it was opened
    lessons_project: int | None = None
    decision: str | None = None
    note: str | None = None
    lessons_added: int | None = None
    knowledge_changed: bool | None = None
    no_lessons: str | None = None
    knowledge_unchanged: str | None = None
    closed: str | None = None
    decided_by: str | None = None  # agent or person
    proposed: str | None = None
    proposed_decision: str | None = None  # what the agent proposed, when a person decided otherwise
    reloaded: str | None = None  # when what it changed was read again


# --- what a status promises -------------------------------------------------------

STATUSES = ("draft", "running", "paused", "evaluated", "judging", "judged", "crashed", "abandoned")


def contradiction(rec: Record) -> str:
    """Why the fields of a record do not fit its status, or "" when they do.

    Asked when a record is read, so a command can rely on it: an experiment that
    has run has its contract and its conditions, one with a result has its design,
    one with a verdict status has the verdict, a scored result has its comparison.
    """
    if rec.status not in STATUSES:
        return f"its status is '{rec.status}', not one of {', '.join(STATUSES)}"
    if rec.status in ("judging", "judged", "abandoned") and rec.verdict is None:
        return f"it is {rec.status} and has no verdict"
    if rec.status in ("paused", "evaluated", "crashed", "judging", "judged") and not (rec.code_commit and rec.attempts):
        return f"it is {rec.status} and has no attempt or no code_commit"
    if rec.code_commit is not None:
        missing = [name for name in ("contract", "conditions") if getattr(rec, name) is None]
        if missing:
            return f"it has run and has no {', '.join(missing)}"
    if (rec.status == "evaluated" or rec.metrics is not None) and rec.design is None:
        return "it has a result and no design"
    if rec.metrics is not None and rec.comparison is None:
        return "it has metrics and no comparison"
    return ""


def need(value: R | None) -> R:
    """A field that is there by now, because the record's status promises it and that
    was checked when the record was read. So this fails only when a command asks for
    what its own checks did not establish: a mistake in arl, not in the ledger."""
    assert value is not None, "a record lacks a field that its status promises"
    return value


# --- reading and writing ----------------------------------------------------------

_SAYS = {str: "text", int: "a whole number", float: "a number", bool: "true or false", list: "a list", dict: "a table"}


class _Wrong(Exception):
    """Something in a file that cannot be read. Where it sits is collected on the way
    out, so naming the place costs nothing while everything is in order."""

    def __init__(self, say: Callable[[str], str]):
        super().__init__()
        self.say, self.path = say, []  # the path is gathered from the inside out

    def text(self) -> str:
        where = ""
        for step in reversed(self.path):
            where += step if step.startswith("[") or not where else f".{step}"
        return self.say(where)


def _wrong_kind(kind: Any, value: Any) -> _Wrong:
    return _Wrong(lambda where: f"{where or 'it'} should be {_SAYS.get(kind, getattr(kind, '__name__', str(kind)))}, not {value!r}")


def _named(names: list[str], where: str) -> str:
    return ", ".join(f"{where}.{name}" if where else name for name in names)


@functools.lru_cache(maxsize=None)
def _reader(tp: Any) -> Callable[[Any], Any]:
    """How a value of a declared type is read from a file. Worked out once per type."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):  # "X | None"
        inner = _reader(next(a for a in typing.get_args(tp) if a is not type(None)))
        return lambda value: None if value is None else inner(value)
    kind = origin or tp
    if kind is Any:
        return lambda value: value
    if dataclasses.is_dataclass(kind):
        return lambda value: _load(typing.cast(type, kind), value)
    if kind is list:
        item = _reader(typing.get_args(tp)[0])

        def read_list(value: Any) -> list[Any]:
            if not isinstance(value, list):
                raise _wrong_kind(list, value)
            out = []
            for i, v in enumerate(value):
                try:
                    out.append(item(v))
                except _Wrong as e:
                    e.path.append(f"[{i}]")
                    raise
            return out

        return read_list
    accepted: Any = (int, float) if kind is float else kind
    number = kind in (int, float)  # a whole number is a number; true and false are neither

    def read_plain(value: Any) -> Any:
        if not isinstance(value, accepted) or (number and isinstance(value, bool)):
            raise _wrong_kind(kind, value)
        return value

    return read_plain


@functools.lru_cache(maxsize=None)
def _plan(cls: type) -> tuple[tuple[str, Callable[[Any], Any], bool, bool], ...]:
    """Per field of a record class: its name, how it is read, whether a file must have it,
    and whether it is left out of the file when empty."""
    hints = typing.get_type_hints(cls)
    return tuple(
        (f.name, _reader(hints[f.name]), f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, bool(f.metadata.get("omit_empty")))
        for f in dataclasses.fields(cls)
    )


def _load(cls: type[R], data: Any) -> R:
    if not isinstance(data, dict):
        raise _wrong_kind(dict, data)
    plan = _plan(cls)
    values = {}
    for name, read, _, _ in plan:
        if name in data:
            try:
                values[name] = read(data[name])
            except _Wrong as e:
                e.path.append(name)
                raise
    missing = [name for name, _, required, _ in plan if required and name not in values]
    unknown = [str(k) for k in data if k not in values] if len(values) != len(data) else []
    if missing or unknown:  # both at once is what a misspelt field looks like
        said = ["no {}" if missing else "", "this arl does not know {}" if unknown else ""]
        raise _Wrong(lambda where: "; ".join(text.format(_named(names, where)) for text, names in zip(said, (missing, unknown)) if text))
    return cls(**values)


def load(cls: type[R], data: Any) -> R:
    """What a file held, as a record of this class. Raises ArlError with a few words
    that say what is wrong: a field that is missing, one this arl does not know,
    one of the wrong kind."""
    try:
        return _load(cls, data)
    except _Wrong as e:
        raise ArlError(e.text()) from None


def dump(record: Any) -> dict[str, Any]:
    """A record as what its file holds. None is not written."""
    out = {}
    for name, _, _, omit_empty in _plan(type(record)):
        value = getattr(record, name)
        if value is None or (omit_empty and not value):
            continue
        out[name] = _plain(value)
    return out


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dump(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
