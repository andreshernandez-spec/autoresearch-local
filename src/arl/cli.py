"""Command line. Every change of the ledger goes through one of these commands.

Three habits hold throughout:

- A command checks everything it can before it changes anything, so a refusal
  leaves the ledger, the worktrees and git as they were.
- Where a change takes several steps (a verdict moves a branch, a track and a
  record), the intent is written first and the steps can be finished later by
  any command, so a process that dies in the middle leaves nothing stuck.
- A run is made of parts. A part that is interrupted (a pause, a closed
  terminal, a killed process) is not a failure: the run continues later in the
  same folder, from the state the job last saved.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Generator

from . import contract, render, rules, runner
from .config import DECISIONS
from .gitops import Git
from .ledger import HYP_STATES, IDEA_STATES, SCOPES, Ledger
from .util import (
    EXIT_BUSY,
    EXIT_CRASH,
    EXIT_PAUSED,
    ArlError,
    canonical,
    check_id,
    check_text,
    fmt,
    now,
    one_line,
    parse_time,
    read_front,
    read_json,
    require_filled,
    sha256_file,
    sha256_text,
    slugify,
    write_front,
    write_json,
    write_text,
)

ARL = render.LAUNCHER
HYP_VERDICTS = ("supported", "refuted", "inconclusive", "open")
AT_TERMINAL = "a person at a terminal"


def interactive() -> bool:
    """A person at a terminal. Some decisions are only theirs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def finish(ledger: Ledger, message: str) -> None:
    """Refresh STATUS.md and commit the ledger."""
    write_text(ledger.status_path, render.status(ledger))
    ledger.commit(message)


def _load_params(path: Path) -> dict[str, Any]:
    try:
        params = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise ArlError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(params, dict):
        raise ArlError(f"{path} must hold one JSON object")
    return params


def _design_hashes(exp: Path) -> tuple[str, str]:
    design = exp / "design.md"
    if not design.exists():
        raise ArlError(f"{design} is missing. Restore it from git: the design is part of the record.")
    return sha256_file(design), sha256_text(canonical(_load_params(exp / "params.json")))


def _require_design_unchanged(ledger: Ledger, rec: dict[str, Any]) -> None:
    exp = ledger.exp_dir(rec["id"])
    if (rec["design"]["design_sha256"], rec["design"]["params_sha256"]) != _design_hashes(exp):
        raise ArlError(
            f"design.md or params.json of {rec['id']} changed after its run started. The design is locked:\n"
            f"put them back ('git -C {ledger.home} diff -- {ledger.rel(exp)}')."
        )


def _data(ledger: Ledger) -> tuple[list[dict[str, Any]], str]:
    fingerprint = runner.data_fingerprint(ledger)
    return fingerprint, runner.data_digest(fingerprint)


def _data_digest_or_none(ledger: Ledger) -> str | None:
    """For commands that only read: a missing data file must not stop them."""
    try:
        return _data(ledger)[1]
    except ArlError:
        return None


ENV_IS_THE_CONTRACTS = (
    "The environment belongs to the contract. Do not create it, do not install into it and do not\n"
    "run in another one: stop, and tell the person who owns the program what is missing."
)


def _env(ledger: Ledger) -> dict[str, Any] | None:
    """What the python a job would get answered, for a command that is about to start work."""
    try:
        return runner.probe_env(ledger)
    except ArlError as e:
        raise ArlError(f"{e}\n{ENV_IS_THE_CONTRACTS}") from e


def _env_or_problem(ledger: Ledger) -> dict[str, Any] | None:
    """For commands that only read: what is wrong with the environment is an answer, not an error."""
    try:
        return runner.probe_env(ledger)
    except ArlError as e:
        return {"problem": str(e)}


def _next(ledger: Ledger) -> dict[str, Any]:
    return rules.next_action(ledger, _data_digest_or_none(ledger), _env_or_problem(ledger))


def _env_note(ledger: Ledger) -> list[str]:
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


def _gate_paused(ledger: Ledger) -> None:
    paused = ledger.paused()
    if paused is not None:
        raise ArlError(
            f"The loop is paused ({paused.get('reason') or 'no reason given'}). Nothing starts, runs or is judged.\n"
            f"Continue only when the person says so: {ARL} resume"
        )


def _gate_new_work(ledger: Ledger) -> None:
    """Reasons no new experiment may be drafted now."""
    _gate_paused(ledger)
    state = rules.introspection_state(ledger)
    if state["due"]:
        raise ArlError(
            "An introspection comes first:\n  " + "\n  ".join(state["reasons"]) + f"\nRun '{ARL} next' for the steps."
        )
    if state["last_decision"] == "stop":
        raise ArlError(f"{state['last']} decided to stop the program. Only a person can reopen it.")
    if state["reload"]:
        raise ArlError(f"{state['reload']} is closed, but what it changed has not been read again. Run '{ARL} reload' first.")
    hit = rules.target_met(ledger)
    if hit:
        raise ArlError(f"{hit['id']} reached the target. Run '{ARL} next'.")
    spent = rules.budget(ledger)["spent"]
    if spent:
        raise ArlError("The budget is spent: " + "; ".join(spent))


def _gate_running(ledger: Ledger, first_attempt: bool) -> None:
    """Reasons nothing may be run now, even a draft that already exists."""
    _gate_paused(ledger)
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


def _authorized(ledger: Ledger, nid: str | None, decisions: tuple[str, ...], what: str) -> str:
    """Who allows a change of direction: nobody is needed before the first verdict,
    after it the latest closed introspection, or a person at a terminal."""
    if not any(e["status"] == "judged" for e in ledger.experiments()):
        return "setup"
    if nid:
        meta, _ = ledger.introspection(nid)
        state = rules.introspection_state(ledger)
        pending = state["open"] or state["waiting"]
        if pending:
            raise ArlError(f"{pending} is not decided yet, so the one before it does not decide today. Finish {pending} first.")
        if meta["status"] != "closed" or nid != state["last"]:
            raise ArlError(f"{nid} is not the latest closed introspection ({state['last'] or 'there is none yet'}). An older one does not decide today.")
        if decisions and meta.get("decision") not in decisions:
            raise ArlError(f"{nid} decided '{meta.get('decision')}', not {' or '.join(decisions)}")
        return nid
    if interactive():
        return "person"
    raise ArlError(
        f"Once experiments have been judged, {what} comes out of an introspection:\n"
        "pass --introspection <id> of the latest closed one"
        + (f" (it must have decided {' or '.join(decisions)})" if decisions else "")
        + ",\nor let a person do it in a terminal."
    )


# --- worktrees ----------------------------------------------------------------------


def _worktree(ledger: Ledger, track: dict[str, Any]) -> tuple[Path, Git]:
    path = ledger.worktree_path(track["name"])
    if not path.exists():
        raise ArlError(
            f"the worktree of track {track['name']} is missing: {path}\n"
            f"Put it back with: {ARL} track worktree {track['name']}"
        )
    return path, Git(path)


def _park(ledger: Ledger, track: dict[str, Any], path: Path, wgit: Git) -> None:
    """A worktree left on a branch that no experiment in flight owns (a draft that was cut
    off, a folder removed by hand): bring it back to the track's branch. The branch stays."""
    here = wgit.branch()
    if here == track["branch"]:
        return
    if any(e["branch"] == here for e in rules.in_flight(ledger, track["name"])):
        return
    dirty = wgit.dirty()
    if dirty:
        raise ArlError(
            f"{path} is on '{here}', a branch no experiment in flight owns, and has uncommitted changes:\n  "
            + "\n  ".join(dirty[:10])
            + f"\nSave what you need elsewhere and put the files back (git -C {path} restore <file>), then try again."
        )
    wgit.switch(track["branch"])


def _exp_worktree(ledger: Ledger, rec: dict[str, Any]) -> tuple[Path, Git]:
    track = ledger.track(rec["track"])
    path, wgit = _worktree(ledger, track)
    here = wgit.branch()
    if here != rec["branch"]:
        # A draft whose branch was never made or never entered: finish what 'exp new' started.
        fresh = rec["status"] == "draft" and not rec["attempts"] and here == track["branch"]
        if fresh and wgit.head() == rec["base_commit"] and not wgit.dirty():
            if ledger.git.branch_exists(rec["branch"]):
                wgit.switch(rec["branch"])
            else:
                wgit.switch_new(rec["branch"], rec["base_commit"])
        else:
            raise ArlError(f"{path} is on '{here}', but {rec['id']} lives on '{rec['branch']}'")
    return path, wgit


# --- what a dead or interrupted process left ------------------------------------------


def _fail(rec: dict[str, Any], reason: str) -> None:
    """The attempt failed: it counts, and the next one starts from nothing."""
    attempt = rec["attempts"][-1]
    attempt.setdefault("ended", now())
    attempt["failed"] = reason
    attempt.pop("paused", None)
    rec["status"] = "crashed"
    for key in ("pid", "pid_start", "child", "metrics", "comparison"):
        rec.pop(key, None)


def _pause_run(rec: dict[str, Any], how: str) -> None:
    """A part of the run ended early without failing: the run can be continued."""
    attempt = rec["attempts"][-1]
    attempt["paused"] = how
    attempt["run_seconds"] = round(sum(s.get("seconds", 0) for s in attempt.get("segments", [])), 2)
    rec["status"] = "paused"
    for key in ("pid", "pid_start", "child"):
        rec.pop(key, None)


def _seconds_since(started: str, log: Path) -> float:
    """How long a part ran that nobody saw end: until its log was last written."""
    try:
        end = dt.datetime.fromtimestamp(log.stat().st_mtime).astimezone()
        return max(0.0, round((end - parse_time(started)).total_seconds(), 2))
    except (OSError, ValueError):
        return 0.0


def _settle(ledger: Ledger, rec: dict[str, Any], certain: bool = False) -> bool:
    """A record says a job is going, but the arl that ran it is gone.

    Stop whatever it left behind, and leave the run where it can be continued.
    certain: the caller holds the run lock, so nothing can be running.
    """
    changed = False
    exp = ledger.exp_dir(rec["id"])
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
            log = exp / "logs" / (f"eval-{n}.log" if attempt.get("run_done") else f"run-{n}.log")
            parts[-1].update(ended=now(), ended_by="interrupted", seconds=_seconds_since(parts[-1]["started"], log))
            if left:
                parts[-1]["note"] = "the job it left behind was stopped"
        _pause_run(rec, "interrupted: the arl that ran it was gone" + (", the job it left behind was stopped" if left else ""))
        changed = True
    rerun = rec.get("rerun")
    if rerun and (certain or not runner.alive(rerun.get("pid"), rerun.get("pid_start"))):
        sid = runner.session_of(rerun.get("child"))
        left = bool(sid and runner.stop_session(sid))
        tree = Path(rerun.get("tree", ""))
        if rerun.get("tree") and tree.exists():
            _drop_tree(ledger, tree)
        seconds = _seconds_since(rerun["started"], exp / "logs" / f"rerun-{rerun['n']}.log")
        entry = {"n": rerun["n"], "at": now(), "split": rerun["split"], "run_seconds": seconds}
        entry["failed"] = "interrupted: the arl that ran it was gone" + (", its job was stopped" if left else "")
        rec.setdefault("reruns", []).append(entry)
        rec.pop("rerun")
        changed = True
    if changed:
        ledger.save_record(rec)
        finish(ledger, f"{rec['id']} interrupted")
    return changed


