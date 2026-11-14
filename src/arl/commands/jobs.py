"""A job arl started: what it is given, what may not change under it, and what is
left to do when the arl that ran it is gone."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Callable

from .. import contract, rules, runner
from ..ledger import Ledger
from ..util import ArlError, now, parse_time
from . import common
from .common import ARL


def run_env(ledger: Ledger, rec: dict[str, Any], values: dict[str, Any], resume: bool) -> dict[str, str]:
    env = runner.job_env(ledger)
    env.update(
        ARL_HOME=str(ledger.home),
        ARL_RESEARCH=str(ledger.dir),
        ARL_EXP=str(values["exp"]),
        ARL_OUT=str(values["out"]),
        ARL_PARAMS=str(values["params"]),
        ARL_SPLIT=str(values["split"]),
        ARL_ID=rec["id"],
        ARL_TRACK=rec["track"],
        ARL_RESUME="1" if resume else "0",
        ARL_CHECKPOINT_EVERY=f"{ledger.cfg.checkpoint_every:g}",
    )
    return env


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


def amend(ledger: Ledger, eid: str, change: Callable[[dict[str, Any]], None], wait: float = 120.0) -> dict[str, Any]:
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


# --- what a dead or interrupted process left --------------------------------------


def fail(rec: dict[str, Any], reason: str) -> None:
    """The attempt failed: it counts, and the next one starts from nothing."""
    attempt = rec["attempts"][-1]
    attempt.setdefault("ended", now())
    attempt["failed"] = reason
    attempt.pop("paused", None)
    rec["status"] = "crashed"
    for key in ("pid", "pid_start", "child", "metrics", "comparison"):
        rec.pop(key, None)


def pause_run(rec: dict[str, Any], how: str) -> None:
    """A part of the run ended early without failing: the run can be continued."""
    attempt = rec["attempts"][-1]
    attempt["paused"] = how
    attempt["run_seconds"] = round(sum(s.get("seconds", 0) for s in attempt.get("segments", [])), 2)
    rec["status"] = "paused"
    for key in ("pid", "pid_start", "child"):
        rec.pop(key, None)


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


def settle(ledger: Ledger, rec: dict[str, Any], certain: bool = False) -> bool:
    """A record says a job is going, but the arl that ran it is gone.

    Stop whatever it left behind, and leave the run where it can be continued.
    certain: the caller holds the run lock, so nothing can be running.
    """
    changed = False
    exp = ledger.exp_dir(rec["id"])
    logs = exp / "logs"
    gone = rec["status"] == "running" and (certain or rules.stale(rec))
    if gone:
        sid = runner.session_of(rec.get("child"))
        left = bool(sid and runner.stop_session(sid, ledger.cfg.pause_grace))
    if gone and not ("code_commit" in rec and "contract" in rec):
        # It was marked as running and never got as far as starting: back to where it was.
        rec["attempts"] = [a for a in rec.get("attempts", []) if a.get("code_commit")]
        rec["status"] = "crashed" if rec["attempts"] else "draft"
        for key in ("pid", "pid_start", "child"):
            rec.pop(key, None)
        changed = True
    elif gone:
        attempt = rec["attempts"][-1]
        parts = attempt.setdefault("segments", [])
        if parts and "ended" not in parts[-1]:
            n = attempt["n"]
            signs = [logs / "heartbeat", logs / f"run-{n}.log", logs / f"eval-{n}.log", exp / "artifacts" / f"run-{n}"]
            parts[-1].update(ended=now(), ended_by="interrupted", seconds=ran_for(parts[-1]["started"], signs, until_now=left))
            if left:
                parts[-1]["note"] = "the job it left behind was stopped"
        pause_run(rec, "interrupted: the arl that ran it was gone" + (", the job it left behind was stopped" if left else ""))
        changed = True
    rerun = rec.get("rerun")
    if rerun and (certain or not runner.alive(rerun.get("pid"), rerun.get("pid_start"))):
        sid = runner.session_of(rerun.get("child"))
        left = bool(sid and runner.stop_session(sid))
        tree = Path(rerun.get("tree", ""))
        if rerun.get("tree") and tree.exists():
            drop_tree(ledger, tree)
        n = rerun["n"]
        signs = [logs / "heartbeat", logs / f"rerun-{n}.log", logs / f"rerun-eval-{n}.log", exp / "artifacts" / f"rerun-{n}-{rerun['split']}"]
        entry = {"n": n, "at": now(), "split": rerun["split"], "run_seconds": ran_for(rerun["started"], signs, until_now=left)}
        entry.update({k: rerun[k] for k in ("eval_digest", "data_digest") if k in rerun})
        entry["failed"] = "interrupted: the arl that ran it was gone" + (", its job was stopped" if left else "")
        entry["interrupted"] = True  # not a failure of the experiment: it does not count against a confirmation
        rec.setdefault("reruns", []).append(entry)
        rec.pop("rerun")
        changed = True
    if changed:
        ledger.save_record(rec)
        common.finish(ledger, f"{rec['id']} interrupted")
    return changed


def settle_all(ledger: Ledger) -> None:
    """Holding the run lock: every job a record still names is a leftover. Stop them all
    before anything starts, whichever experiment the command is about."""
    for rec in ledger.experiments():
        if rec["status"] == "running" or rec.get("rerun"):
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
        if only not in (None, rec["id"]):
            continue
        rerun = rec.get("rerun")
        if rec["status"] == "running" and not rules.stale(rec):
            kind = "run"
        elif rerun and runner.alive(rerun.get("pid"), rerun.get("pid_start")):
            kind = "rerun"
        else:
            continue
        request = ledger.exp_dir(rec["id"]) / "logs" / "pause.request"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(now())
        asked.append((rec["id"], kind))
    return asked


def say_asked(ledger: Ledger, asked: list[tuple[str, str]]) -> None:
    grace = f"{ledger.cfg.pause_grace:.0f} s"
    for eid, kind in asked:
        if kind == "run":
            print(f"{eid} was asked to save its state and stop; it has {grace} before it is forced.")
            print(f"  It continues later, from what it saved:  {ARL} exp run {eid}")
        else:
            print(f"The rerun of {eid} was asked to stop; it has {grace}. A rerun is not continued: run it again later.")
