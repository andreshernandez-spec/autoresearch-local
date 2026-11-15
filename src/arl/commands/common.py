"""What every command leans on: who is asking, the gates, the worktrees, and the
ledger lock that finishes a verdict an earlier command left half applied."""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Generator

from .. import contract, render, rules, runner
from ..gitops import Git
from ..ledger import Ledger
from ..record import HeadMove, Record, Track, need
from ..util import ArlError, canonical, now, read_front, read_json, sha256_file, sha256_text, write_text


ARL = render.LAUNCHER


def interactive() -> bool:
    """A person at a terminal. Some decisions are only theirs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def finish(ledger: Ledger, message: str) -> None:
    """Refresh STATUS.md and commit the ledger."""
    write_text(ledger.status_path, render.status(ledger))
    ledger.commit(message)


def load_params(path: Path) -> dict[str, Any]:
    try:
        params = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise ArlError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(params, dict):
        raise ArlError(f"{path} must hold one JSON object")
    return params


def design_hashes(exp: Path) -> tuple[str, str]:
    design = exp / "design.md"
    if not design.exists():
        raise ArlError(f"{design} is missing. Restore it from git: the design is part of the record.")
    return sha256_file(design), sha256_text(canonical(load_params(exp / "params.json")))


def require_design_unchanged(ledger: Ledger, rec: Record) -> None:
    exp = ledger.exp_dir(rec.id)
    design = need(rec.design)
    if (design.design_sha256, design.params_sha256) != design_hashes(exp):
        raise ArlError(
            f"design.md or params.json of {rec.id} changed after its run started. The design is locked:\n"
            f"put them back ('git -C {ledger.home} diff -- {ledger.rel(exp)}')."
        )


def data_files(ledger: Ledger) -> tuple[list[dict[str, Any]], str]:
    fingerprint = runner.data_fingerprint(ledger)
    return fingerprint, runner.data_digest(fingerprint)


ENV_IS_THE_CONTRACTS = (
    "The environment belongs to the contract. Do not create it, do not install into it and do not\n"
    "run in another one: stop, and tell the person who owns the program what is missing."
)


def env_or_refuse(ledger: Ledger) -> dict[str, Any] | None:
    """What the python a job would get answered, for a command that is about to start work."""
    try:
        return runner.probe_env(ledger)
    except ArlError as e:
        raise ArlError(f"{e}\n{ENV_IS_THE_CONTRACTS}") from e


def env_or_problem(ledger: Ledger) -> dict[str, Any] | None:
    """For commands that only read: what is wrong with the environment is an answer, not an error."""
    try:
        return runner.probe_env(ledger)
    except ArlError as e:
        return {"problem": str(e)}


def next_action(ledger: Ledger) -> dict[str, Any]:
    return rules.next_action(ledger, runner.data_now(ledger), env_or_problem(ledger))


def env_note(ledger: Ledger) -> list[str]:
    """For the start of a session: where runs happen, and whether this shell's python is somewhere else."""
    prefix = runner.env_prefix(ledger)
    if prefix is None:
        return []
    lines = [f"==> Environment: runs and the evaluator use {ledger.cfg.env_prefix}. arl starts them there itself, whatever this shell has active."]
    other = runner.shell_python_elsewhere(ledger)
    if other:
        lines.append(f"    'python' in this shell is {other}. For anything you run yourself, call {prefix / 'bin' / 'python'}.")
    return lines + [""]


# --- gates ------------------------------------------------------------------------


def gate_paused(ledger: Ledger) -> None:
    paused = ledger.paused()
    if paused is not None:
        raise ArlError(
            f"The loop is paused ({paused.get('reason') or 'no reason given'}). Nothing starts, runs or is judged.\n"
            f"Continue only when the person says so: {ARL} resume"
        )