def _settle_all(ledger: Ledger) -> None:
    """Holding the run lock: every job a record still names is a leftover. Stop them all
    before anything starts, whichever experiment the command is about."""
    for rec in ledger.experiments():
        if rec["status"] == "running" or rec.get("rerun"):
            _settle(ledger, rec, certain=True)


def _drop_tree(ledger: Ledger, tree: Path) -> None:
    """Remove one of arl's own throwaway trees."""
    try:
        ledger.git.remove_worktree(tree)
    except ArlError:
        shutil.rmtree(tree, ignore_errors=True)
    ledger.git.prune_worktrees()


def _finish_judge(ledger: Ledger, rec: dict[str, Any]) -> None:
    """Apply a recorded verdict: branch, track, hypotheses, then the record.

    Every step looks at what is already done, so it can be run again after a
    process died in the middle, and a different verdict can no longer be given.
    """
    eid, verdict = rec["id"], rec["verdict"]
    decision = verdict["decision"]
    track = ledger.track(rec["track"])
    path, wgit = _worktree(ledger, track)
    exp = ledger.exp_dir(eid)
    if rec["branch"] != track["branch"]:
        here = wgit.branch()
        if here == rec["branch"]:
            changed = wgit.tracked_changes()
            if changed:
                raise ArlError(f"tracked files in {path} were edited while the verdict of {eid} was being recorded:\n  " + "\n  ".join(changed[:10]) + f"\nPut them back (git -C {path} restore <file>), then run '{ARL} exp judge {eid}'.")
            wgit.switch(track["branch"])
        elif here != track["branch"]:
            raise ArlError(f"{path} is on '{here}', neither the branch of {eid} nor the track's. Put it on '{track['branch']}' and run '{ARL} exp judge {eid}'.")
    if decision == "keep":
        code = rec["code_commit"]
        if rec["branch"] != track["branch"] and not wgit.is_ancestor(code, "HEAD"):
            wgit.fast_forward(code)
        if wgit.head() != code:
            raise ArlError(f"track {rec['track']} is at {wgit.head()[:10]}, not at the commit {eid} measured ({code[:10]}). The branch was moved by hand: put it back, then run '{ARL} exp judge {eid}'.")
        track["head_commit"], track["head_experiment"] = code, eid
        ledger.write_track_params(track, _load_params(exp / "params.json"))
        line = f"{eid} kept ({rec['type']}): {rec['title']}"
        if line not in read_front(ledger.track_path(track["name"]))[1]:
            _track_log(ledger, track, line)
        else:
            ledger.save_track(track)
    for hid, state in verdict.get("hypotheses", {}).items():
        if state == "open":
            ledger.log_hypothesis(hid, f"{eid} ({decision}): left as it is")
        else:
            ledger.set_hypothesis(hid, state, f"{eid} ({decision})")
    verdict["track_head"] = {"before": verdict.get("track_head", {}).get("before", track["head_commit"]), "after": track["head_commit"]}
    rec["status"] = "judged"
    ledger.save_record(rec)
    finish(ledger, f"{eid} {decision}")


@contextlib.contextmanager
def writing(ledger: Ledger, wait: float = 120.0) -> Generator[None, None, None]:
    """The ledger lock, after finishing any verdict an earlier command left half applied."""
    with ledger.locked(wait):
        for rec in ledger.experiments():
            if rec["status"] == "judging":
                _finish_judge(ledger, rec)
        yield


# --- setup ------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if ledger.exists():
        raise ArlError(f"{ledger.config_path} already exists")
    for sub in ("eval", "hypotheses", "tracks", "experiments", "introspections"):
        (ledger.dir / sub).mkdir(parents=True, exist_ok=True)
    branch = ledger.git.branch() or "main"
    files = {
        ledger.program_path: render.template("PROGRAM.md"),
        ledger.knowledge_path: render.template("KNOWLEDGE.md"),
        ledger.lessons_path: render.template("LESSONS.md"),
        ledger.ideas_path: render.template("IDEAS.md"),
        ledger.dir / ".gitignore": render.template("gitignore"),
        ledger.dir / "eval" / "README.md": render.template("eval_README.md"),
        ledger.config_path: render.template("arl.toml").replace("@NAME@", ledger.home.name).replace("@BRANCH@", branch),
    }
    kept = []
    for path, text in files.items():
        if path.exists():
            kept.append(path.name)  # never overwrite what is already there
        else:
            write_text(path, text)
    print(f"Created {ledger.dir}" + (f" (left as they were: {', '.join(kept)})" if kept else ""))
    print("Next: write PROGRAM.md and arl.toml, put the evaluator under eval/, say what is already")
    print("known about the problem in KNOWLEDGE.md, then")
    print(f"  {ARL} freeze --reason \"initial\"")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    person = interactive()
    old = contract.load(ledger) if ledger.exists() else None
    shown = None
    if old and person and not args.yes:
        # Ask before taking the ledger lock, so a run that ends meanwhile can write.
        # The answer covers this list only: it is compared again under the lock.
        shown = contract.problems(ledger)
        print("Changes since contract v%d:" % old["version"])
        for line in shown:
            print(f"  {line}")
        if input(f"Freeze these as contract v{old['version'] + 1}? [y/N] ").strip().lower() != "y":
            raise ArlError("not frozen")
    with ledger.locked():
        before = contract.load(ledger)
        try:
            runner.probe_env(ledger)  # a contract that names an environment nothing can run in is not frozen
        except ArlError as e:
            raise ArlError(f"{e}\nNothing was frozen. Name an environment that is on this machine, with those packages in it.") from e
        lock = contract.freeze(ledger, args.reason, person, shown)
        try:
            write_text(ledger.status_path, render.status(ledger))
            ledger.git.commit_paths(f"{ledger.cfg.commit_prefix}: freeze contract v{lock['version']}", contract.commit_paths(ledger))
        except BaseException:
            contract.unfreeze(ledger, before)  # a freeze that is not in git did not happen
            raise
    print(f"Contract v{lock['version']} frozen ({len(lock['files'])} files).")
    if old and old["eval_digest"] != lock["eval_digest"]:
        print("What a score depends on changed (the evaluator, the metrics, the commands or the environment): every track needs a new baseline.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if args.json:
        print(json.dumps(render.state(ledger), indent=2, default=str))
    else:
        print(render.status(ledger), end="")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    data = env = None
    if ledger.exists():
        ledger.check_files()  # a damaged record is the first thing to say
        data, env = _data_digest_or_none(ledger), _env_or_problem(ledger)
    action = rules.next_action(ledger, data, env)
    if args.json:
        print(json.dumps(action, indent=2))
    else:
        print(render.next_text(ledger, action), end="")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check that the ledger, the contract and git still agree."""
    ledger = Ledger.locate()
    ledger.check_files()  # a record that cannot be read is reported on its own, first
    bad = list(contract.problems(ledger))
    if not bad:
        left = contract.uncommitted(ledger)
        if left:
            bad.append("the frozen contract is not committed: " + ", ".join(left[:6]) + ". A person runs 'arl freeze' again, or commits these files.")
        env = _env_or_problem(ledger)
        if env and env.get("problem"):
            bad.append("the environment: " + env["problem"].splitlines()[0])
    git = ledger.git
    for t in ledger.tracks():
        if not git.branch_exists(t["branch"]):
            bad.append(f"track {t['name']}: branch {t['branch']} is gone ('arl track worktree {t['name']}' makes it again where the ledger says it stood)")
        elif t["status"] == "active" and git.head(t["branch"]) != t["head_commit"]:
            bad.append(f"track {t['name']}: branch moved outside arl (run 'arl track sync {t['name']}')")
        try:
            ledger.track_params(t)
        except ArlError as e:
            bad.append(f"track {t['name']}: {str(e).splitlines()[0]}")
        if t.get("head_experiment"):
            try:
                head = ledger.record(t["head_experiment"])
                if not rules.kept(head) and head["status"] != "judging":  # a verdict cut off is named below
                    bad.append(f"track {t['name']}: its reference {t['head_experiment']} was not kept")
            except ArlError as e:
                bad.append(f"track {t['name']}: {str(e).splitlines()[0]}")
    for folder in sorted(ledger.exp_root.glob("E[0-9][0-9][0-9][0-9]*")) if ledger.exp_root.exists() else []:
        if not (folder / "record.json").exists():
            bad.append(f"{ledger.rel(folder)} has no record.json: a leftover, not an experiment")
    for e in ledger.experiments():
        exp = ledger.exp_dir(e["id"])
        if e["status"] == "judging":
            bad.append(f"{e['id']}: its verdict ({e['verdict']['decision']}) was cut off before it was complete. '{ARL} exp judge {e['id']}' finishes it.")
        if "design" in e:
            try:
                design_hash, params_hash = _design_hashes(exp)
                if design_hash != e["design"]["design_sha256"]:
                    bad.append(f"{e['id']}: design.md changed after the run started")
                if params_hash != e["design"]["params_sha256"]:
                    bad.append(f"{e['id']}: params.json changed after the run started")
            except ArlError as err:
                bad.append(f"{e['id']}: {str(err).splitlines()[0]}")
        if e.get("code_commit") and not git.commit_exists(e["code_commit"]):
            bad.append(f"{e['id']}: commit {e['code_commit'][:10]} is not in the repository")
        if rules.kept(e) and e["type"] == "feature":
            track = ledger.track(e["track"])
            if git.branch_exists(track["branch"]) and not git.is_ancestor(e["code_commit"], track["branch"]):
                bad.append(f"{e['id']}: kept, but its commit is not in {track['branch']}")
        if "metrics" in e:
            try:
                stored = read_json(exp / "metrics.json") if (exp / "metrics.json").exists() else {}
            except (ArlError, json.JSONDecodeError):
                stored = {}
            if any(stored.get(k) != v for k, v in e["metrics"].items()):
                bad.append(f"{e['id']}: metrics.json differs from the record")
    bad += ledger.idea_problems()
    if bad:
        print("\n".join(bad))
        return 1
    print("ok")
    return 0


# --- pausing the loop, and loading what it knows --------------------------------------


def _ask_runs_to_pause(ledger: Ledger, only: str | None = None) -> list[tuple[str, str]]:
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


def _say_asked(ledger: Ledger, asked: list[tuple[str, str]]) -> None:
    grace = f"{ledger.cfg.pause_grace:.0f} s"
    for eid, kind in asked:
        if kind == "run":
            print(f"{eid} was asked to save its state and stop; it has {grace} before it is forced.")
            print(f"  It continues later, from what it saved:  {ARL} exp run {eid}")
        else:
            print(f"The rerun of {eid} was asked to stop; it has {grace}. A rerun is not continued: run it again later.")


def cmd_pause(args: argparse.Namespace) -> int:
    """Stop the loop where it is, to be continued later."""
    ledger = Ledger.locate()
    with ledger.locked():
        state = ledger.paused()
        if state is not None:
            if not args.note:
                raise ArlError(
                    f"the loop is paused already. To add to the note for whoever continues: {ARL} pause --note \"...\"\n"
                    f"To continue, when the person says so: {ARL} resume"
                )
            state["note"] = f"{state['note']} {args.note}" if state.get("note") else args.note
            write_json(ledger.paused_path, state)
            ledger.log_activity("paused", f"note added: {args.note}")
            if ledger.lock_path.exists():
                finish(ledger, "pause, note added")
            print("The note is kept with the pause and printed again at 'arl resume'.")
            return 0
        # A stop is never refused for a missing word: what is not said is recorded as such.
        reason = args.reason or "not given"
        state = {"at": now(), "by": AT_TERMINAL if interactive() else "the agent", "reason": reason, "note": args.note or ""}
        write_json(ledger.paused_path, state)
        asked = _ask_runs_to_pause(ledger)
        ledger.log_activity("paused", f"{reason}. {args.note or ''}".strip())
        if ledger.lock_path.exists():
            finish(ledger, "pause")
    print("The loop is paused. Nothing starts, runs or is judged until 'arl resume'.")
    _say_asked(ledger, asked)
    if not args.note:
        print("No note was left. If you were in the middle of something, say where, now. The next session starts")
        print(f"from the files and that note:  {ARL} pause --note \"where I was, what comes next\"")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Continue a paused loop: print what was left behind and what there is to read again."""
    ledger = Ledger.locate()
    with ledger.locked():
        state = ledger.paused()
        if state is None:
            raise ArlError("the loop is not paused")
        if state.get("by") == AT_TERMINAL and not interactive():
            raise ArlError(
                "The loop was paused by a person at a terminal, and it is continued the same way:\n"
                f"they run '{ARL} resume' there. Until then nothing starts, runs or is judged.\n"
                "Notes, ideas and lessons can still be written down."
            )
        ledger.paused_path.unlink()
        ledger.log_activity("resumed", f"paused {state.get('at', '')} ({state.get('reason', '')}). Note: {state.get('note', '')}")
        _mark_reloaded(ledger)
        if ledger.lock_path.exists():
            finish(ledger, "resume")
    print(render.reload_text(ledger, state, _env_note(ledger)), end="")
    print()
    print(render.next_text(ledger, _next(ledger)), end="")
    return 0


def _mark_reloaded(ledger: Ledger) -> str | None:
    nid = rules.introspection_state(ledger)["reload"]
    if nid:
        meta, body = ledger.introspection(nid)
        meta["reloaded"] = now()
        write_front(ledger.intro_path(nid), meta, body)
    ledger.mark_reloaded()
    return nid


def cmd_reload(args: argparse.Namespace) -> int:
    """Print the knowledge and the lessons as they are now. Part 4 of an introspection."""
    ledger = Ledger.locate()
    with ledger.locked():
        nid = _mark_reloaded(ledger)
        if nid:
            finish(ledger, f"{nid} reloaded")
    print(render.reload_text(ledger, env=_env_note(ledger)), end="")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    """Write down, when it happens, something an introspection should take up."""
    ledger = Ledger.locate()
    if not ledger.exists():
        raise ArlError(f"no {ledger.config_path}. Run 'arl init' in the project first.")
    text = one_line(" ".join(args.text), "the note")
    ledger.log_activity("note", text, about=args.about)
    where = "part 2 (how the work went)" if args.about == "work" else "part 3 (what we know about the problem)"
    print(f"Noted for the next introspection, {where}.")
    return 0


def cmd_lesson_add(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        lid = ledger.add_lesson(args.scope, args.title, args.body, args.source or "")
        path = ledger.lessons_file(args.scope)
        if args.scope == "project" and ledger.lock_path.exists():
            finish(ledger, f"lesson {lid}")
        # A general lesson is not committed anywhere. Its file sits next to the skill, on this
        # machine only: what a loop writes in one project never goes into the arl repository.
    print(f"{lid} added to {path}")
    return 0


def cmd_lesson_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[l["id"], scope, l["title"]] for scope in SCOPES if args.scope in (None, scope) for l in ledger.lessons(scope)]
    print(render.table(["id", "scope", "title"], rows), end="")
    return 0


# --- tracks -----------------------------------------------------------------------


def cmd_track_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    name = args.name
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name):
        raise ArlError("a track name is lower case letters, digits and dashes")
    with writing(ledger):
        contract.require(ledger)
        _gate_paused(ledger)
        if ledger.track_path(name).exists():
            raise ArlError(f"track {name} already exists")
        opened_by = _authorized(ledger, args.introspection, ("branch", "pivot"), "a new track")
        git, branch = ledger.git, f"track/{name}"
        source = None
        if args.from_ref and re.fullmatch(r"E\d{4}", args.from_ref):
            # An experiment: the track starts at the code it ran and, unless told otherwise, its params.
            source = ledger.record(args.from_ref)
            if source["type"] not in rules.SCORED:
                raise ArlError(f"{source['id']} is a diagnose experiment: its code observes, it is not a method. Name a commit if you mean it.")
            if not source.get("code_commit") or "design" not in source:
                raise ArlError(f"{source['id']} never ran, so there is no code to start from")
            _require_design_unchanged(ledger, source)
            start = git.head(source["code_commit"])
            params = _load_params(Path(args.params)) if args.params else _load_params(ledger.exp_dir(source["id"]) / "params.json")
        else:
            start = git.head(args.from_ref or "HEAD")
            params = _load_params(Path(args.params)) if args.params else {}
        path = ledger.worktree_path(name)
        # A branch or a worktree without a track file is what an earlier try left when it was cut off.
        if git.branch_exists(branch) and git.head(branch) != start:
            raise ArlError(f"branch {branch} exists already and is not at {start[:10]}. Pick another name.")
        registered = {w["path"]: w.get("branch") for w in git.worktrees()}
        if path.exists() and registered.get(str(path)) != branch:
            raise ArlError(f"{path} exists already and is not a worktree of {branch}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not git.branch_exists(branch):
            git.create_branch(branch, start)
        if not path.exists():
            git.prune_worktrees()
            git.add_worktree(path, branch)
        ledger.track_root.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "status": "active",
            "branch": branch,
            "created": now(),
            "from_ref": args.from_ref or git.branch() or "HEAD",
            "base_commit": start,
            "head_commit": start,
            "head_experiment": "",
            "opened_by": opened_by,
        }
        ledger.write_track_params(meta, params)
        body = render.fill(render.template("track.md"), name=name, branch=branch, worktree=path)
        write_front(ledger.track_path(name), meta, body)
        finish(ledger, f"open track {name}")
    print(f"Track {name}: branch {branch} at {start[:10]}, worktree {path}")
    if source:
        print(f"It starts from {source['id']}: the code it ran and {'the params given' if args.params else 'its params'}. Its baseline should give the result of {source['id']} again.")
    print(f"Now write {ledger.track_path(name)} (approach, premises, stop if), below the +++ block.")
    return 0


