"""A job arl started: what it is given, the one path it takes from its start to its
metrics, what may not change under it, and what is left to do when the arl that
ran it is gone."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Generator

from .. import contract, rules, runner
from ..config import Config
from ..ledger import Ledger
from ..record import Record, Rerun
from ..util import EXIT_BUSY, ArlError, now, parse_time
from . import common
from .common import ARL


def run_env(ledger: Ledger, rec: Record, values: dict[str, Any], resume: bool) -> dict[str, str]:
    env = runner.job_env(ledger)
    env.update(
        ARL_HOME=str(ledger.home),
        ARL_RESEARCH=str(ledger.dir),
        ARL_EXP=str(values["exp"]),
        ARL_OUT=str(values["out"]),
        ARL_PARAMS=str(values["params"]),
        ARL_SPLIT=str(values["split"]),
        ARL_ID=rec.id,
        ARL_TRACK=rec.track,
        ARL_RESUME="1" if resume else "0",
        ARL_CHECKPOINT_EVERY=f"{ledger.cfg.checkpoint_every:g}",
    )
    return env


def places(exp: Path, what: str, n: int, split: str = "") -> tuple[Path, Path, Path]:
    """Where a job writes: its output folder, its log and the evaluator's log.
    what: "run" for attempt n of the experiment, "rerun" for its rerun n on a split."""
    logs = exp / "logs"
    if what == "run":
        return exp / "artifacts" / f"run-{n}", logs / f"run-{n}.log", logs / f"eval-{n}.log"
    return exp / "artifacts" / f"rerun-{n}-{split}", logs / f"rerun-{n}.log", logs / f"rerun-eval-{n}.log"


def require_free_machine(cfg: Config) -> None:
    reasons = runner.busy(cfg)
    if reasons:
        raise ArlError(
            "The machine is busy, not starting:\n  " + "\n  ".join(reasons) + "\nWait and try again. Leave other jobs alone.",
            EXIT_BUSY,
        )


# --- the one path of a job --------------------------------------------------------


@dataclasses.dataclass
class Step:
    """One process of a job, the run or the evaluation of what it left: how long it
    took and how it ended."""

    seconds: float = 0.0
    ended_by: str = ""  # finished, failed, paused or interrupted
    exit_code: int | None = None  # None when the process never started, or nobody saw it end


@dataclasses.dataclass
class Job:
    """What measure() is told about a run or a rerun."""

    what: str  # "run" or "rerun": the word the messages use
    command: str  # the run command, its placeholders not filled yet
    cwd: Path  # the worktree it runs in
    values: dict[str, Any]  # what fills the placeholders of the run command and the evaluator's
    env: dict[str, str]
    log: Path
    eval_log: Path
    contract: str | None  # the digest of the contract it is scored under. None: not scored, so not evaluated
    data: str | None  # the digest of the data files it reads. None: a record from before they were written down
    timeout: float  # seconds the run may take, over all its parts
    used: float = 0.0  # seconds the earlier parts of the run took
    on_start: Callable[[int], None] | None = None  # told the pid of each process when it starts
    run_ended: Callable[[Step], None] | None = None  # told how the run ended, once, the moment it does
    evaluation_ended: Callable[[Step], None] | None = None  # the same for the evaluation
    same_code: Callable[[], bool] | None = None  # whether the worktree still holds the commit that was recorded


@dataclasses.dataclass
class Outcome:
    """What came of a job."""

    failed: str = ""  # why it does not count
    paused: str = ""  # why it stopped early without failing, when it did
    artifacts: list[dict[str, Any]] | None = None  # what the run left, before the evaluator added to it
    train_metrics: dict[str, float] = dataclasses.field(default_factory=dict)  # what the run says about itself
    note: str = ""  # why that could not be read, when it could not
    metrics: dict[str, float] | None = None  # the evaluator's numbers

    @property
    def went_on(self) -> bool:
        return not (self.failed or self.paused)


@contextlib.contextmanager
def _booked(ended: Callable[[Step], None] | None) -> Generator[Step, None, None]:
    """One step of a job. However it ends, it ends once and what it took is told at that
    moment: "failed" when arl refused something in the middle, "interrupted" when arl
    itself is being stopped. Time that nobody books is time the budget never sees."""
    step, clock = Step(), time.monotonic()
    try:
        yield step
    except ArlError:
        step.ended_by = "failed"
        raise
    except BaseException:
        step.ended_by = "interrupted"
        raise
    finally:
        if step.exit_code is None:  # the process did not come back: count the time until now
            step.seconds = round(time.monotonic() - clock, 2)
        if ended is not None:
            ended(step)


def measure(ledger: Ledger, job: Job, run: bool = True) -> Outcome:
    """Run a job and evaluate what it left. A run and a rerun both go this way.

    The job, within its limits. Then what must still be as it was recorded: the
    code, the contract, the data files. Then the evaluator, the same questions
    again, and the metrics. Whatever goes wrong on the way is the reason the job
    failed. A job that was asked to save its state and stop has not failed: it
    is paused.

    run: False when an earlier part of the run finished the job itself and only
    the evaluation is left.
    Raises only when arl itself is being stopped. The step that was going is
    booked before that, like any other.
    """
    cfg, got = ledger.cfg, Outcome()
    out, metrics = Path(job.values["out"]), Path(job.values["metrics"])
    watch: dict[str, Any] = {
        "max_rss_gb": cfg.max_run_rss_gb,
        "min_free_ram_gb": cfg.stop_below_free_ram_gb,
        "pause_file": job.log.parent / "pause.request",
        "pause_grace": cfg.pause_grace,
        "heartbeat": job.log.parent / "heartbeat",
        "on_start": job.on_start,
    }
    try:
        if run:
            with _booked(job.run_ended) as step:
                left = job.timeout - job.used
                if left <= 0:
                    got.failed = f"the {job.what} passed its limit of {job.timeout:.0f} s over all its parts"
                else:
                    res = runner.execute(runner.substitute(job.command, job.values), job.cwd, job.env, left, job.log, **watch)
                    step.seconds, step.exit_code = res["seconds"], res["exit_code"]
                    if res["paused"]:
                        got.paused = res["paused"]
                    elif res["stopped"]:
                        got.failed = f"the {job.what} was stopped: {res['stopped']}"
                    elif res["exit_code"] != 0:
                        got.failed = f"the {job.what} exited with code {res['exit_code']}"
                    elif job.same_code is not None and not job.same_code():
                        got.failed = f"the code in the worktree changed while the {job.what} was going, so the result does not belong to the recorded commit"
                step.ended_by = "paused" if got.paused else "failed" if got.failed else "finished"
        if got.went_on:
            got.artifacts = runner.manifest(out)
            got.train_metrics, got.note = runner.read_train_metrics(out / "train_metrics.json")
            if job.contract is not None and not [a for a in got.artifacts if not a["path"].startswith("checkpoint/")]:
                got.failed = f"the {job.what} wrote nothing into its output folder"  # saved state is not a result
            else:
                got.failed = moved(ledger, job.contract, job.data, f"while the {job.what} was going")
        if got.went_on and job.contract is not None:
            metrics.unlink(missing_ok=True)
            with _booked(job.evaluation_ended) as step:
                ev = runner.execute(runner.substitute(cfg.eval_command, job.values), ledger.home, job.env, cfg.eval_timeout, job.eval_log, **watch)
                step.seconds, step.exit_code = ev["seconds"], ev["exit_code"]
                if ev["paused"]:
                    got.paused = f"{ev['paused']} (during the evaluation; the {job.what} itself is complete)"
                elif ev["stopped"]:
                    got.failed = f"the evaluator was stopped: {ev['stopped']}"
                elif ev["exit_code"] != 0:
                    got.failed = f"the evaluator exited with code {ev['exit_code']}"
                else:
                    got.failed = moved(ledger, job.contract, job.data, "while the evaluator was running")
                if got.went_on:
                    got.metrics = runner.read_metrics(metrics, cfg)
                step.ended_by = "paused" if got.paused else "failed" if got.failed else "finished"
    except ArlError as e:
        got.failed = str(e)
    return got


def moved(ledger: Ledger, contract_digest: str | None, data: str | None, when: str) -> str:
    """What changed under a job while it was going, in words, or "" when the contract and
    the data files are what they were when it started. A run and a rerun ask this the
    same way, after the job and after its evaluation: a result made while one of them
    changed belongs to nothing that was recorded, and is not kept.

    contract_digest: None for a job that is not scored. data: None when the record is
    from before data files were written down.
    """
    if contract_digest is not None:
        lock = contract.require(ledger)  # raises when the files no longer match the lock: the job touched them
        if lock["digest"] != contract_digest:
            return f"the contract was frozen anew {when} (it is v{lock['version']} now), so the result belongs to no contract that was recorded"
    if data is not None and runner.data_now(ledger) != data:
        return f"the data files changed {when}, so the result belongs to no data that was recorded"
    return ""


def amend(ledger: Ledger, eid: str, change: Callable[[Record], None], wait: float = 120.0) -> Record:
    """Change one record from the middle of a job, or when it has just ended.

    The command that started the job read the record before it began, and another
    command may have written to it since: a verdict given while a rerun is going.
    So the change is made to the record as it is now, under the ledger lock, and
    never by writing back the copy from before.
    """
    with ledger.held(wait):
        rec = ledger.record(eid)
        change(rec)
        ledger.save_record(rec)
    return rec


def keep_outcome(ledger: Ledger, eid: str, write: Callable[[], None], message: str, what: str = "outcome") -> None:
    """The job is over: put what came of it into the record and commit.

    The outcome must not be lost because git or the home checkout is in the way.
    Then it is saved all the same, and the next command that writes the ledger
    commits it.
    write: puts the outcome into the record and saves it. It is called a second
    time when the first could not be committed, and must then change nothing.
    """
    try:
        with ledger.locked(wait=900):
            write()
            common.finish(ledger, message)
    except ArlError as e:
        try:
            with ledger.held(30):
                write()
        except ArlError:
            write()  # the lock is taken or the ledger cannot be read: the record of this experiment is written anyway
        print(f"{eid}: the {what} is saved but could not be committed:\n  {e}\nFix that; the next arl command that writes the ledger commits it.", file=sys.stderr)


# --- what a dead or interrupted process left --------------------------------------


def fail(rec: Record, reason: str) -> None:
    """The attempt failed: it counts, and the next one starts from nothing."""
    attempt = rec.attempts[-1]
    attempt.ended = attempt.ended or now()
    attempt.failed = reason
    attempt.paused = None
    rec.status = "crashed"
    rec.forget_job()
    rec.metrics = rec.comparison = None


def pause_run(rec: Record, how: str) -> None:
    """A part of the run ended early without failing: the run can be continued."""
    attempt = rec.attempts[-1]
    attempt.paused = how
    attempt.run_seconds = round(attempt.ran(), 2)
    rec.status = "paused"
    rec.forget_job()


def ran_for(started: str, signs: list[Path], until_now: bool = False) -> float:
    """How long a job ran that nobody saw end.

    until_now: it was still going when it was found, so it ran until this moment.
    Otherwise until its last sign of life: the file arl marks every second while it
    watches a job, the log, or what the job wrote into its output folder. A job
    that says nothing is charged the time it ran all the same.
    """
    try:
        last = dt.datetime.now().timestamp() if until_now else runner.last_sign(signs)
        if last is None:
            return 0.0
        end = dt.datetime.fromtimestamp(last).astimezone()
        return max(0.0, round((end - parse_time(started)).total_seconds(), 2))
    except (OSError, ValueError):
        return 0.0


def _stop_leftover(child: dict[str, Any] | None, grace: float = runner.QUICK_GRACE) -> bool:
    """Stop the job a dead arl left behind. True when something was still going."""
    sid = runner.session_of(child)
    return bool(sid and runner.stop_session(sid, grace))


def _signs(exp: Path, what: str, n: int, split: str = "") -> list[Path]:
    """Where a job leaves signs of life: the file arl marks while it watches, the two logs,
    the output folder."""
    out, log, eval_log = places(exp, what, n, split)
    return [log.parent / "heartbeat", log, eval_log, out]


def settle(ledger: Ledger, rec: Record, certain: bool = False) -> bool:
    """A record says a job is going, but the arl that ran it is gone.

    Stop whatever it left behind, and leave the run where it can be continued.
    certain: the caller holds the run lock, so nothing can be running.
    """
    changed = False
    exp = ledger.exp_dir(rec.id)
    gone = rec.status == "running" and (certain or rules.stale(rec))
    left = gone and _stop_leftover(rec.child, ledger.cfg.pause_grace)  # a run gets the time to save its state
    if gone and not (rec.code_commit is not None and rec.contract is not None):
        # It was marked as running and never got as far as starting: back to where it was.
        rec.attempts = [a for a in rec.attempts if a.code_commit]
        rec.status = "crashed" if rec.attempts else "draft"
        rec.forget_job()
        changed = True
    elif gone:
        attempt = rec.attempts[-1]
        last = attempt.segments[-1] if attempt.segments else None
        if last is not None and last.ended is None:
            last.ended, last.ended_by = now(), "interrupted"
            last.seconds = ran_for(last.started, _signs(exp, "run", attempt.n), until_now=left)
            if left:
                last.note = "the job it left behind was stopped"
        pause_run(rec, "interrupted: the arl that ran it was gone" + (", the job it left behind was stopped" if left else ""))
        changed = True
    rerun = rec.rerun
    if rerun and (certain or not runner.alive(rerun.pid, rerun.pid_start)):
        left = _stop_leftover(rerun.child)
        tree = Path(rerun.tree or "")
        if rerun.tree and tree.exists():
            drop_tree(ledger, tree)
        signs = _signs(exp, "rerun", rerun.n, rerun.split)
        entry = Rerun(
            n=rerun.n,
            at=now(),
            split=rerun.split,
            eval_digest=rerun.eval_digest,
            data_digest=rerun.data_digest,
            run_seconds=ran_for(rerun.started, signs, until_now=left),
            interrupted=True,  # not a failure of the experiment: it does not count against a confirmation
            failed="interrupted: the arl that ran it was gone" + (", its job was stopped" if left else ""),
        )
        rec.reruns.append(entry)
        rec.rerun = None
        changed = True
    if changed:
        ledger.save_record(rec)
        common.finish(ledger, f"{rec.id} interrupted")
    return changed


def settle_all(ledger: Ledger) -> None:
    """Holding the run lock: every job a record still names is a leftover. Stop them all
    before anything starts, whichever experiment the command is about."""
    for rec in ledger.experiments():
        if rec.status == "running" or rec.rerun:
            settle(ledger, rec, certain=True)


def drop_tree(ledger: Ledger, tree: Path) -> None:
    """Remove one of arl's own throwaway trees."""
    try:
        ledger.git.remove_worktree(tree)
    except ArlError:
        shutil.rmtree(tree, ignore_errors=True)
    ledger.git.prune_worktrees()


# --- asking a job to stop ---------------------------------------------------------


def ask_to_pause(ledger: Ledger, only: str | None = None) -> list[tuple[str, str]]:
    """Leave a request for every job that is going. Returns (experiment, "run" or "rerun")."""
    asked = []
    for rec in ledger.experiments():
        if only not in (None, rec.id):
            continue
        rerun = rec.rerun
        if rec.status == "running" and not rules.stale(rec):
            kind = "run"
        elif rerun and runner.alive(rerun.pid, rerun.pid_start):
            kind = "rerun"
        else:
            continue
        request = ledger.exp_dir(rec.id) / "logs" / "pause.request"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(now())
        asked.append((rec.id, kind))
    return asked


def say_asked(ledger: Ledger, asked: list[tuple[str, str]]) -> None:
    grace = f"{ledger.cfg.pause_grace:.0f} s"
    for eid, kind in asked:
        if kind == "run":
            print(f"{eid} was asked to save its state and stop; it has {grace} before it is forced.")
            print(f"  It continues later, from what it saved:  {ARL} exp run {eid}")
        else:
            print(f"The rerun of {eid} was asked to stop; it has {grace}. A rerun is not continued: run it again later.")