def gate_new_work(ledger: Ledger) -> None:
    """Reasons no new experiment may be drafted now."""
    gate_paused(ledger)
    state = rules.introspection_state(ledger)
    if state["due"]:
        raise ArlError(
            "An introspection comes first:\n  " + "\n  ".join(state["reasons"]) + f"\nRun '{ARL} next' for the steps."
        )
    if state["last_decision"] == "stop":
        raise ArlError(f"{state['last']} decided to stop the program. Only a person can reopen it.")
    if state["reload"]:
        raise ArlError(f"{state['reload']} is closed, but what it changed has not been read again. Run '{ARL} reload' first.")
    hit = rules.target_claim(ledger, contract.load(ledger), runner.data_now(ledger))
    if hit and hit["state"] == "confirmed":
        raise ArlError(f"{rules.claim_text(hit)}. That ends the program: run '{ARL} next'.")
    if hit:
        raise ArlError(f"{hit['id']} reached the target on {hit['first']}. That has to be confirmed first: run '{ARL} next'.")
    spent = rules.budget(ledger)["spent"]
    if spent:
        raise ArlError("The budget is spent: " + "; ".join(spent))


def gate_running(ledger: Ledger, first_attempt: bool) -> None:
    """Reasons nothing may be run now, even a draft that already exists."""
    gate_paused(ledger)
    state = rules.introspection_state(ledger)
    if state["waiting"]:
        raise ArlError(f"{state['waiting']} waits for a person to decide. Nothing runs until then.")
    if state["last_decision"] == "stop":
        raise ArlError(f"{state['last']} decided to stop the program. Nothing runs until a person reopens it.")
    if state["reload"]:
        raise ArlError(f"{state['reload']} is closed, but what it changed has not been read again. Run '{ARL} reload' first.")
    b = rules.budget(ledger)
    if b["hours_spent"] or (first_attempt and b["count_spent"]):
        raise ArlError("The budget is spent: " + "; ".join(b["spent"]))


def authorized(ledger: Ledger, nid: str | None, decisions: tuple[str, ...], what: str) -> str:
    """Who allows a change of direction: nobody is needed before the first verdict,
    after it the latest closed introspection, or a person at a terminal."""
    if not any(e.status == "judged" for e in ledger.experiments()):
        return "setup"
    if nid:
        meta, _ = ledger.introspection(nid)
        state = rules.introspection_state(ledger)
        pending = state["open"] or state["waiting"]
        if pending:
            raise ArlError(f"{pending} is not decided yet, so the one before it does not decide today. Finish {pending} first.")
        if meta.status != "closed" or nid != state["last"]:
            raise ArlError(f"{nid} is not the latest closed introspection ({state['last'] or 'there is none yet'}). An older one does not decide today.")
        if decisions and meta.decision not in decisions:
            raise ArlError(f"{nid} decided '{meta.decision}', not {' or '.join(decisions)}")
        return nid
    if interactive():
        return "person"
    raise ArlError(
        f"Once experiments have been judged, {what} comes out of an introspection:\n"
        "pass --introspection <id> of the latest closed one"
        + (f" (it must have decided {' or '.join(decisions)})" if decisions else "")
        + ",\nor let a person do it in a terminal."
    )


# --- worktrees --------------------------------------------------------------------


def worktree(ledger: Ledger, track: Track) -> tuple[Path, Git]:
    path = ledger.worktree_path(track.name)
    if not path.exists():
        raise ArlError(
            f"the worktree of track {track.name} is missing: {path}\n"
            f"Put it back with: {ARL} track worktree {track.name}"
        )
    return path, Git(path)


def park(ledger: Ledger, track: Track, path: Path, wgit: Git) -> None:
    """A worktree left on a branch that no experiment in flight owns (a draft that was cut
    off, a folder removed by hand): bring it back to the track's branch. The branch stays."""
    here = wgit.branch()
    if here == track.branch:
        return
    if any(e.branch == here for e in rules.in_flight(ledger, track.name)):
        return
    dirty = wgit.dirty()
    if dirty:
        raise ArlError(
            f"{path} is on '{here}', a branch no experiment in flight owns, and has uncommitted changes:\n  "
            + "\n  ".join(dirty[:10])
            + f"\nSave what you need elsewhere and put the files back (git -C {path} restore <file>), then try again."
        )
    wgit.switch(track.branch)