def _track_log(ledger: Ledger, meta: dict[str, Any], line: str) -> None:
    body = read_front(ledger.track_path(meta["name"]))[1]
    ledger.save_track(meta, body.rstrip("\n") + f"\n- {now()[:10]}: {' '.join(line.split())}\n")


def cmd_track_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with writing(ledger):
        contract.require(ledger)
        _gate_paused(ledger)
        track = ledger.track(args.name)
        if track["status"] == args.status:
            raise ArlError(f"track {args.name} is already {args.status}")
        flying = rules.in_flight(ledger, args.name)
        if args.status != "active" and flying:
            raise ArlError(f"{flying[0]['id']} is still in flight on track {args.name}")
        by = _authorized(ledger, args.introspection, (), "parking, closing or reopening a track")
        track["status"] = args.status
        _track_log(ledger, track, f"{args.status} (by {by}). {args.note}")
        finish(ledger, f"track {args.name} {args.status}")
    print(f"Track {args.name} is {args.status}.")
    return 0


def cmd_track_sync(args: argparse.Namespace) -> int:
    """The branch was changed by hand. Record the new head; the track needs a new baseline."""
    ledger = Ledger.locate()
    with writing(ledger):
        contract.require(ledger)
        _gate_paused(ledger)
        track = ledger.track(args.name)
        if rules.in_flight(ledger, args.name):
            raise ArlError(f"an experiment is in flight on track {args.name}")
        head = ledger.git.head(track["branch"])
        if head == track["head_commit"]:
            raise ArlError("the branch did not move")
        old = track["head_commit"]
        track["head_commit"], track["head_experiment"] = head, ""
        _track_log(ledger, track, f"branch moved outside arl from {old[:10]} to {head[:10]}. {args.note}")
        finish(ledger, f"track {args.name} synced to {head[:10]}")
    print(f"Track {args.name} now at {head[:10]}. It needs a new baseline.")
    return 0


def cmd_track_worktree(args: argparse.Namespace) -> int:
    """Put a track's worktree back when its folder is gone."""
    ledger = Ledger.locate()
    with ledger.locked():
        track = ledger.track(args.name)
        path = ledger.worktree_path(args.name)
        if path.exists():
            raise ArlError(f"{path} is there already")
        flying = [e for e in rules.in_flight(ledger, args.name) if e["status"] != "judging"]
        branch = flying[0]["branch"] if flying else track["branch"]
        git = ledger.git
        # A clone of the project brings the commits and not the local branches. The ledger
        # says where each one stood; an experiment's own branch may be further on where
        # the project was cloned from.
        wanted = {track["branch"]: track["head_commit"]}
        if flying and branch != track["branch"]:
            there = f"refs/remotes/origin/{branch}"
            wanted[branch] = git.head(there) if git.ok("rev-parse", "--verify", "--quiet", there) else flying[0].get("code_commit") or flying[0]["base_commit"]
        missing = {name: commit for name, commit in wanted.items() if not git.branch_exists(name)}
        for name, commit in missing.items():
            if not git.commit_exists(commit):
                raise ArlError(f"branch {name} is not in this repository, and neither is the commit it stood at ({commit[:10]}). Fetch it from where the project came from.")
        for name, commit in missing.items():
            git.create_branch(name, commit)
        git.prune_worktrees()  # git still lists the folder that is gone
        path.parent.mkdir(parents=True, exist_ok=True)
        git.add_worktree(path, branch)
    for name, commit in missing.items():
        print(f"Branch {name} was not in this repository: made again at {commit[:10]}, where the ledger says it stood.")
    print(f"Track {args.name}: worktree {path} is back, on {branch}.")
    return 0


def cmd_track_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    print(render.track_table(ledger, contract.load(ledger)), end="")
    for t in ledger.tracks():
        print(f"{t['name']}: {ledger.worktree_path(t['name'])}")
    return 0


# --- hypotheses and ideas ---------------------------------------------------------


def cmd_hyp_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        hid = ledger.next_id("H")
        ledger.hyp_root.mkdir(parents=True, exist_ok=True)
        path = ledger.hyp_root / f"{hid}-{slugify(args.title)}.md"
        source = one_line(args.source, "--from") if args.source else ""
        meta = {"id": hid, "title": args.title, "status": "open", "created": now(), "updated": now(), "source": source}
        write_front(path, meta, render.fill(render.template("hypothesis.md"), id=hid, title=args.title))
    print(f"{hid}: write {path} (below the +++ block, which is arl's)")
    return 0


