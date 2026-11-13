"""Sequences that used to break a guarantee, lose work, dead-end or end in a traceback.

Most were found by running the CLI against scratch projects the way a careless
agent would. Each test pins the behaviour that replaced the defect.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from arl import cli, contract, runner
from conftest import Project, git

DEAD_PID = 2**22 - 7


def new_exp(p: Project, typ: str, h: str | None = None, code: int = 0, track: str = "main-line") -> str:
    args = ["exp", "new", "--type", typ, "--track", track, "--title", "next one"]
    return p.arl(*args, *(["--hyp", h] if h else []), code=code)


def after_the_run(monkeypatch, action):
    """Do something at the moment the run has ended and the evaluator has not started."""
    real, calls = runner.execute, []

    def execute(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            action()
        return result

    monkeypatch.setattr(runner, "execute", execute)


# --- dead ends ----------------------------------------------------------------------


def test_run_killed_on_its_last_attempt_can_still_be_judged(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    p.arl("exp", "run", eid, code=2)
    rec = p.record(eid)  # the session dies during the second, last attempt
    rec.update(status="running", pid=DEAD_PID, pid_start=1)
    rec["attempts"].append({"n": 2, "started": "2026-01-01T00:00:00+00:00"})
    p.ledger.save_record(rec)
    assert "NEXT: interrupted E0002" in p.arl("next")
    assert "the limit" in p.arl("exp", "run", eid, code=1)
    rec = p.record(eid)
    assert rec["status"] == "crashed" and rec["attempts"][1]["failed"].startswith("interrupted")
    assert "NEXT: crashed E0002" in p.arl("next") and "judge it as a crash" in p.arl("next")
    p.judge(eid, "crash")
    assert "NEXT: hypothesize" in p.arl("next")


def test_stale_run_is_settled_by_whichever_command_comes_next(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    rec = p.record(eid)
    rec.update(status="running", pid=DEAD_PID, pid_start=1, attempts=[{"n": 1, "started": "2026-01-01T00:00:00+00:00"}])
    p.ledger.save_record(rec)
    p.edit_code("main-line", 0.5)  # the fix the agent wants to commit after the run died
    assert "committed" in p.arl("exp", "commit", eid, "-m", "fix after the interruption")
    assert p.record(eid)["status"] == "crashed" and "OFFSET = 0.5" in (wt / "run.py").read_text()
    p.arl("exp", "run", eid)

    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    stuck = p.draft("tune", hyps=(h,), params={"x": 0.2})
    rec = p.record(stuck)
    rec.update(status="running", pid=1, pid_start=1, attempts=[{"n": 1, "started": "2026-01-01T00:00:00+00:00"}])
    p.ledger.save_record(rec)
    # pid 1 is alive but it is not the arl that started the run: a reused pid must not block for ever
    assert "NEXT: interrupted" in p.arl("next")
    p.judge(stuck, "crash")


def test_what_a_dead_run_left_behind_is_stopped(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        rec = p.record(eid)
        rec.update(status="running", pid=DEAD_PID, pid_start=1, child=orphan.pid, attempts=[{"n": 1, "started": "2026-01-01T00:00:00+00:00"}])
        p.ledger.save_record(rec)
        p.arl("exp", "run", eid)
        assert orphan.wait(timeout=20) is not None  # no second copy of the job beside the new run
        assert "left behind were stopped" in p.record(eid)["attempts"][0]["failed"]
    finally:
        orphan.kill()


def test_hangup_takes_the_job_down(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "sleep": 60})
    launcher = Path(cli.__file__).resolve().parents[2] / "bin" / "arl"
    proc = subprocess.Popen([sys.executable, str(launcher), "exp", "run", eid], cwd=p.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while time.time() < deadline and not p.record(eid).get("child"):
        time.sleep(0.1)
    child = p.record(eid)["child"]
    assert runner.session_pids(child)
    os.kill(proc.pid, signal.SIGHUP)
    proc.communicate(timeout=40)
    assert proc.returncode == 130 and not runner.session_pids(child)
    rec = p.record(eid)
    assert rec["status"] == "crashed" and rec["attempts"][0]["failed"] == "interrupted"


def test_refrozen_contract_with_work_pending(project: Project, monkeypatch):
    p = project
    h = p.started()
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# stricter\n")
    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("freeze", "--reason", "stricter", "--yes")
    monkeypatch.setattr(cli, "interactive", lambda: False)
    out = p.arl("next")
    assert "NEXT: outdated E0002" in out and "exp abandon E0002" in out
    assert "Abandon it" in p.arl("exp", "run", draft, code=1)
    p.arl("exp", "abandon", draft, "--reason", "its reference no longer counts")
    assert "NEXT: baseline main-line" in p.arl("next")


def test_reproduce_after_a_killed_rerun(project: Project):
    p = project
    h = p.started()
    eid = p.tune(0.5, "keep", h)
    left = p.base / "wt" / "toy-rerun-E0002-1"
    git(p.root, "worktree", "add", "--detach", str(left), p.record(eid)["code_commit"])
    assert "reproduced: True" in p.arl("exp", "reproduce", eid)
    assert not left.exists()


def test_worktree_folder_removed_by_hand(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    shutil.rmtree(wt)
    assert "track worktree main-line" in new_exp(p, "tune", h, code=1)
    assert "is back, on track/main-line" in p.arl("track", "worktree", "main-line")
    new_exp(p, "tune", h)


# --- records that describe what ran ---------------------------------------------------


def test_baseline_changes_nothing(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h)
    eid = p.draft("baseline", params={"x": 0.0})  # would lower the reference, then "improve" on it
    assert "differs from the track's params" in p.arl("exp", "run", eid, code=1)
    p.arl("exp", "abandon", eid, "--reason", "a baseline changes nothing")
    assert "tests no hypothesis" in p.arl("exp", "new", "--type", "baseline", "--track", "main-line", "--title", "b", "--hyp", h, code=1)


def test_track_params_change_only_through_a_kept_experiment(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h)
    params = p.research / "tracks" / "main-line.params.json"
    params.write_text(json.dumps({"x": 0.69}))
    assert "changed by hand" in new_exp(p, "tune", h, code=1)
    assert "changed by hand" in p.arl("verify", code=1)
    params.write_text(json.dumps({"x": 0.5}))
    new_exp(p, "tune", h)


def test_reproduce_uses_the_recorded_design(project: Project):
    p = project
    h = p.started()
    eid = p.tune(0.5, "keep", h)
    params = p.ledger.exp_dir(eid) / "params.json"
    params.write_text(json.dumps({"x": 0.7}))
    assert "The design is locked" in p.arl("exp", "reproduce", eid, code=1)
    assert "reruns" not in p.record(eid)


def test_contract_frozen_anew_during_a_run(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})

    def refreeze():
        evaluator = p.research / "eval" / "evaluate.py"
        evaluator.write_text(evaluator.read_text().replace("0.7,", "0.5,"))
        contract.freeze(p.ledger, "kinder", interactive=True)

    after_the_run(monkeypatch, refreeze)
    assert "frozen anew while the run was going" in p.arl("exp", "run", eid, code=2)
    rec = p.record(eid)
    assert rec["status"] == "crashed" and "metrics" not in rec


def test_code_committed_during_a_run(project: Project, monkeypatch):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    refused = []

    def commit_more():
        p.edit_code("main-line", 0.1)
        refused.append(p.arl("exp", "commit", eid, "-m", "one more thing", code=1))
        git(wt, "commit", "-qam", "by hand, behind arl's back")

    after_the_run(monkeypatch, commit_more)
    assert "changed while the run was going" in p.arl("exp", "run", eid, code=2)
    assert "is running: its code is fixed" in refused[0]
    assert p.record(eid)["status"] == "crashed"


def test_tracked_file_with_a_space_in_its_name(project: Project):
    p = project
    (p.root / "data file.txt").write_text("one\n")
    git(p.root, "add", "-A")
    git(p.root, "commit", "-qm", "a file with a space")
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("exp", "run", eid)
    (wt / "data file.txt").write_text("two\n")
    out = p.judge(eid, "keep", "--hyp", f"{h}=supported", code=1)
    assert "data file.txt" in out and "restore" in out and "exp commit" not in out
    (wt / "data file.txt").write_text("one\n")
    p.judge(eid, "keep", "--hyp", f"{h}=supported")


def test_same_code_on_two_tracks(project: Project):
    p = project
    p.setup()
    p.track("alpha")
    start = p.ledger.track("alpha")["head_commit"]
    p.track("beta", "--from", start)
    p.baseline("alpha")
    p.baseline("beta")  # the same commit and params, but its own reference
    assert p.ledger.track("beta")["head_experiment"] == "E0002"


# --- gates ----------------------------------------------------------------------------


def test_parking_a_stalled_track_does_not_skip_the_review(project: Project, monkeypatch):
    p = project
    p.setup(every=20, stagnation=2)
    p.track("alpha")
    p.track("beta")
    p.baseline("alpha")
    p.baseline("beta")
    h = p.hyp()
    p.tune(-0.2, "discard", h, track="alpha")
    p.tune(-0.3, "discard", h, track="alpha")
    assert "A review comes first" in new_exp(p, "tune", h, code=1, track="beta")
    assert "comes out of a review" in p.arl("track", "set", "alpha", "--status", "parked", "--note", "stalled", code=1)
    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("track", "set", "alpha", "--status", "parked", "--note", "stalled")
    monkeypatch.setattr(cli, "interactive", lambda: False)
    assert "A review comes first" in new_exp(p, "tune", h, code=1, track="beta")

    p.arl("review", "new")
    p.fill(p.research / "reviews" / "R001.md")
    p.arl("review", "close", "R001", "--decision", "adjust")
    new_exp(p, "tune", h, track="beta")
    assert "is already parked" in p.arl("track", "set", "alpha", "--status", "parked", "--note", "x", "--review", "R001", code=1)
    p.arl("track", "set", "alpha", "--status", "active", "--note", "one more try", "--review", "R001")


def test_only_the_latest_review_opens_tracks(project: Project):
    p = project
    h = p.started()
    for rid, decision in (("R001", "branch"), ("R002", "continue")):
        p.arl("review", "new", "--trigger", "asked")
        p.fill(p.research / "reviews" / f"{rid}.md")
        p.arl("review", "close", rid, "--decision", decision)
    assert "not the latest closed review" in p.arl("track", "new", "late", "--review", "R001", code=1)
    assert "decided 'continue'" in p.arl("track", "new", "late", "--review", "R002", code=1)
    assert h


def test_who_may_decide_is_read_from_the_frozen_contract(project: Project):
    p = project
    p.started()
    p.arl("review", "new", "--trigger", "asked")
    p.fill(p.research / "reviews" / "R001.md")
    toml = p.research / "arl.toml"
    frozen = toml.read_text()
    toml.write_text(frozen.replace('"pivot"]', '"pivot", "stop"]'))
    assert "does not match the files on disk" in p.arl("review", "close", "R001", "--decision", "stop", code=1)
    toml.write_text(frozen)
    assert "may not decide alone" in p.arl("review", "close", "R001", "--decision", "stop")


def test_stop_budget_and_waiting_hold_for_existing_drafts(project: Project, monkeypatch):
    p = project
    h = p.started(max_experiments=2)
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("review", "new", "--trigger", "asked")
    p.fill(p.research / "reviews" / "R001.md")
    p.arl("review", "close", "R001", "--decision", "stop")
    assert "waits for a person" in p.arl("exp", "run", draft, code=1)
    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("review", "resolve", "R001", "--decision", "stop", "--note", "enough")
    assert "NEXT: stopped R001" in p.arl("next")
    assert "Nothing runs until a person reopens it" in p.arl("exp", "run", draft, code=1)
    assert "Nothing runs" in p.arl("exp", "reproduce", "E0001", code=1)
    p.arl("review", "new", "--trigger", "reopen")
    p.fill(p.research / "reviews" / "R002.md")
    p.arl("review", "close", "R002", "--decision", "continue")
    p.arl("review", "resolve", "R002", "--decision", "continue", "--note", "go on")
    p.arl("exp", "run", draft)  # the second experiment: the budget of two is now used
    p.judge(draft, "keep", "--hyp", f"{h}=supported")
    assert "NEXT: budget" in p.arl("next")


def test_budget_holds_for_a_draft_made_before_it_ran_out(project: Project):
    p = project
    p.setup(max_experiments=2)
    p.track("alpha")
    p.track("beta")
    first = p.draft("baseline", "alpha")
    second = p.draft("baseline", "beta")
    p.arl("exp", "run", first)
    p.judge(first, "keep")
    p.arl("exp", "run", second)
    p.judge(second, "keep")
    h = p.hyp()
    assert "budget is spent" in new_exp(p, "tune", h, code=1, track="alpha")
    assert "NEXT: budget" in p.arl("next")


def test_run_hours_bound_reruns_too(project: Project):
    p = project
    p.arl("init")
    p.contract()
    toml = p.research / "arl.toml"
    toml.write_text(toml.read_text().replace("max_experiments = 40", "max_experiments = 40\nmax_run_hours = 0.000001"))
    (p.research / "eval" / "evaluate.py").write_text(__import__("conftest").EVAL_PY)
    p.fill(p.research / "PROGRAM.md")
    p.arl("freeze", "--reason", "initial")
    p.track()
    p.baseline()
    assert "budget is spent" in p.arl("exp", "reproduce", "E0001", code=1)


def test_experiments_judged_around_a_review_are_counted(project: Project):
    p = project
    h = p.started(every=3)
    crashed = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    p.arl("exp", "run", crashed, code=2)
    p.arl("exp", "run", crashed, code=2)
    p.arl("review", "new", "--trigger", "asked")  # lists E0001 only: E0002 has no verdict yet
    p.fill(p.research / "reviews" / "R001.md")
    p.arl("review", "close", "R001", "--decision", "continue")
    p.judge(crashed, "crash")  # the same second as the review, as likely as not
    assert "1 of 3 experiments judged since R001" in p.arl("review", "due")
    p.arl("review", "new", "--trigger", "asked")
    assert "E0002" in (p.research / "reviews" / "R002.md").read_text()


def test_verdict_flags_must_fit(project: Project):
    p = project
    h = p.started()
    crashed = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    p.arl("exp", "run", crashed, code=2)
    p.arl("exp", "run", crashed, code=2)
    assert "a crash says nothing about a hypothesis" in p.judge(crashed, "crash", "--hyp", f"{h}=supported", code=1)
    assert "--simpler is only for keeping a neutral result" in p.judge(crashed, "crash", "--simpler", code=1)
    p.judge(crashed, "crash", "--hyp", f"{h}=open")
    assert p.ledger.hypothesis(h)[0]["status"] == "open"
    worse = p.draft("tune", hyps=(h,), params={"x": -0.5})
    p.arl("exp", "run", worse)
    assert "--simpler is only" in p.judge(worse, "discard", "--simpler", "--hyp", f"{h}=open", code=1)
    p.judge(worse, "discard", "--hyp", f"{h}=refuted")


# --- nothing half done, no tracebacks -------------------------------------------------


def test_refused_judge_changes_nothing(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    p.arl("exp", "run", eid)
    p.fill(p.ledger.exp_dir(eid) / "analysis.md")
    p.ledger.hyp_path(h).unlink()
    assert "no hypothesis H001" in p.arl("exp", "judge", eid, "--verdict", "keep", "--reason", "r", "--hyp", f"{h}=supported", code=1)
    assert p.record(eid)["status"] == "evaluated" and p.ledger.track("main-line")["head_experiment"] == "E0001"
    assert git(wt, "branch", "--show-current").startswith("exp/E0002")


def test_refused_draft_leaves_nothing_behind(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    params = p.research / "tracks" / "main-line.params.json"
    good = params.read_text()
    params.write_text("{,}")
    assert "is not valid JSON" in p.arl("exp", "new", "--type", "feature", "--track", "main-line", "--title", "new code", "--hyp", h, code=1)
    assert git(wt, "branch", "--show-current") == "track/main-line"
    assert not list((p.research / "experiments").glob("E0002*"))
    params.write_text(good)
    assert "NEXT: design E0002" in p.arl("exp", "new", "--type", "feature", "--track", "main-line", "--title", "new code", "--hyp", h)


def test_commit_that_fails_at_the_start_counts_no_attempt(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    index_lock = p.root / ".git" / "index.lock"
    index_lock.write_text("")  # another git process, or what a killed one left
    assert "index.lock" in p.arl("exp", "run", eid, code=1)
    rec = p.record(eid)
    assert rec["status"] == "draft" and rec["attempts"] == []
    index_lock.unlink()
    p.arl("exp", "run", eid)
    assert len(p.record(eid)["attempts"]) == 1


def test_result_survives_a_home_checkout_on_the_wrong_branch(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    after_the_run(monkeypatch, lambda: git(p.root, "switch", "-qc", "side-work"))
    out = p.arl("exp", "run", eid)
    assert "saved but could not be committed" in out
    assert p.record(eid)["status"] == "evaluated" and p.record(eid)["metrics"]["score"] == pytest.approx(0.8)
    git(p.root, "switch", "-q", "main")
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    assert git(p.root, "status", "--porcelain", "--", "research") == ""


def test_damaged_record_header_is_named_before_anything_changes(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    other = p.hyp("another claim")
    path = p.ledger.hyp_path(other)
    path.write_text("# H002: another claim\n\nThe agent rewrote the whole file and lost the header.\n")
    for command in (("status",), ("next",), ("hyp", "list"), ("exp", "run", eid), ("verify",)):
        out = p.arl(*command, code=1)
        assert "the block between the +++ lines" in out and path.name in out and "Traceback" not in out
    rec = p.record(eid)
    assert rec["status"] == "draft" and rec["attempts"] == []
    track = p.research / "tracks" / "main-line.md"
    path.unlink()
    track.write_text("# Track main-line\n\nAll of it rewritten.\n")
    assert "main-line.md is missing or damaged" in p.arl("next", code=1)


def test_ids_are_ids(project: Project):
    p = project
    h = p.started()
    for bad in ("", "E*", "e0001", "E0001/", "../x", str(p.research / "experiments" / "E0001-baseline"), "E0001-baseline"):
        assert "is not an experiment id like E0007" in p.arl("exp", "show", bad, code=1)
        assert "is not an experiment id" in p.arl("exp", "log", bad, code=1)
    for bad in ("H*", "h001", "H001-x-nearer-the-target-scores-higher"):
        assert "is not a hypothesis id like H003" in new_exp(p, "tune", bad, code=1)
    assert "no hypothesis H999" in new_exp(p, "tune", "H999", code=1)
    assert "is not a review id" in p.arl("review", "close", "R1", "--decision", "continue", code=1)
    assert "is not an idea id" in p.arl("idea", "set", "I*", "--status", "parked", "--note", "x", code=1)
    assert "no track ''" in p.arl("track", "set", "", "--status", "parked", "--note", "x", code=1)
    assert not list((p.research / "experiments").glob("E0002*")) and h


def test_files_removed_by_hand(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("exp", "run", eid)
    design = p.ledger.exp_dir(eid) / "design.md"
    text = design.read_text()
    design.unlink()
    assert "design.md is missing" in p.judge(eid, "keep", "--hyp", f"{h}=supported", code=1)
    assert "design.md is missing" in p.arl("verify", code=1)
    design.write_text(text)
    shutil.rmtree(p.ledger.exp_dir(eid) / "artifacts")
    (p.research / "experiments" / "E0009-stray").mkdir()
    assert "E0009-stray has no record.json" in p.arl("verify", code=1)
    p.judge(eid, "keep", "--hyp", f"{h}=supported")


def test_text_that_is_typed(project: Project):
    p = project
    p.setup()
    bad = "caf\udce9"  # a byte that is not UTF-8, as the shell hands it over
    assert "not valid UTF-8" in p.arl("hyp", "new", "--title", bad, code=1)
    assert "not valid UTF-8" in p.arl("idea", "add", "--title", "fine", "--body", bad, code=1)
    assert "the title is empty" in p.arl("idea", "add", "--title", "  \n ", "--body", "text", code=1)
    p.arl("idea", "add", "--title", "two\nlines", "--body", "first\n## I050: smuggled\nStatus: rejected\n# a heading\nlast")
    ideas = p.ledger.ideas()
    assert [(i["id"], i["title"], i["status"]) for i in ideas] == [("I001", "two lines", "open")]
    p.arl("idea", "add", "--title", "Über größe αβγ 🚀 | pipes | {id}", "--body", "FILL: not a placeholder here")
    assert p.ledger.ideas()[1]["title"] == "Über größe αβγ 🚀 | pipes | {id}"
    out = p.arl("hyp", "new", "--title", 'He said "x = y"\\ then left')
    assert p.ledger.hypothesis(out.split(":")[0])[0]["title"] == 'He said "x = y"\\ then left'
    assert p.arl("verify").strip() == "ok"


def test_init_keeps_what_is_there(project: Project):
    p = project
    p.research.mkdir()
    (p.research / "IDEAS.md").write_text("# Ideas\n\nmine\n")
    (p.research / "PROGRAM.md").write_text("# Program\n\nmine too\n")
    out = p.arl("init")
    assert "left as they were: PROGRAM.md, IDEAS.md" in out
    assert "mine" in (p.research / "IDEAS.md").read_text() and "mine too" in (p.research / "PROGRAM.md").read_text()
    assert (p.research / "arl.toml").exists()


def test_editor_leftovers_beside_the_evaluator(project: Project):
    p = project
    h = p.started()
    for name in (".evaluate.py.swp", "evaluate.py~", ".#evaluate.py", "#evaluate.py#"):
        (p.research / "eval" / name).write_text("an editor was here")
    assert p.arl("verify").strip() == "ok"
    p.tune(0.5, "keep", h)
    (p.research / "eval" / "helper.py").write_text("# a real new file\n")
    assert "new file: research/eval/helper.py" in new_exp(p, "tune", h, code=1)


def test_research_copy_in_a_worktree_can_be_put_back(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    (wt / "research" / "notes.md").write_text("in the wrong checkout")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "by hand")
    assert "which belongs to the home checkout" in p.arl("exp", "run", eid, code=1)
    (wt / "research" / "notes.md").unlink()
    program = wt / "research" / "PROGRAM.md"
    program.write_text(program.read_text() + "\nedited in the worktree\n")
    assert "PROGRAM.md" in p.arl("exp", "commit", eid, "-m", "undo", code=1)
    git(wt, "restore", "research/PROGRAM.md")
    p.arl("exp", "commit", eid, "-m", "take the stray note out again")
    p.arl("exp", "run", eid)


def test_environment_and_vanished_directory(project: Project, monkeypatch, tmp_path):
    p = project
    p.started()
    monkeypatch.setenv("ARL_HOME", str(tmp_path / "elsewhere"))  # as a run exports it to its job
    assert "Tracks" in p.arl("status")
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert "current directory no longer exists" in p.arl("next", code=1)


def test_older_records_survive_a_new_metric(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("exp", "run", eid)
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text().replace('"cost": abs(x)}', '"cost": abs(x), "extra": 1.0}'))
    toml = p.research / "arl.toml"
    toml.write_text(toml.read_text().replace("[budget]", '[[metric]]\nname = "extra"\nrole = "info"\n\n[budget]'))
    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("freeze", "--reason", "one more metric", "--yes")
    assert "score" in p.arl("exp", "show", "E0001")
    assert "verdict: keep" in p.judge(eid, "keep", "--hyp", f"{h}=supported")
    assert "NEXT: baseline main-line" in p.arl("next")