def exp_worktree(ledger: Ledger, rec: Record) -> tuple[Path, Git]:
    track = ledger.track(rec.track)
    path, wgit = worktree(ledger, track)
    here = wgit.branch()
    if here != rec.branch:
        # A draft whose branch was never made or never entered: finish what 'exp new' started.
        fresh = rec.status == "draft" and not rec.attempts and here == track.branch
        if fresh and wgit.head() == rec.base_commit and not wgit.dirty():
            if ledger.git.branch_exists(rec.branch):
                wgit.switch(rec.branch)
            else:
                wgit.switch_new(rec.branch, rec.base_commit)
        else:
            raise ArlError(f"{path} is on '{here}', but {rec.id} lives on '{rec.branch}'")
    return path, wgit


# --- a verdict, once recorded, is carried out -------------------------------------


def track_log(ledger: Ledger, meta: Track, line: str) -> None:
    body = read_front(ledger.track_path(meta.name))[1]
    ledger.save_track(meta, body.rstrip("\n") + f"\n- {now()[:10]}: {' '.join(line.split())}\n")


def finish_judge(ledger: Ledger, rec: Record) -> None:
    """Apply a recorded verdict: branch, track, hypotheses, then the record.

    Every step looks at what is already done, so it can be run again after a
    process died in the middle, and a different verdict can no longer be given.
    """
    eid, verdict = rec.id, need(rec.verdict)
    decision = verdict.decision
    track = ledger.track(rec.track)
    path, wgit = worktree(ledger, track)
    exp = ledger.exp_dir(eid)
    if rec.branch != track.branch:
        here = wgit.branch()
        if here == rec.branch:
            changed = wgit.tracked_changes()
            if changed:
                raise ArlError(f"tracked files in {path} were edited while the verdict of {eid} was being recorded:\n  " + "\n  ".join(changed[:10]) + f"\nPut them back (git -C {path} restore <file>), then run '{ARL} exp judge {eid}'.")
            wgit.switch(track.branch)
        elif here != track.branch:
            raise ArlError(f"{path} is on '{here}', neither the branch of {eid} nor the track's. Put it on '{track.branch}' and run '{ARL} exp judge {eid}'.")
    if decision == "keep":
        code = need(rec.code_commit)
        if rec.branch != track.branch and not wgit.is_ancestor(code, "HEAD"):
            wgit.fast_forward(code)
        if wgit.head() != code:
            raise ArlError(f"track {rec.track} is at {wgit.head()[:10]}, not at the commit {eid} measured ({code[:10]}). The branch was moved by hand: put it back, then run '{ARL} exp judge {eid}'.")
        track.head_commit, track.head_experiment = code, eid
        ledger.write_track_params(track, load_params(exp / "params.json"))
        line = f"{eid} kept ({rec.type}): {rec.title}"
        if line not in read_front(ledger.track_path(track.name))[1]:
            track_log(ledger, track, line)
        else:
            ledger.save_track(track)
    for hid, state in (verdict.hypotheses or {}).items():
        if state == "open":
            ledger.log_hypothesis(hid, f"{eid} ({decision}): left as it is")
        else:
            ledger.set_hypothesis(hid, state, f"{eid} ({decision})")
    verdict.track_head = HeadMove(before=verdict.track_head.before if verdict.track_head else track.head_commit, after=track.head_commit)
    rec.status = "judged"
    ledger.save_record(rec)
    finish(ledger, f"{eid} {decision}")


@contextlib.contextmanager
def writing(ledger: Ledger, wait: float = 120.0) -> Generator[None, None, None]:
    """The ledger lock, after finishing any verdict an earlier command left half applied."""
    with ledger.locked(wait):
        for rec in ledger.experiments():
            if rec.status == "judging":
                finish_judge(ledger, rec)
        yield