def cmd_hyp_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        ledger.set_hypothesis(args.id, args.status, args.note)
        finish(ledger, f"{args.id} {args.status}")
    print(f"{args.id} is {args.status}.")
    return 0


def cmd_hyp_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[h["id"], h["status"], h["title"]] for h in ledger.hypotheses() if not args.status or h["status"] == args.status]
    print(render.table(["id", "status", "title"], rows), end="")
    return 0


def cmd_idea_add(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        iid = ledger.add_idea(args.title, args.source or "", args.kind, args.body)
        if ledger.lock_path.exists():
            finish(ledger, f"idea {iid}")
    print(f"{iid} added to {ledger.ideas_path}")
    return 0


def cmd_idea_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        ledger.set_idea(args.id, args.status, args.note)
        finish(ledger, f"idea {args.id} {args.status}")
    print(f"{args.id} is {args.status}.")
    return 0


def cmd_idea_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[i["id"], i["status"], i["title"]] for i in ledger.ideas() if not args.status or i["status"] == args.status]
    print(render.table(["id", "status", "title"], rows), end="")
    return 0


# --- experiments ------------------------------------------------------------------


def cmd_exp_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    typ, hyps = args.type, [check_id("H", h) for h in (args.hyp or [])]
    with writing(ledger):
        lock = contract.require(ledger)
        cfg = ledger.cfg
        _gate_new_work(ledger)
        probed = _env(ledger)
        track = ledger.track(args.track)
        if track["status"] != "active":
            raise ArlError(f"track {args.track} is {track['status']}")
        require_filled(ledger.track_path(args.track), f"the description of track {args.track}")
        flying = rules.in_flight(ledger, args.track)
        if flying:
            raise ArlError(
                f"{flying[0]['id']} is still in flight on track {args.track} ({flying[0]['status']}). "
                "One experiment at a time per track: finish it first."
            )
        path, wgit = _worktree(ledger, track)
        _park(ledger, track, path, wgit)
        dirty = wgit.dirty()
        if dirty:
            raise ArlError(f"{path} has uncommitted changes:\n  " + "\n  ".join(dirty[:10]))
        head = wgit.head()
        if head != track["head_commit"]:
            raise ArlError(
                f"branch {track['branch']} moved outside arl ({track['head_commit'][:10]} -> {head[:10]}).\n"
                f"Record it with '{ARL} track sync {args.track} --note \"...\"', then run a baseline."
            )
        params = ledger.track_params(track)
        if typ == "baseline" and hyps:
            raise ArlError("a baseline measures the track as it is. It tests no hypothesis: leave --hyp out.")
        for h in hyps:
            meta, _ = ledger.hypothesis(h)
            require_filled(ledger.hyp_path(h), f"hypothesis {h}")
            if meta["status"] == "dropped":
                raise ArlError(f"{h} was dropped")
        if typ in ("tune", "feature"):
            if not hyps:
                raise ArlError(f"a {typ} experiment tests a hypothesis: write one with 'arl hyp new' and pass --hyp")
            data = _data(ledger)[1]
            if rules.reference_for(ledger, track, lock["eval_digest"], data, (probed or {}).get("digest")) is None:
                raise ArlError(f"{rules.why_no_reference(ledger, track, lock, data, probed)}: run a baseline first")
        if args.command and typ != "diagnose":
            raise ArlError("--command is for diagnose experiments. Scored runs use the command in arl.toml.")

        eid, slug = ledger.next_id("E"), slugify(args.title)
        exp = ledger.exp_root / f"{eid}-{slug}"
        branch = f"exp/{eid}-{slug}" if typ in ("feature", "diagnose") else track["branch"]
        if exp.exists():
            raise ArlError(f"{exp} exists already: a leftover. Remove the folder.")
        leftover = branch != track["branch"] and ledger.git.branch_exists(branch)
        if leftover and ledger.git.head(branch) != head:
            raise ArlError(f"branch {branch} exists already with commits of its own. Pick another title.")
        rec = {
            "id": eid,
            "title": args.title,
            "type": typ,
            "track": args.track,
            "hypotheses": hyps,
            "status": "draft",
            "created": now(),
            "base_commit": head,
            "branch": branch,
            "command": args.command or "",
            "attempts": [],
        }
        ctx = {
            "id": eid,
            "title": args.title,
            "type": typ,
            "track": args.track,
            "hyps": ", ".join(hyps) or "no hypothesis",
            "primary": cfg.primary.name,
            "change_hint": "params only" if typ == "tune" else "code, and params if the code needs them",
        }
        kind = typ if typ in ("baseline", "diagnose") else "scored"
        design = render.fill(render.template(f"design_{kind}.md"), **ctx)
        # Everything is checked. From here on, undo on failure.
        if "params_sha256" not in track:  # a track from before the hash was kept: trust it once
            ledger.write_track_params(track, params)
            ledger.save_track(track)
        exp.mkdir(parents=True)
        try:
            write_text(exp / "design.md", design)
            write_json(exp / "params.json", params)
            write_json(exp / "record.json", rec)
            if branch != track["branch"]:
                if leftover:
                    wgit.switch(branch)  # an empty branch a cut-off try left: use it
                else:
                    wgit.switch_new(branch, head)
        except BaseException:
            shutil.rmtree(exp, ignore_errors=True)
            with contextlib.suppress(ArlError):
                if wgit.branch() != track["branch"] and not wgit.dirty():
                    wgit.switch(track["branch"])
            raise
        write_text(ledger.status_path, render.status(ledger))
    print(render.next_text(ledger, {"stage": "design", "target": eid, "why": f"{eid} drafted on track {args.track}"}), end="")
    return 0


def _blob_id(path: Path) -> str:
    """The id git gives this file's content (sha1 object format)."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def cmd_exp_commit(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with writing(ledger):
        _gate_paused(ledger)
        rec = ledger.record(args.id)
        if rec["type"] not in ("feature", "diagnose"):
            raise ArlError(f"{rec['id']} is a {rec['type']} experiment: it does not change code")
        _settle(ledger, rec)
        if rec["status"] == "paused":
            raise ArlError(
                f"{rec['id']} is paused in the middle of its run, and a run continues the code it started with.\n"
                f"Continue it as it is:  {ARL} exp run {rec['id']}\n"
                f"Or give that run up, so the code can change and the next run starts from nothing:\n"
                f"  {ARL} exp fail {rec['id']} --reason \"...\""
            )
        if rec["status"] not in ("draft", "crashed"):
            raise ArlError(f"{rec['id']} is {rec['status']}: its code is fixed")
        path, wgit = _exp_worktree(ledger, rec)
        dirty = wgit.dirty()
        if not dirty:
            raise ArlError(f"nothing to commit in {path}")
        # research/ belongs to the home checkout. The only change allowed there is one
        # that puts a file back to what the track started from.
        base, wrong = rec["base_commit"], []
        for rel in (p for p in dirty if p.startswith(f"{ledger.name}/")):
            before, here = wgit.blob(base, rel), path / rel
            same = (not here.exists() and before is None) or (here.is_file() and before == _blob_id(here))
            if not same:
                wrong.append(rel)
        if wrong:
            raise ArlError(
                f"{ledger.name}/ belongs to the home checkout ({ledger.home}), not to a track worktree.\n"
                "Undo these changes first:\n  " + "\n  ".join(wrong[:10])
            )
        sha = wgit.commit_all(f"{rec['id']}: {one_line(args.message, 'the message')}")
    print(f"{rec['id']}: committed {sha[:10] if sha else ''} on {rec['branch']} ({len(dirty)} paths)")
    return 0


def _run_env(ledger: Ledger, rec: dict[str, Any], values: dict[str, Any], resume: bool) -> dict[str, str]:
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


def cmd_exp_run(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    # The run lock is held from the checks to the end: no second run can start in
    # between, and whatever a record still calls "running" is a leftover.
    with runner.run_lock(ledger):
        with writing(ledger):
            lock = contract.require(ledger)
            _settle_all(ledger)
            rec = ledger.record(eid)
            cfg = ledger.cfg
            if rec["status"] not in ("draft", "crashed", "paused"):
                raise ArlError(f"{eid} is {rec['status']}. To run it again use '{ARL} exp reproduce {eid}'.")
            resuming = rec["status"] == "paused"
            attempts = rec["attempts"]
            if not resuming and len(attempts) >= cfg.max_attempts:
                raise ArlError(
                    f"{eid} has failed {len(attempts)} times, the limit. Judge it:\n"
                    f"  {ARL} exp judge {eid} --verdict crash --reason \"...\""
                )
            _gate_running(ledger, first_attempt=not attempts)
            exp = ledger.exp_dir(eid)
            require_filled(exp / "design.md", f"the design of {eid}")
            params = _load_params(exp / "params.json")
            for h in rec["hypotheses"]:
                require_filled(ledger.hyp_path(h), f"hypothesis {h}")
            track = ledger.track(rec["track"])
            path, wgit = _exp_worktree(ledger, rec)
            typ = rec["type"]
            dirty = wgit.dirty()
            if dirty:
                how = f"Commit them with '{ARL} exp commit {eid} -m \"...\"' or undo them." if typ in ("feature", "diagnose") and not resuming else "Undo them."
                raise ArlError(
                    f"{path} has uncommitted changes, so the run would not be the committed code:\n  "
                    + "\n  ".join(dirty[:10])
                    + f"\n{how}"
                )
            code, base = wgit.head(), rec["base_commit"]
            give_up = f"Judge it as a crash ('{ARL} exp judge {eid} --verdict crash --reason \"...\"') and draft it again."
            again = give_up if attempts else f"Abandon it ('{ARL} exp abandon {eid} --reason \"...\"') and draft again."
            fingerprint, data = _data(ledger)
            probed = _env(ledger)
            env_digest = (probed or {}).get("digest")
            if resuming:
                if code != rec["code_commit"]:
                    raise ArlError(f"{path} is at {code[:10]}, but {eid} was paused while running {rec['code_commit'][:10]}. A run continues the code it started with. {give_up}")
                if lock["digest"] != rec["contract"]["digest"]:
                    raise ArlError(f"the contract was frozen anew since {eid} started (v{rec['contract']['version']} to v{lock['version']}), so it cannot be continued. {give_up}")
                if rec["conditions"].get("data_digest") not in (None, data):
                    raise ArlError(f"the data files changed since {eid} started, so it cannot be continued. {give_up}")
                if env_digest is not None and rec["conditions"].get("env_digest") != env_digest:
                    changes = "; ".join(runner.env_changes(rec["conditions"].get("env"), probed)) or "see its record"
                    raise ArlError(f"the packages changed since {eid} started ({changes}), so it cannot be continued. {give_up}")
            elif typ in ("baseline", "tune"):
                if code != track["head_commit"]:
                    raise ArlError(f"track {rec['track']} moved since {eid} was drafted. {again}")
            else:
                if not wgit.is_ancestor(base, code):
                    raise ArlError(f"{rec['branch']} no longer contains the commit it started from ({base[:10]})")
                if typ == "feature" and code == base:
                    raise ArlError(f"no code change is committed on {rec['branch']}. Edit, then '{ARL} exp commit {eid} -m \"...\"'.")
                touched = wgit.changed(base, code, ledger.name)
                if touched:
                    raise ArlError(
                        f"the code of {eid} changes {ledger.name}/, which belongs to the home checkout:\n  "
                        + "\n  ".join(touched[:10])
                        + f"\nPut those files back in the worktree, then '{ARL} exp commit {eid} -m \"...\"'."
                    )
            if typ == "baseline" and canonical(params) != canonical(ledger.track_params(track)):
                raise ArlError(
                    f"a baseline measures the track as it is, but {exp / 'params.json'} differs from the track's params.\n"
                    f"Put it back, or make the change a tune experiment. {again}"
                )
            design_hash, params_hash = _design_hashes(exp)
            if "design" in rec:
                _require_design_unchanged(ledger, rec)
            scored = typ in rules.SCORED
            split = cfg.splits[0]
            command = rec.get("command") or cfg.run_command
            # The files, not the commit: the same change committed twice is the same experiment.
            what = {"track": rec["track"], "tree": wgit.tree(code), "params": params_hash, "command": command, "eval": lock["eval_digest"] if scored else "", "split": split, "data": data}
            if env_digest is not None:
                what["env"] = env_digest  # the same run with other packages is not a known result
            key = sha256_text(canonical(what))
            reference = rules.reference_for(ledger, track, lock["eval_digest"], data, env_digest)
            repeat = typ == "baseline" and reference is not None  # the track has a reference: this one shows the noise
            if not resuming:
                for other in ledger.experiments():
                    same = other["id"] != eid and other.get("dedup") == key and other["status"] in ("evaluated", "judging", "judged")
                    if same and other.get("verdict", {}).get("decision") != "crash":
                        if not args.allow_duplicate:
                            raise ArlError(
                                f"{eid} has the same code, params, data and evaluator as {other['id']}: the result is already known.\n"
                                "To repeat it on purpose (to see the noise), pass --allow-duplicate \"why\"."
                            )
                        rec["duplicate_of"], rec["duplicate_reason"] = other["id"], args.allow_duplicate
            if typ in ("tune", "feature") and reference is None:
                raise ArlError(f"{rules.why_no_reference(ledger, track, lock, data, probed)}. {again} Then run a baseline first.")
            reasons = runner.busy(cfg)
            if reasons:
                raise ArlError(
                    "The machine is busy, not starting:\n  " + "\n  ".join(reasons) + "\nWait and try again. Leave other jobs alone.",
                    EXIT_BUSY,
                )

            # All checks passed. Mark it as running and lock the design; if that
            # cannot be committed, nothing ran and nothing is counted.
            before = json.dumps(rec)
            try:
                if resuming:
                    attempt = attempts[-1]
                    attempt.pop("paused", None)
                else:
                    attempt = {"n": len(attempts) + 1, "started": now(), "code_commit": code, "segments": []}
                    attempts.append(attempt)
                    rec["contract"] = {k: lock[k] for k in ("version", "digest", "eval_digest")}
                    rec["conditions"] = {"command": command, "split": split, "worktree": str(path), "data": fingerprint, "data_digest": data}
                    if env_digest is not None:
                        rec["conditions"]["env_digest"] = env_digest
                n = attempt["n"]
                part = {"started": now()}
                attempt.setdefault("segments", []).append(part)
                # A pause is asked of a record that says "running": clear an old request before it does.
                (exp / "logs" / "pause.request").unlink(missing_ok=True)
                rec.update(status="running", pid=os.getpid(), pid_start=runner.proc_start(os.getpid()), code_commit=code, dedup=key)
                rec.setdefault("design", {"design_sha256": design_hash, "params_sha256": params_hash, "locked_at": now()})
                part["env"] = runner.environment(ledger, probed)
                rec["conditions"].setdefault("env", part["env"])
                ledger.save_record(rec)
                parts = len(attempt["segments"])
                finish(ledger, f"{eid} continued, part {parts}" if parts > 1 else (f"{eid} design, {typ} on {rec['track']}" if n == 1 else f"{eid} attempt {n}"))
            except BaseException:
                ledger.save_record(json.loads(before))
                raise

        out, logs = exp / "artifacts" / f"run-{n}", exp / "logs"
        out.mkdir(parents=True, exist_ok=True)
        values = {"params": exp / "params.json", "out": out, "split": split, "home": ledger.home, "research": ledger.dir, "exp": exp, "worktree": path, "metrics": exp / "metrics.json"}
        env = _run_env(ledger, rec, values, resume=len(attempt["segments"]) > 1)
        limits = {"max_rss_gb": cfg.max_run_rss_gb, "min_free_ram_gb": cfg.stop_below_free_ram_gb}
        pausing = {"pause_file": logs / "pause.request", "pause_grace": cfg.pause_grace}
        failed = paused = ""
        clock = time.monotonic()

        def started(pid: int) -> None:
            rec["child"] = runner.child_ref(pid)  # so a later arl can stop the job if this one is killed
            ledger.save_record(rec)

        def close_part(how: str, seconds: float) -> None:
            part.update(ended=now(), ended_by=how, seconds=round(part.get("seconds", 0) + seconds, 2))
            attempt["run_seconds"] = round(sum(s.get("seconds", 0) for s in attempt["segments"]), 2)

        try:
            if not attempt.get("run_done"):
                used = sum(s.get("seconds", 0) for s in attempt["segments"][:-1])
                left = cfg.run_timeout - used
                if left <= 0:
                    close_part("failed", 0)
                    failed = f"the run passed its limit of {cfg.run_timeout:.0f} s over all its parts"
                else:
                    res = runner.execute(runner.substitute(command, values), path, env, left, logs / f"run-{n}.log", on_start=started, **limits, **pausing)
                    attempt["exit_code"] = res["exit_code"]
                    write_text(exp / "run.tail.txt", runner.tail(logs / f"run-{n}.log"))
                    saved = runner.last_checkpoint(out)
                    if saved:
                        attempt["last_checkpoint"] = dt.datetime.fromtimestamp(saved).astimezone().isoformat(timespec="seconds")
                    elif cfg.checkpoint_every and res["seconds"] > 3 * cfg.checkpoint_every:
                        attempt["warning"] = f"this part ran {res['seconds']:.0f} s and saved no state under {{out}}/checkpoint: a pause loses that work"
                    if res["paused"]:
                        paused = res["paused"]
                        close_part("paused", res["seconds"])
                    elif res["stopped"]:
                        failed = f"the run was stopped: {res['stopped']}"
                    elif res["exit_code"] != 0:
                        failed = f"the run exited with code {res['exit_code']}"
                    elif wgit.branch() != rec["branch"] or wgit.head() != code or wgit.tracked_changes():
                        failed = "the code in the worktree changed while the run was going, so the result does not belong to the recorded commit"
                    if failed:
                        close_part("failed", res["seconds"])
                    elif not paused:
                        close_part("finished", res["seconds"])
                        attempt["run_done"] = True
            if not failed and not paused:
                rec["artifacts"] = runner.manifest(out)  # what the run left, before the evaluator adds to it
                if scored and not [a for a in rec["artifacts"] if not a["path"].startswith("checkpoint/")]:
                    failed = "the run wrote nothing into its output folder"
            if not failed and not paused and scored:
                lock_now = contract.require(ledger)  # the run itself must not have touched the evaluator
                if lock_now["digest"] != rec["contract"]["digest"]:
                    failed = f"the contract was frozen anew while the run was going (v{rec['contract']['version']} to v{lock_now['version']}), so this run cannot be scored"
            if not failed and not paused and scored:
                (exp / "metrics.json").unlink(missing_ok=True)
                ev = runner.execute(runner.substitute(cfg.eval_command, values), ledger.home, env, cfg.eval_timeout, logs / f"eval-{n}.log", on_start=started, **limits, **pausing)
                attempt["eval_seconds"] = round(attempt.get("eval_seconds", 0) + ev["seconds"], 2)
                write_text(exp / "eval.tail.txt", runner.tail(logs / f"eval-{n}.log"))
                if ev["paused"]:
                    paused = f"{ev['paused']} (during the evaluation; the run itself is complete)"
                elif ev["stopped"]:
                    failed = f"the evaluator was stopped: {ev['stopped']}"
                elif ev["exit_code"] != 0:
                    failed = f"the evaluator exited with code {ev['exit_code']}"
                elif contract.require(ledger)["digest"] != rec["contract"]["digest"]:
                    failed = "the contract was frozen anew while the evaluator was running, so this score cannot be trusted"
                else:
                    rec["metrics"] = runner.read_metrics(exp / "metrics.json", cfg)
                    rec["comparison"] = rules.compare(cfg, rec["metrics"], reference, repeat=repeat)
        except ArlError as e:
            failed = str(e)
            if "ended" not in part:
                close_part("failed", time.monotonic() - clock)
        except BaseException:
            # arl itself is being stopped (a closed terminal, Ctrl-C). The job was given the
            # time to save its state. This is an interruption, not a failure.
            if "ended" not in part:
                close_part("interrupted", time.monotonic() - clock)
            _pause_run(rec, "interrupted: arl was stopped while the run was going")
            ledger.save_record(rec)
            with contextlib.suppress(ArlError):
                with ledger.locked():
                    finish(ledger, f"{eid} interrupted")
            raise

        def write_outcome() -> str:
            attempt["ended"] = now()
            for key in ("pid", "pid_start", "child"):
                rec.pop(key, None)
            if failed:
                _fail(rec, failed)
                ledger.save_record(rec)
                return f"{eid} failed, attempt {n}"
            if paused:
                attempt.pop("ended")
                _pause_run(rec, paused)
                ledger.save_record(rec)
                return f"{eid} paused"
            rec["status"] = "evaluated"
            rec["kept"] = runner.keep_small(out, exp / "kept", cfg.keep, cfg.keep_max_kb)
            ledger.save_record(rec)  # the result is safe before anything else is written about it
            kind = "scored" if scored else "diagnose"
            try:
                facts = render.analysis_facts(ledger, rec)
            except (ArlError, KeyError) as e:
                facts = f"Written by arl. The table of results could not be written ({e}). The numbers are in metrics.json and record.json."
            write_text(exp / "analysis.md", render.fill(render.template(f"analysis_{kind}.md"), id=eid, facts=facts))
            return f"{eid} result" + (f", {cfg.primary.name} {fmt(rec['metrics'][cfg.primary.name])}" if scored else "")

        try:
            with ledger.locked(wait=900):
                finish(ledger, write_outcome())
        except ArlError as e:
            # The run is over and its outcome must not be lost because git or the home
            # checkout is in the way: save the record, leave the commit for later.
            if rec["status"] == "running":
                write_outcome()
            print(f"{eid}: the outcome is saved but could not be committed:\n  {e}\nFix that; the next arl command that writes the ledger commits it.", file=sys.stderr)

    if failed:
        print(f"{eid} FAILED (attempt {n} of {cfg.max_attempts}): {failed}")
        print(f"Log: {ARL} exp log {eid}")
        return EXIT_CRASH
    if paused:
        print(f"{eid} PAUSED after {attempt['run_seconds']:.0f} s of running: {paused}")
        print(f"It continues from the state it last saved:  {ARL} exp run {eid}")
        return EXIT_PAUSED
    print(_summary(ledger, rec))
    if attempt.get("warning"):
        print(f"  note: {attempt['warning']}")
    print()
    print(render.next_text(ledger, {"stage": "judge", "target": eid, "why": f"{eid} has a result and no verdict"}), end="")
    return 0


def cmd_exp_pause(args: argparse.Namespace) -> int:
    """Ask a run that is going to save its state and stop. It continues later with 'exp run'."""
    ledger = Ledger.locate()
    asked = _ask_runs_to_pause(ledger, check_id("E", args.id) if args.id else None)
    if not asked:
        raise ArlError("nothing is running" + (f" under {args.id}" if args.id else ""))
    _say_asked(ledger, asked)
    return 0


def cmd_exp_fail(args: argparse.Namespace) -> int:
    """Give up the saved state of a paused run. The attempt counts as failed."""
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with writing(ledger):
        _gate_paused(ledger)
        rec = ledger.record(eid)
        _settle(ledger, rec)
        if rec["status"] != "paused":
            raise ArlError(f"{eid} is {rec['status']}. Only a paused run is given up this way.")
        n = rec["attempts"][-1]["n"]
        _fail(rec, f"not continued: {args.reason}")
        ledger.save_record(rec)
        finish(ledger, f"{eid} attempt {n} given up")
    left = ledger.cfg.max_attempts - len(rec["attempts"])
    print(f"{eid}: attempt {n} counts as failed. Its saved state stays in artifacts/run-{n} and is not used again.")
    if left > 0:
        print(f"The next run starts from nothing ({left} attempt(s) left):  {ARL} exp run {eid}")
    else:
        print(f"That was the last attempt. Judge it:  {ARL} exp judge {eid} --verdict crash --reason \"...\"")
    return 0


def _summary(ledger: Ledger, rec: dict[str, Any]) -> str:
    cfg = ledger.cfg
    lines = [f"{rec['id']} {rec['title']} [{rec['type']} on {rec['track']}, {rec['status']}]"]
    if "metrics" in rec:
        comp = rec["comparison"]
        for m in cfg.metrics:
            if m.name in rec["metrics"]:  # a metric added to the contract later is not in older records
                lines.append(f"  {m.name:28s} {fmt(rec['metrics'][m.name])}" + ("  (primary)" if m.role == "primary" else ""))
        if comp.get("reference"):
            lines.append(
                f"  against {comp['reference']}: {comp['metric']} {fmt(comp['reference_value'])} -> {fmt(comp['value'])}, "
                f"gain {comp['gain']:+.6g} (at least {fmt(comp['min_delta'])} counts)"
            )
        for g in comp["gate_failures"]:
            lines.append(f"  constraint not met: {g}")
        lines.append(f"  outcome: {comp['outcome']}")
    if rec["status"] in ("judged", "judging", "abandoned"):
        v = rec["verdict"]
        lines.append(f"  verdict: {v['decision']}. {v['reason']}")
    return "\n".join(lines)


def cmd_exp_judge(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid, verdict = check_id("E", args.id), args.verdict
    cut_off = ledger.record(eid)["status"] == "judging"
    with writing(ledger):  # finishes a verdict that was cut off, this one's included
        contract.require(ledger)
        _gate_paused(ledger)
        rec = ledger.record(eid)
        if cut_off:
            given = rec["verdict"]["decision"]
            print(f"The verdict of {eid} ({given}) was recorded before and cut off. It is complete now" + (f"; '{verdict}' was not applied." if verdict and verdict != given else "."))
            print(_summary(ledger, rec))
            return 0
        if verdict is None or args.reason is None:
            raise ArlError("say what the verdict is and why: --verdict <keep|discard|finding|crash> --reason \"...\"")
        _settle(ledger, rec)
        if rec["status"] not in ("evaluated", "crashed", "paused"):
            raise ArlError(f"{eid} is {rec['status']}: there is nothing to judge")
        allowed = rules.allowed_verdicts(rec)
        outcome = rec.get("comparison", {}).get("outcome")
        if verdict not in allowed:
            what = outcome or ("not scored" if rec["status"] == "evaluated" else rec["status"])
            raise ArlError(f"{eid} is '{what}': the verdict can be {' or '.join(allowed)}, not {verdict}")
        if args.simpler and not (verdict == "keep" and outcome == "neutral"):
            raise ArlError("--simpler is only for keeping a neutral result")
        if verdict == "keep" and outcome == "neutral" and not args.simpler:
            raise ArlError("a neutral result is kept only when it makes things simpler: pass --simpler and say how in --reason")
        exp = ledger.exp_dir(eid)
        if rec["status"] == "evaluated":
            require_filled(exp / "analysis.md", f"the analysis of {eid}")
            _require_design_unchanged(ledger, rec)
        updates: dict[str, str] = {}
        for item in args.hyp or []:
            hid, _, state = item.partition("=")
            if hid not in rec["hypotheses"] or state not in HYP_VERDICTS:
                raise ArlError(f"--hyp takes <id>=<{'|'.join(HYP_VERDICTS)}> for a hypothesis of {eid}, got '{item}'")
            meta, _ = ledger.hypothesis(hid)  # it must still be there, before anything is changed
            if meta["status"] == "dropped" and state != "open":
                raise ArlError(f"{hid} was dropped. Pass {hid}=open, or reopen it first with 'arl hyp set'.")
            updates[hid] = state
        if verdict == "crash" and any(state != "open" for state in updates.values()):
            raise ArlError("a crash says nothing about a hypothesis: leave --hyp out, or pass <id>=open")
        if rec["status"] == "evaluated" and rec["type"] in ("tune", "feature"):
            missing = [h for h in rec["hypotheses"] if h not in updates]
            if missing:
                raise ArlError("say what this run did to each hypothesis: " + " ".join(f"--hyp {h}=<{'|'.join(HYP_VERDICTS)}>" for h in missing))

        track = ledger.track(rec["track"])
        path, wgit = _worktree(ledger, track)
        changed = wgit.tracked_changes()
        if changed:
            raise ArlError(
                f"tracked files in {path} differ from the commit that ran:\n  "
                + "\n  ".join(changed[:10])
                + "\nThese edits are not part of what was measured. Copy what you want to keep somewhere else,\n"
                + f"then put the files back: git -C {path} restore <file>"
            )
        if rec["branch"] != track["branch"] and wgit.branch() != rec["branch"]:
            raise ArlError(f"{path} is on '{wgit.branch()}', expected '{rec['branch']}'")
        if verdict == "keep":
            if wgit.head() != rec["code_commit"]:
                raise ArlError(f"{rec['branch']} moved after the run ({rec['code_commit'][:10]} was measured). Keep needs the measured commit.")
            _load_params(exp / "params.json")

        # Everything is checked. Write the verdict first: from here on it is decided, and
        # the branch, the track and the hypotheses follow, now or when the next command runs.
        rec["verdict"] = {
            "decision": verdict,
            "reason": args.reason,
            "at": now(),
            "hypotheses": updates,
            "track_head": {"before": track["head_commit"]},
        }
        if args.simpler:
            rec["verdict"]["simpler"] = True
        for key in ("pid", "pid_start", "child"):
            rec.pop(key, None)
        rec["status"] = "judging"
        ledger.save_record(rec)
        _finish_judge(ledger, rec)
    print(_summary(ledger, rec))
    print()
    print(render.next_text(ledger, _next(ledger)), end="")
    return 0


def cmd_exp_abandon(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with writing(ledger):
        _gate_paused(ledger)
        rec = ledger.record(eid)
        _settle(ledger, rec)
        if rec["status"] != "draft" or rec["attempts"]:
            how = f" It ran: judge it ('{ARL} exp judge {eid} --verdict crash --reason \"...\"')." if rec["status"] in ("crashed", "paused") else ""
            raise ArlError(f"only a draft that never ran can be abandoned. {eid} is {rec['status']}.{how}")
        track = ledger.track(rec["track"])
        path, wgit = _worktree(ledger, track)
        if rec["branch"] != track["branch"] and wgit.branch() == rec["branch"]:
            if wgit.dirty():
                raise ArlError(f"{path} has uncommitted changes. Commit them to {rec['branch']} ('{ARL} exp commit {eid} -m \"...\"') or undo them first.")
            wgit.switch(track["branch"])
        rec["status"] = "abandoned"
        rec["verdict"] = {"decision": "abandoned", "reason": args.reason, "at": now()}
        ledger.save_record(rec)
        finish(ledger, f"{eid} abandoned")
    print(f"{eid} abandoned. Its branch, if any, stays in git.")
    return 0


def cmd_exp_show(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rec = ledger.record(args.id)
    if args.json:
        print(json.dumps(rec, indent=2))
        return 0
    exp = ledger.exp_dir(args.id)
    print(_summary(ledger, rec))
    print(f"  tests: {' '.join(rec['hypotheses']) or 'no hypothesis'}")
    print(f"  code: {rec.get('code_commit', rec['base_commit'])[:10]} on {rec['branch']} (track head was {rec['base_commit'][:10]})")
    try:
        print(f"  params: {json.dumps(_load_params(exp / 'params.json'))}")
    except ArlError as e:
        print(f"  params: {e}")
    for a in rec["attempts"]:
        took = f"run {a.get('run_seconds', 0):.0f} s" + (f", evaluation {a['eval_seconds']:.0f} s" if "eval_seconds" in a else "")
        parts = len(a.get("segments", []))
        took += f", in {parts} parts" if parts > 1 else ""
        state = f", FAILED: {a['failed']}" if a.get("failed") else (f", PAUSED: {a['paused']}" if a.get("paused") else "")
        saved = f", state last saved {a['last_checkpoint'][:16]}" if a.get("last_checkpoint") else ""
        print(f"  attempt {a['n']}: {took}{state}{saved}")
        if a.get("warning"):
            print(f"    note: {a['warning']}")
    name = ledger.cfg.primary.name
    for r in rec.get("reruns", []):
        if r.get("failed"):
            print(f"  rerun {r['n']} on {r['split']}: FAILED: {r['failed']}")
        else:
            value = fmt(r["metrics"][name]) if name in r.get("metrics", {}) else "not reported"
            other = f", other packages ({'; '.join(r['packages_changed'])})" if r.get("packages_changed") else ""
            print(f"  rerun {r['n']} on {r['split']}: {name} {value}" + (f", reproduced: {r['reproduced']}" if "reproduced" in r else "") + other)
    env = rec.get("conditions", {}).get("env")
    if env:
        named = [f"{k} {v}" for k, v in (env.get("versions") or {}).items()]
        what = [env.get("prefix") or "the python of the shell that ran it"] + ([f"python {env['python']}"] if env.get("python") else []) + named
        listing = f"; every package: {ledger.name}/envs/{env['packages']}.txt" if env.get("packages") else ""
        print(f"  environment: {', '.join(what)}{listing}")
        print(f"  ran under: contract v{rec['contract']['version']}, arl {env.get('arl', '(version not recorded)')}, on {env.get('host', 'an unnamed machine')}")
    print(f"  files: {exp}")
    code, base = rec.get("code_commit"), rec["base_commit"]
    if code and code != base:
        print(f"  its code change:  git -C {ledger.home} diff {base[:10]} {code[:10]} -- . ':(exclude){ledger.name}'")
    if rec["type"] in rules.SCORED and "metrics" in rec:
        print(f"  run it again:     {ARL} exp reproduce {rec['id']}")
        print(f"  start from it:    {ARL} track new <name> --from {rec['id']}")
    return 0


def cmd_exp_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    exps = [
        e
        for e in ledger.experiments()
        if (not args.track or e["track"] == args.track) and (not args.status or e["status"] == args.status)
    ]
    print(render.exp_table(ledger, exps), end="")
    return 0


def cmd_exp_log(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    logs = ledger.exp_dir(args.id) / "logs"
    kind = "eval" if args.eval else "run"
    files = sorted(logs.glob(f"{kind}-*.log"), key=lambda p: p.stat().st_mtime) if logs.exists() else []
    if not files:
        raise ArlError(f"no {kind} log for {args.id} yet")
    print(f"# {files[-1]}")
    print(runner.tail(files[-1], lines=args.lines), end="")
    return 0


def cmd_exp_reproduce(args: argparse.Namespace) -> int:
    """Run a recorded experiment again from its commit and params, on any split."""
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with runner.run_lock(ledger):
        with writing(ledger):
            lock = contract.require(ledger)
            _settle_all(ledger)
            rec = ledger.record(eid)
            cfg = ledger.cfg
            if rec["type"] not in rules.SCORED or "metrics" not in rec:
                raise ArlError("only a scored experiment with a result can be run again")
            _gate_running(ledger, first_attempt=False)
            _require_design_unchanged(ledger, rec)
            probed = _env(ledger)
            split = args.split or cfg.splits[0]
            if split not in cfg.splits:
                raise ArlError(f"arl.toml knows the splits {cfg.splits}, not '{split}'")
            reasons = runner.busy(cfg)
            if reasons:
                raise ArlError("The machine is busy, not starting:\n  " + "\n  ".join(reasons), EXIT_BUSY)
            exp = ledger.exp_dir(eid)
            n = max([r["n"] for r in rec.get("reruns", [])], default=0) + 1  # failed reruns keep their number
            tree = ledger.worktree_root() / f"{cfg.name}-rerun-{eid}-{n}"
            out, logs = exp / "artifacts" / f"rerun-{n}-{split}", exp / "logs"
            if out.exists():
                shutil.rmtree(out)  # nothing recorded owns it: a rerun starts in an empty folder
            if tree.exists():
                _drop_tree(ledger, tree)
            (logs / "pause.request").unlink(missing_ok=True)  # an old request is not meant for this rerun
            env_now = runner.environment(ledger, probed)
            rec["rerun"] = {"n": n, "split": split, "started": now(), "pid": os.getpid(), "pid_start": runner.proc_start(os.getpid()), "tree": str(tree)}
            ledger.save_record(rec)

        out.mkdir(parents=True)
        metrics_path = out / "metrics.json"
        values = {"params": exp / "params.json", "out": out, "split": split, "home": ledger.home, "research": ledger.dir, "exp": exp, "worktree": tree, "metrics": metrics_path}
        env = _run_env(ledger, rec, values, resume=False)
        limits = {"max_rss_gb": cfg.max_run_rss_gb, "min_free_ram_gb": cfg.stop_below_free_ram_gb, "pause_file": logs / "pause.request", "pause_grace": cfg.pause_grace}
        entry: dict[str, Any] = {"n": n, "at": now(), "split": split, "eval_digest": lock["eval_digest"], "env": env_now}
        then = rec["conditions"]
        if probed and probed.get("digest") and then.get("env_digest") not in (None, probed["digest"]):
            entry["packages_changed"] = runner.env_changes(then.get("env"), probed)

        def why(res: dict[str, Any]) -> str:
            if res["paused"]:
                return f"stopped: {res['paused']}. A rerun is not continued, run it again"
            return res["stopped"] or f"exit {res['exit_code']}"
        clock, failed, metrics = time.monotonic(), "", {}

        def started(pid: int) -> None:
            rec["rerun"]["child"] = runner.child_ref(pid)
            ledger.save_record(rec)

        try:
            tree.parent.mkdir(parents=True, exist_ok=True)
            ledger.git.prune_worktrees()
            ledger.git.add_detached(tree, rec["code_commit"])
            command = rec.get("command") or cfg.run_command
            res = runner.execute(runner.substitute(command, values), tree, env, cfg.run_timeout, logs / f"rerun-{n}.log", on_start=started, **limits)
            entry["run_seconds"] = res["seconds"]
            if res["stopped"] or res["paused"] or res["exit_code"] != 0:
                failed = f"the rerun failed ({why(res)})"
            elif not runner.manifest(out):
                failed = "the rerun wrote nothing into its output folder"
            if not failed:
                ev = runner.execute(runner.substitute(cfg.eval_command, values), ledger.home, env, cfg.eval_timeout, logs / f"rerun-eval-{n}.log", on_start=started, **limits)
                entry["eval_seconds"] = ev["seconds"]
                if ev["stopped"] or ev["paused"] or ev["exit_code"] != 0:
                    failed = f"the evaluator failed on the rerun ({why(ev)})"
                elif contract.require(ledger)["digest"] != lock["digest"]:
                    failed = "the contract was frozen anew during the rerun"
                else:
                    metrics = runner.read_metrics(metrics_path, cfg)
        except ArlError as e:
            failed = str(e).splitlines()[0]
        except BaseException:
            entry.setdefault("run_seconds", round(time.monotonic() - clock, 2))
            entry["failed"] = "interrupted"
            rec.setdefault("reruns", []).append(entry)
            rec.pop("rerun", None)
            ledger.save_record(rec)
            with contextlib.suppress(ArlError):
                _drop_tree(ledger, tree)
            raise
        finally:
            if tree.exists():
                with contextlib.suppress(ArlError):
                    _drop_tree(ledger, tree)
        entry.setdefault("run_seconds", round(time.monotonic() - clock, 2))
        primary = cfg.primary
        if failed:
            entry["failed"] = failed
        else:
            entry["metrics"] = metrics
            if split == cfg.splits[0] and rec["contract"]["eval_digest"] == lock["eval_digest"]:
                diff = abs(metrics[primary.name] - rec["metrics"][primary.name])
                entry["difference"] = diff
                entry["reproduced"] = bool(diff <= max(primary.min_delta, 1e-12))

        def store() -> None:
            now_rec = ledger.record(eid)
            now_rec.setdefault("reruns", []).append(entry)
            now_rec.pop("rerun", None)
            ledger.save_record(now_rec)

        try:
            with ledger.locked(wait=900):
                store()
                finish(ledger, f"{eid} rerun on {split}" + (" failed" if failed else ""))
        except ArlError as e:
            if "rerun" in ledger.record(eid):
                store()  # the result is kept even if it cannot be committed now
            print(f"{eid}: the rerun is saved but could not be committed:\n  {e}", file=sys.stderr)
    if failed:
        print(f"{eid} rerun on split '{split}' FAILED: {failed}. Log: {logs / f'rerun-{n}.log'}")
        return EXIT_CRASH
    print(f"{eid} rerun on split '{split}': {primary.name} = {fmt(metrics[primary.name])}")
    if entry.get("packages_changed"):
        print(f"  not with the packages of the recorded run: {'; '.join(entry['packages_changed'])}")
    if "reproduced" in entry:
        print(f"  recorded {fmt(rec['metrics'][primary.name])}, difference {entry['difference']:.3g}, reproduced: {entry['reproduced']}")
    else:
        print("  not compared with the recorded result (another split or another evaluator). Never used for a verdict.")
    return 0


# --- introspections ---------------------------------------------------------------


def cmd_introspect_due(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    state = rules.introspection_state(ledger)
    if args.json:
        print(json.dumps(state, indent=2))
    elif state["due"]:
        print("An introspection is due:\n  " + "\n  ".join(state["reasons"]))
    else:
        print(f"No introspection due: {state['judged_since']} of {ledger.cfg.intro_every} experiments judged since {state['last'] or 'the start'}.")
    return 0


def cmd_introspect_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with writing(ledger):
        contract.require(ledger)
        _gate_paused(ledger)
        state = rules.introspection_state(ledger)
        if state["open"] or state["waiting"]:
            raise ArlError(f"{state['open'] or state['waiting']} is not finished yet")
        pending = [e["id"] for e in ledger.experiments() if e["status"] in ("running", "evaluated", "judging")]
        if pending:
            raise ArlError(f"finish {', '.join(pending)} first: an introspection needs their verdicts")
        nid = ledger.next_id("N")
        trigger = one_line(args.trigger, "--trigger") if args.trigger else "; ".join(state["reasons"]) or "asked for"
        ledger.intro_root.mkdir(parents=True, exist_ok=True)
        events = ledger.activity()
        earlier = [n for n in ledger.introspections() if "activity_upto" in n]
        activity_from = earlier[-1]["activity_upto"] if earlier else 0
        meta = {
            "id": nid,
            "status": "open",
            "trigger": trigger,
            "opened": now(),
            "window": state["window"],
            "activity_upto": len(events),
            "knowledge_sha256": ledger.knowledge_hash(),
            "lessons_general": len(ledger.lessons("general")),
            "lessons_project": len(ledger.lessons("project")),
        }
        facts = render.introspection_facts(ledger, trigger, state, activity_from)
        write_front(ledger.intro_root / f"{nid}.md", meta, render.fill(render.template("introspection.md"), id=nid, facts=facts))
        write_text(ledger.status_path, render.status(ledger))
    print(f"{nid} opened: {ledger.intro_root / f'{nid}.md'}")
    print("The facts are filled in. Write its parts with fresh eyes, below the +++ block:")
    print("  part 1, the path; part 2, lessons about the way of working ('arl lesson add');")
    print(f"  part 3, what is now known about the problem (edit {ledger.knowledge_path}).")
    print(f"Then:  {ARL} introspect close {nid} --decision <{'|'.join(DECISIONS)}>")
    print(f"'{ARL} next' prints the full steps.")
    return 0


def cmd_introspect_close(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with writing(ledger):
        contract.require(ledger)  # who may decide what is in arl.toml: it has to be the frozen one
        _gate_paused(ledger)
        meta, body = ledger.introspection(args.id)
        if meta["status"] != "open":
            raise ArlError(f"{args.id} is {meta['status']}")
        require_filled(ledger.intro_path(args.id), f"introspection {args.id}")
        added = len(ledger.lessons("general")) - meta.get("lessons_general", 0) + len(ledger.lessons("project")) - meta.get("lessons_project", 0)
        if added <= 0 and not args.no_lessons:
            raise ArlError(
                f"Part 2 of {args.id} is not done: no lesson was recorded since it was opened.\n"
                f"Record what deserves a standing instruction ('{ARL} lesson add --scope general|project ...'),\n"
                "or pass --no-lessons \"why\" if this window taught nothing about the way of working."
            )
        same_knowledge = ledger.knowledge_hash() == meta.get("knowledge_sha256")
        if same_knowledge and not args.knowledge_unchanged:
            raise ArlError(
                f"Part 3 of {args.id} is not done: {ledger.knowledge_path} is as it was when the introspection was opened.\n"
                "Bring it up to date with what this window showed about the problem (revise and merge, do not append),\n"
                "or pass --knowledge-unchanged \"why\" if the window showed nothing new."
            )
        reopening = rules.introspection_state(ledger)["last_decision"] == "stop" and args.decision != "stop"
        alone = args.decision in ledger.cfg.agent_may and not reopening
        meta.update(status="closed" if alone else "waiting", decision=args.decision, note=args.note or "", lessons_added=max(added, 0), knowledge_changed=not same_knowledge)
        if args.no_lessons:
            meta["no_lessons"] = args.no_lessons
        if args.knowledge_unchanged:
            meta["knowledge_unchanged"] = args.knowledge_unchanged
        if alone:
            meta.update(closed=now(), decided_by="agent")
        else:
            meta["proposed"] = now()
        write_front(ledger.intro_path(args.id), meta, body)
        finish(ledger, f"{args.id} introspection, {args.decision}" + ("" if alone else " proposed"))
    if alone:
        print(f"{args.id} closed: {args.decision}.")
    elif reopening:
        print(f"{args.id} proposes '{args.decision}' after a stop. Reopening the program is a person's decision.")
    else:
        print(f"{args.id} proposes '{args.decision}', which the agent may not decide alone.")
    print()
    print(render.next_text(ledger, _next(ledger)), end="")
    return 0


def cmd_introspect_resolve(args: argparse.Namespace) -> int:
    if not interactive():
        raise ArlError("a waiting introspection is resolved by a person, in a terminal")
    ledger = Ledger.locate()
    with writing(ledger):
        contract.require(ledger)
        meta, body = ledger.introspection(args.id)
        if meta["status"] != "waiting":
            raise ArlError(f"{args.id} is {meta['status']}, not waiting")
        meta.update(status="closed", proposed_decision=meta["decision"], decision=args.decision, closed=now(), decided_by="person", note=args.note)
        write_front(ledger.intro_path(args.id), meta, body.rstrip("\n") + f"\n\n# Resolved\n\n{now()[:10]}: {args.decision}. {args.note}\n")
        finish(ledger, f"{args.id} resolved, {args.decision}")
    print(f"{args.id} closed: {args.decision}.")
    return 0


# --- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arl", description="Local harness for automated research.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(parent: Any, name: str, func: Any, help: str) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help, description=help)
        sp.set_defaults(func=func)
        return sp

    add(sub, "init", cmd_init, "Create research/ with templates in this project.")
    sp = add(sub, "freeze", cmd_freeze, "Hash the contract into frozen.lock. After the first experiment only a person can.")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--yes", action="store_true", help="skip the question (still needs a terminal)")
    sp = add(sub, "status", cmd_status, "Where things stand.")
    sp.add_argument("--json", action="store_true")
    sp = add(sub, "next", cmd_next, "The one thing to do now, with instructions.")
    sp.add_argument("--json", action="store_true")
    add(sub, "verify", cmd_verify, "Check that ledger, contract and git agree.")
    add(sub, "reload", cmd_reload, "Print the knowledge and the lessons as they are now. Read them.")
    sp = add(sub, "pause", cmd_pause, "Stop the loop where it is: a run saves its state and stops, nothing new starts.")
    sp.add_argument("--reason", help="why it is paused")
    sp.add_argument("--note", help="where you were and what you were about to do, for whoever continues. Can be added after the pause")
    add(sub, "resume", cmd_resume, "Continue a paused loop, when the person says so.")
    sp = add(sub, "note", cmd_note, "Write down now what the next introspection should take up.")
    sp.add_argument("--about", required=True, choices=["work", "problem"], help="work: a slip or waste in the way of working. problem: something seen about the problem")
    sp.add_argument("text", nargs="+")

    track = sub.add_parser("track", help="Code tracks: one branch and worktree each.").add_subparsers(dest="action", required=True)
    sp = add(track, "new", cmd_track_new, "Open a track.")
    sp.add_argument("name")
    sp.add_argument("--from", dest="from_ref", help="commit, branch or experiment (E0007: its code and its params) to start from. Default: the home branch")
    sp.add_argument("--introspection", help="the latest closed introspection, which decided to open it")
    sp.add_argument("--params", help="JSON file with the track's starting params")
    sp = add(track, "set", cmd_track_set, "Park, close or reactivate a track.")
    sp.add_argument("name")
    sp.add_argument("--status", required=True, choices=["active", "parked", "closed"])
    sp.add_argument("--note", required=True)
    sp.add_argument("--introspection", help="the latest closed introspection, which decided it")
    sp = add(track, "sync", cmd_track_sync, "Record that a track branch was changed by hand.")
    sp.add_argument("name")
    sp.add_argument("--note", required=True)
    sp = add(track, "worktree", cmd_track_worktree, "Put a track's worktree back when its folder is gone, and its branch after a clone.")
    sp.add_argument("name")
    add(track, "list", cmd_track_list, "List tracks.")

    hyp = sub.add_parser("hyp", help="Hypotheses: claims, separate from the experiments that test them.").add_subparsers(dest="action", required=True)
    sp = add(hyp, "new", cmd_hyp_new, "Start a hypothesis file.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--from", dest="source", help="where it comes from (an experiment, an idea, an introspection)")
    sp = add(hyp, "set", cmd_hyp_set, "Change a hypothesis's status.")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=HYP_STATES)
    sp.add_argument("--note", required=True)
    sp = add(hyp, "list", cmd_hyp_list, "List hypotheses.")
    sp.add_argument("--status", choices=HYP_STATES)

    idea = sub.add_parser("idea", help="IDEAS.md: what could change the path, read at every introspection.").add_subparsers(dest="action", required=True)
    sp = add(idea, "add", cmd_idea_add, "Write an idea down.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", required=True, help="what it is, why it could matter, the cheapest test")
    sp.add_argument("--from", dest="source", help="where it came up")
    sp.add_argument("--kind", default="approach", help="approach, feature, diagnostic, evaluator, external ...")
    sp = add(idea, "set", cmd_idea_set, "Change an idea's status.")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=IDEA_STATES)
    sp.add_argument("--note", required=True)
    sp = add(idea, "list", cmd_idea_list, "List ideas.")
    sp.add_argument("--status", choices=IDEA_STATES)

    lesson = sub.add_parser("lesson", help="Lessons about the way of working, kept in the instructions agents load.").add_subparsers(dest="action", required=True)
    sp = add(lesson, "add", cmd_lesson_add, "Record a lesson.")
    sp.add_argument("--scope", required=True, choices=SCOPES, help="general: any project that uses arl. project: only this one")
    sp.add_argument("--title", required=True, help="the instruction, in a few words")
    sp.add_argument("--body", required=True, help="what happened, and what to do instead")
    sp.add_argument("--from", dest="source", help="the introspection or experiment it comes from")
    sp = add(lesson, "list", cmd_lesson_list, "List lessons.")
    sp.add_argument("--scope", choices=SCOPES)

    exp = sub.add_parser("exp", help="Experiments: design, run, judge.").add_subparsers(dest="action", required=True)
    sp = add(exp, "new", cmd_exp_new, "Draft an experiment.")
    sp.add_argument("--type", required=True, choices=rules.TYPES)
    sp.add_argument("--track", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--hyp", action="append", help="hypothesis it tests (repeatable)")
    sp.add_argument("--command", help="diagnose only: the command to run instead of the one in arl.toml")
    sp = add(exp, "commit", cmd_exp_commit, "Commit the code of a feature or diagnose experiment.")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
    sp = add(exp, "run", cmd_exp_run, "Lock the design, run, evaluate. Continues a run that was paused.")
    sp.add_argument("id")
    sp.add_argument("--allow-duplicate", metavar="WHY", help="repeat a known result on purpose")
    sp = add(exp, "pause", cmd_exp_pause, "Ask the run that is going to save its state and stop.")
    sp.add_argument("id", nargs="?")
    sp = add(exp, "fail", cmd_exp_fail, "Give up a paused run: the attempt counts as failed, the next run starts from nothing.")
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    sp = add(exp, "judge", cmd_exp_judge, "Record the verdict.")
    sp.add_argument("id")
    sp.add_argument("--verdict", choices=["keep", "discard", "finding", "crash"])
    sp.add_argument("--reason")
    sp.add_argument("--hyp", action="append", metavar="ID=STATE", help="what the run did to a hypothesis (repeatable)")
    sp.add_argument("--simpler", action="store_true", help="keep a neutral result because it simplifies")
    sp = add(exp, "abandon", cmd_exp_abandon, "Drop a draft that never ran.")
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    sp = add(exp, "show", cmd_exp_show, "One experiment: conditions, result, verdict.")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp = add(exp, "list", cmd_exp_list, "List experiments.")
    sp.add_argument("--track")
    sp.add_argument("--status")
    sp = add(exp, "log", cmd_exp_log, "The end of the latest log.")
    sp.add_argument("id")
    sp.add_argument("--eval", action="store_true", help="the evaluator's log")
    sp.add_argument("--lines", type=int, default=60)
    sp = add(exp, "reproduce", cmd_exp_reproduce, "Run a recorded experiment again from its commit and params.")
    sp.add_argument("id")
    sp.add_argument("--split", help="score on another split named in arl.toml")

    intro = sub.add_parser("introspect", help="The periodic introspection: the path, the way of working, what is known.").add_subparsers(dest="action", required=True)
    sp = add(intro, "due", cmd_introspect_due, "Is an introspection due, and why.")
    sp.add_argument("--json", action="store_true")
    sp = add(intro, "new", cmd_introspect_new, "Open an introspection with the facts filled in.")
    sp.add_argument("--trigger", help="why now, when it is not one of the counters")
    sp = add(intro, "close", cmd_introspect_close, "Record the decision, once all parts are done.")
    sp.add_argument("id")
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--note")
    sp.add_argument("--no-lessons", metavar="WHY", help="part 2 found nothing worth a standing instruction")
    sp.add_argument("--knowledge-unchanged", metavar="WHY", help="part 3 found nothing new about the problem")
    sp = add(intro, "resolve", cmd_introspect_resolve, "A person decides a waiting introspection (terminal only).")
    sp.add_argument("id")
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--note", required=True)
    return p


_ONE_LINE = ("title", "reason", "note", "trigger", "no_lessons", "knowledge_unchanged", "allow_duplicate", "message", "source")


def _clean(args: argparse.Namespace) -> None:
    """Everything typed has to be text that can be stored. What is one line is made one line,
    and what was asked for cannot be blank."""
    for name, value in vars(args).items():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                check_text(item, f"--{name.replace('_', '-')}")
        if name in _ONE_LINE and isinstance(value, str):
            setattr(args, name, one_line(value, f"--{name.replace('_', '-')}"))


def _on_signal(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        # Asked to stop in any of these ways, a run saves its state and takes its job down with it.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _on_signal)
    except ValueError:
        pass
    try:
        _clean(args)
        return args.func(args) or 0
    except ArlError as e:
        print(f"arl: {e}", file=sys.stderr)
        if e.code != EXIT_BUSY:
            with contextlib.suppress(Exception):  # what was refused is material for the next introspection
                ledger = Ledger.locate()
                if ledger.exists():
                    ledger.log_activity("refused", str(e), command=" ".join(argv))
        return e.code
    except KeyboardInterrupt:
        print("arl: interrupted", file=sys.stderr)
        return 130
