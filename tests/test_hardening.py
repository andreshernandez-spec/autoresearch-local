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

import pytest

from arl import contract, gitops, render, runner
from arl.commands import common
from conftest import DEAD_PID, EVAL_PY, NOTHING_NEW, Project, cut_off, git, job_pid, launch, pid_alive, wait_for


def new_exp(p: Project, typ: str, h: str | None = None, code: int = 0, track: str = "main-line") -> str:
    args = ["exp", "new", "--type", typ, "--track", track, "--title", "next one"]
    return p.arl(*args, *(["--hyp", h] if h else []), code=code)


def after_the_run(monkeypatch, action, job: int = 1):
    """Do something at the moment the run has ended and the evaluator has not started.
    job=2: at the moment the evaluator has ended."""
    real, calls = runner.execute, []

    def execute(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == job:
            action()
        return result

    monkeypatch.setattr(runner, "execute", execute)


# --- dead ends ----------------------------------------------------------------------


def test_run_cut_off_on_its_last_attempt_is_no_dead_end(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    p.arl("exp", "run", eid, code=2)
    cut_off(p, eid)  # the session dies during the second, last attempt
    assert "NEXT: interrupted E0002" in p.arl("next")
    # an interruption is not a failure: the run goes on although both attempts are used
    assert "FAILED (attempt 2 of 2)" in p.arl("exp", "run", eid, code=2)
    rec = p.record(eid)
    assert rec["status"] == "crashed" and len(rec["attempts"]) == 2
    assert [s["ended_by"] for s in rec["attempts"][1]["segments"]] == ["interrupted", "failed"]
    out = p.arl("next")
    assert "NEXT: crashed E0002" in out and "judge it as a crash" in out
    p.judge(eid, "crash")
    assert "NEXT: hypothesize" in p.arl("next")


def test_cut_off_run_is_settled_by_whichever_command_comes_next(project: Project):
    p = project
    h = p.started()
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    cut_off(p, eid)
    p.edit_code("main-line", 0.5)  # the fix the agent wants to commit after the run died
    out = p.arl("exp", "commit", eid, "-m", "fix after the interruption", code=1)
    assert "is paused in the middle of its run" in out and f"exp fail {eid}" in out
    assert p.record(eid)["status"] == "paused"  # the refused command settled it all the same
    assert "counts as failed" in p.arl("exp", "fail", eid, "--reason", "the offset was wrong")
    assert "committed" in p.arl("exp", "commit", eid, "-m", "fix after the interruption")
    p.arl("exp", "run", eid)
    rec = p.record(eid)
    assert rec["attempts"][0]["failed"] == "not continued: the offset was wrong" and "failed" not in rec["attempts"][1]
    assert rec["metrics"]["score"] == pytest.approx(0.8)

    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    stuck = p.draft("tune", hyps=(h,), params={"x": 0.2})
    cut_off(p, stuck)
    rec = p.record(stuck)
    rec.update(pid=1, pid_start=987654321)
    p.ledger.save_record(rec)
    # pid 1 is alive but it is not the arl that started the run: a reused pid must not block for ever
    assert "NEXT: interrupted" in p.arl("next")
    p.judge(stuck, "crash")


def test_run_marked_as_started_that_never_got_going(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        rec = p.record(eid)  # killed between "running" and the first line of the job
        rec.update(status="running", pid=DEAD_PID, pid_start=1, child=runner.child_ref(orphan.pid), attempts=[{"n": 1, "started": "2026-01-01T00:00:00+00:00"}])
        p.ledger.save_record(rec)
        assert "only a draft that never ran" not in p.arl("exp", "abandon", eid, "--reason", "not wanted after all")
        assert orphan.wait(timeout=20) is not None
        assert p.record(eid)["status"] == "abandoned" and p.record(eid)["attempts"] == []
    finally:
        orphan.kill()


def test_what_a_dead_run_left_behind_is_stopped(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        cut_off(p, eid, child=runner.child_ref(orphan.pid))
        p.arl("exp", "run", eid)
        assert orphan.wait(timeout=20) is not None  # no second copy of the job beside the new run
        rec = p.record(eid)
        assert rec["status"] == "evaluated" and len(rec["attempts"]) == 1
        assert rec["attempts"][0]["segments"][0]["note"] == "the job it left behind was stopped"
    finally:
        orphan.kill()


def test_leftover_job_is_stopped_whichever_experiment_runs_next(project: Project):
    p = project
    p.setup()
    p.track("alpha")
    p.track("beta")
    p.baseline("alpha")
    p.baseline("beta")
    h = p.hyp()
    left = p.draft("tune", "alpha", hyps=(h,), params={"x": 0.5})
    other = p.draft("tune", "beta", hyps=(h,), params={"x": 0.4})
    orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        cut_off(p, left, child=runner.child_ref(orphan.pid))
        p.arl("exp", "run", other)  # a command about another experiment: two jobs must not share the machine
        assert orphan.wait(timeout=20) is not None
        assert p.record(left)["status"] == "paused" and p.record(other)["status"] == "evaluated"
    finally:
        orphan.kill()


def test_a_number_from_before_is_not_mistaken_for_the_job(project: Project):
    p = project
    h = p.started()
    bystander = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        for x, change in ((0.4, {"boot": "the-boot-before-this-one"}), (0.5, {"start": 1})):
            eid = p.draft("tune", hyps=(h,), params={"x": x})
            cut_off(p, eid, child={**runner.child_ref(bystander.pid), **change})
            p.arl("exp", "run", eid)
            assert bystander.poll() is None  # same number, another process: left alone
            p.judge(eid, "discard", "--hyp", f"{h}=open")
    finally:
        bystander.kill()


def test_refrozen_contract_with_work_pending(project: Project, monkeypatch):
    p = project
    h = p.started()
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# stricter\n")
    monkeypatch.setattr(common, "interactive", lambda: True)
    p.arl("freeze", "--reason", "stricter", "--yes")
    monkeypatch.setattr(common, "interactive", lambda: False)
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


def test_parking_a_stalled_track_does_not_skip_the_introspection(project: Project, monkeypatch):
    p = project
    p.setup(every=20, stagnation=2)
    p.track("alpha")
    p.track("beta")
    p.baseline("alpha")
    p.baseline("beta")
    h = p.hyp()
    p.tune(-0.2, "discard", h, track="alpha")
    p.tune(-0.3, "discard", h, track="alpha")
    assert "An introspection comes first" in new_exp(p, "tune", h, code=1, track="beta")
    assert "comes out of an introspection" in p.arl("track", "set", "alpha", "--status", "parked", "--note", "stalled", code=1)
    monkeypatch.setattr(common, "interactive", lambda: True)
    p.arl("track", "set", "alpha", "--status", "parked", "--note", "stalled")
    monkeypatch.setattr(common, "interactive", lambda: False)
    assert "An introspection comes first" in new_exp(p, "tune", h, code=1, track="beta")

    assert p.introspect("adjust") == "N001"
    new_exp(p, "tune", h, track="beta")
    assert "is already parked" in p.arl("track", "set", "alpha", "--status", "parked", "--note", "x", "--introspection", "N001", code=1)
    p.arl("track", "set", "alpha", "--status", "active", "--note", "one more try", "--introspection", "N001")


def test_only_the_latest_introspection_opens_tracks(project: Project):
    p = project
    h = p.started()
    assert p.introspect("branch") == "N001"
    p.arl("introspect", "new", "--trigger", "asked")
    # while a newer one is open, the one before it decides nothing
    assert "N002 is not decided yet" in p.arl("track", "new", "late", "--introspection", "N001", code=1)
    p.fill(p.research / "introspections" / "N002.md")
    p.arl("introspect", "close", "N002", "--decision", "continue", *NOTHING_NEW)
    p.arl("reload")
    assert "not the latest closed introspection" in p.arl("track", "new", "late", "--introspection", "N001", code=1)
    assert "decided 'continue'" in p.arl("track", "new", "late", "--introspection", "N002", code=1)
    assert h


def test_who_may_decide_is_read_from_the_frozen_contract(project: Project):
    p = project
    p.started()
    p.arl("introspect", "new", "--trigger", "asked")
    p.fill(p.research / "introspections" / "N001.md")
    toml = p.research / "arl.toml"
    frozen = toml.read_text()
    toml.write_text(frozen.replace('"pivot"]', '"pivot", "stop"]'))
    assert "does not match the files on disk" in p.arl("introspect", "close", "N001", "--decision", "stop", *NOTHING_NEW, code=1)
    toml.write_text(frozen)
    assert "may not decide alone" in p.arl("introspect", "close", "N001", "--decision", "stop", *NOTHING_NEW)


def test_stop_budget_and_waiting_hold_for_existing_drafts(project: Project, monkeypatch):
    p = project
    h = p.started(max_experiments=2)
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    assert p.introspect("stop") == "N001"
    assert "waits for a person" in p.arl("exp", "run", draft, code=1)
    monkeypatch.setattr(common, "interactive", lambda: True)
    p.arl("introspect", "resolve", "N001", "--decision", "stop", "--note", "enough")
    assert "NEXT: stopped N001" in p.arl("next")
    assert "Nothing runs until a person reopens it" in p.arl("exp", "run", draft, code=1)
    assert "Nothing runs" in p.arl("exp", "reproduce", "E0001", code=1)
    assert p.introspect("continue") == "N002"  # after a stop even "continue" waits for a person
    p.arl("introspect", "resolve", "N002", "--decision", "continue", "--note", "go on")
    assert "has not been read again" in p.arl("exp", "run", draft, code=1)
    p.arl("reload")
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
    (p.research / "eval" / "evaluate.py").write_text(EVAL_PY)
    p.fill(p.research / "PROGRAM.md")
    p.arl("freeze", "--reason", "initial")
    p.track()
    p.baseline()
    assert "budget is spent" in p.arl("exp", "reproduce", "E0001", code=1)


def test_experiments_judged_around_an_introspection_are_counted(project: Project):
    p = project
    h = p.started(every=3)
    crashed = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    p.arl("exp", "run", crashed, code=2)
    p.arl("exp", "run", crashed, code=2)
    p.introspect("continue")  # lists E0001 only: E0002 has no verdict yet
    p.judge(crashed, "crash")  # the same second as the introspection, as likely as not
    assert "1 of 3 experiments judged since N001" in p.arl("introspect", "due")
    p.arl("introspect", "new", "--trigger", "asked")
    assert "E0002" in (p.research / "introspections" / "N002.md").read_text()


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
    assert "is not an introspection id" in p.arl("introspect", "close", "R1", "--decision", "continue", code=1)
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
    assert "--title is empty" in p.arl("idea", "add", "--title", "  \n ", "--body", "text", code=1)
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
    monkeypatch.setattr(common, "interactive", lambda: True)
    p.arl("freeze", "--reason", "one more metric", "--yes")
    assert "score" in p.arl("exp", "show", "E0001")
    assert "verdict: keep" in p.judge(eid, "keep", "--hyp", f"{h}=supported")
    assert "NEXT: baseline main-line" in p.arl("next")


# --- found by the second pass ---------------------------------------------------------


def test_killed_rerun_leaves_nothing_running_and_keeps_its_number(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "steps": 30, "step_s": 0.1})
    p.arl("exp", "run", eid)
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    proc = launch(p, "exp", "reproduce", eid)
    job = job_pid(p, eid, "rerun-1-dev")
    proc.kill()
    proc.communicate(timeout=30)
    assert pid_alive(job) and p.record(eid)["rerun"]["n"] == 1
    assert "reproduced: True" in p.arl("exp", "reproduce", eid)  # stops the job that was left, then runs its own
    assert not pid_alive(job) and not (p.base / "wt" / "toy-rerun-E0002-1").exists()
    rec = p.record(eid)
    assert "rerun" not in rec and [r["n"] for r in rec["reruns"]] == [1, 2]
    assert rec["reruns"][0]["failed"] == "interrupted: the arl that ran it was gone, its job was stopped"
    assert rec["reruns"][0]["run_seconds"] >= 0 and rec["reruns"][1]["reproduced"] is True
    assert (p.ledger.exp_dir(eid) / "artifacts" / "rerun-2-dev" / "prediction.json").exists()


def test_stopping_a_job_reaches_every_process_it_started(tmp_path):
    seen = []
    script = "import os, subprocess, time; subprocess.Popen(['sleep', '300'], preexec_fn=os.setpgrp); time.sleep(300)"
    result = runner.execute(f'{sys.executable} -c "{script}"', tmp_path, dict(os.environ), 2, tmp_path / "log.txt", on_start=seen.append)
    assert "passed its limit" in result["stopped"]
    assert runner.session_pids(seen[0]) == []  # the one that left the job's process group as well
    assert runner._memory_kb(os.getpid()) > 1000


def test_crash_leaves_a_settled_hypothesis_alone(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h, "supported")
    crashed = p.draft("tune", hyps=(h,), params={"x": 0.6, "crash": True})
    p.arl("exp", "run", crashed, code=2)
    p.arl("exp", "run", crashed, code=2)
    p.judge(crashed, "crash", "--hyp", f"{h}=open")
    meta, body = p.ledger.hypothesis(h)
    assert meta["status"] == "supported" and "E0003 (crash): left as it is" in body


def test_same_change_committed_twice_is_the_same_experiment(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    first = p.draft("feature", hyps=(h,), offset=0.6)
    p.arl("exp", "run", first)
    p.judge(first, "discard", "--hyp", f"{h}=open")
    second = p.draft("feature", hyps=(h,), offset=0.6)  # another commit, the same files
    assert git(wt, "rev-parse", "HEAD") != p.record(first)["code_commit"]
    assert f"as {first}: the result is already known" in p.arl("exp", "run", second, code=1)


def test_file_that_is_not_utf8_is_named(project: Project):
    p = project
    p.started()
    ideas = p.research / "IDEAS.md"
    good = ideas.read_bytes()
    ideas.write_bytes(b"# Ideas\n\n## I001: caf\xe9\nStatus: open\n")
    for command in (("next",), ("status",), ("idea", "list"), ("verify",), ("hyp", "new", "--title", "t")):
        out = p.arl(*command, code=1)
        assert "IDEAS.md is not UTF-8 text" in out and "Traceback" not in out
    ideas.write_bytes(good)
    (p.research / "KNOWLEDGE.md").write_bytes(b"\xff\xfe")
    assert "KNOWLEDGE.md is not UTF-8 text" in p.arl("reload", code=1)


def test_ideas_file_edited_by_hand(project: Project):
    p = project
    p.setup()
    p.arl("idea", "add", "--title", "first", "--body", "b")
    ideas = p.research / "IDEAS.md"
    ideas.write_text(ideas.read_text() + "## I001: a second first\nStatus: open\n\ntext\n\n## I7 short number\n\ntext\n\n## I003: no status line\n\ntext\n")
    out = p.arl("verify", code=1)
    assert "two entries called I001" in out and "'## I7 short number' is not read as an idea" in out and "I003 has no Status line" in out
    assert "two entries called I001" in p.arl("idea", "set", "I001", "--status", "parked", "--note", "x", code=1)
    assert p.arl("idea", "add", "--title", "next", "--body", "b").startswith("I004")
    p.arl("idea", "set", "I003", "--status", "parked", "--note", "now it has one")
    text = ideas.read_text()
    assert "## I003: no status line\nStatus: parked" in text and "## I7 short number" in text


def test_verdict_cut_off_in_the_middle_is_finished_later(project: Project, monkeypatch):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    p.arl("exp", "run", eid)
    p.fill(p.ledger.exp_dir(eid) / "analysis.md")
    real = gitops.Git.fast_forward

    def dies(self, commit):
        raise KeyboardInterrupt  # the process is stopped after the verdict is written, before the track moves

    monkeypatch.setattr(gitops.Git, "fast_forward", dies)
    p.arl("exp", "judge", eid, "--verdict", "keep", "--reason", "better", "--hyp", f"{h}=supported", code=130)
    monkeypatch.setattr(gitops.Git, "fast_forward", real)
    assert p.record(eid)["status"] == "judging" and p.ledger.track("main-line")["head_experiment"] == "E0001"
    out = p.arl("next")
    assert f"NEXT: unfinished {eid}" in out and f"exp judge {eid}" in out
    assert f"{eid}: its verdict (keep) was cut off" in p.arl("verify", code=1)
    # the verdict stands: another one can no longer be given
    out = p.arl("exp", "judge", eid, "--verdict", "discard", "--reason", "changed my mind", "--hyp", f"{h}=refuted")
    assert "was recorded before and cut off" in out and "'discard' was not applied" in out
    rec = p.record(eid)
    assert rec["status"] == "judged" and rec["verdict"]["decision"] == "keep"
    assert p.ledger.track("main-line")["head_experiment"] == eid and p.ledger.hypothesis(h)[0]["status"] == "supported"
    assert git(wt, "rev-parse", "HEAD") == rec["code_commit"] and git(wt, "branch", "--show-current") == "track/main-line"
    assert p.arl("verify").strip() == "ok"

    # cut off again, and this time the next command that writes finishes it without being asked
    second = p.draft("tune", hyps=(h,), params={"x": 0.1})
    p.arl("exp", "run", second)
    p.fill(p.ledger.exp_dir(second) / "analysis.md")
    monkeypatch.setattr(common, "finish", lambda ledger, message: (_ for _ in ()).throw(KeyboardInterrupt))
    p.arl("exp", "judge", second, "--verdict", "discard", "--reason", "worse", "--hyp", f"{h}=open", code=130)
    monkeypatch.undo()
    monkeypatch.chdir(p.root)
    third = p.draft("tune", hyps=(h,), params={"x": 0.2})
    assert p.record(second)["status"] == "judged" and p.record(third)["status"] == "draft"


def test_draft_cut_off_between_its_branch_and_its_folder(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    git(wt, "switch", "-qc", "exp/E0002-next-one")  # what a killed 'exp new' leaves: the branch, no folder
    assert "NEXT: design E0002" in new_exp(p, "feature", h)
    assert git(wt, "branch", "--show-current") == "exp/E0002-next-one"
    # the worktree moved off the draft's branch by hand: the draft still finds its branch
    git(wt, "switch", "-q", "track/main-line")
    assert "nothing to commit" in p.arl("exp", "commit", "E0002", "-m", "x", code=1)
    assert git(wt, "branch", "--show-current") == "exp/E0002-next-one"
    p.arl("exp", "abandon", "E0002", "--reason", "only a leftover")
    # the worktree left on a branch that no experiment owns
    git(wt, "switch", "-qc", "scratch")
    new_exp(p, "tune", h)
    assert git(wt, "branch", "--show-current") == "track/main-line"


def test_freeze_that_cannot_be_committed_did_not_happen(project: Project, monkeypatch):
    p = project
    p.arl("init")
    p.contract()
    (p.research / "eval" / "evaluate.py").write_text(EVAL_PY)
    p.fill(p.research / "PROGRAM.md")
    index_lock = p.root / ".git" / "index.lock"
    index_lock.write_text("")
    assert "index.lock" in p.arl("freeze", "--reason", "initial", code=1)
    assert not (p.research / "frozen.lock").exists() and "NEXT: freeze" in p.arl("next")
    index_lock.unlink()
    p.arl("freeze", "--reason", "initial")
    p.track()
    p.baseline()
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# stricter\n")
    monkeypatch.setattr(common, "interactive", lambda: True)
    index_lock.write_text("")
    assert "index.lock" in p.arl("freeze", "--reason", "stricter", "--yes", code=1)
    assert contract.load(p.ledger)["version"] == 1  # the old lock, so the change still shows as a change
    index_lock.unlink()
    p.arl("freeze", "--reason", "stricter", "--yes")
    assert contract.load(p.ledger)["version"] == 2


def test_freeze_covers_only_what_was_shown(project: Project, monkeypatch):
    p = project
    p.started()
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# one change\n")
    program = p.research / "PROGRAM.md"

    def answer(prompt):
        program.write_text(program.read_text() + "\nA rule that slipped in while the question was open.\n")
        return "y"

    monkeypatch.setattr(common, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    out = p.arl("freeze", "--reason", "one change", code=1)
    assert "changed: research/eval/evaluate.py" in out and "changed: research/PROGRAM.md" not in out
    assert "the files changed while the question was open" in out
    assert contract.load(p.ledger)["version"] == 1


def test_missing_lock_is_not_a_fresh_start(project: Project):
    p = project
    p.started()
    (p.research / "frozen.lock").unlink()
    out = p.arl("next")
    assert "NEXT: contract" in out and "STOP" in out and "although experiments exist" in out
    assert "a new freeze needs a person" in p.arl("freeze", "--reason", "again", code=1)
    assert not (p.research / "frozen.lock").exists()
    git(p.root, "restore", "research/frozen.lock")
    assert "NEXT: hypothesize" in p.arl("next")


def test_rerun_survives_a_home_checkout_on_the_wrong_branch(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.tune(0.5, "keep", h)
    after_the_run(monkeypatch, lambda: git(p.root, "switch", "-qc", "side-work"))
    out = p.arl("exp", "reproduce", eid)
    assert "the rerun is saved but could not be committed" in out and "reproduced: True" in out
    rec = p.record(eid)
    assert rec["reruns"][0]["reproduced"] is True and "rerun" not in rec


def test_result_is_kept_when_its_table_cannot_be_written(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    monkeypatch.setattr(render, "analysis_facts", lambda ledger, rec: (_ for _ in ()).throw(KeyError("reference")))
    p.arl("exp", "run", eid)
    assert p.record(eid)["status"] == "evaluated" and p.record(eid)["metrics"]["score"] == pytest.approx(0.8)
    assert "could not be written" in (p.ledger.exp_dir(eid) / "analysis.md").read_text()


def test_repeating_the_baseline_never_moves_the_reference(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h, "supported")  # the reference is now E0002
    eid = p.draft("baseline")
    assert "the result is already known" in p.arl("exp", "run", eid, code=1)
    out = p.arl("exp", "run", eid, "--allow-duplicate", "to see the noise")
    assert "outcome: repeat" in out and "finding" in out
    assert "the verdict can be finding, not keep" in p.judge(eid, "keep", code=1)
    p.judge(eid, "finding")
    assert p.ledger.track("main-line")["head_experiment"] == "E0002"
    assert "does not replace the reference" in (p.ledger.exp_dir(eid) / "analysis.md").read_text()


def test_a_draft_is_dropped_although_its_worktree_folder_is_gone(project: Project):
    """Found by the walk: a named package changed, the draft was outdated, and dropping it
    was refused for a folder that dropping does not need."""
    p = project
    h = p.started()
    draft = p.draft("feature", hyps=(h,), offset=0.2)  # on a branch of its own, checked out in the worktree
    wt = p.base / "wt" / "toy-main-line"
    shutil.rmtree(wt)
    p.arl("exp", "abandon", draft, "--reason", "its reference no longer counts")
    assert p.record(draft)["status"] == "abandoned"
    assert "is back, on track/main-line" in p.arl("track", "worktree", "main-line")
    assert git(wt, "branch", "--show-current") == "track/main-line"
    assert p.arl("verify").strip() == "ok"
    assert "NEXT: hypothesize" in p.arl("next")


def test_changed_data_asks_for_a_new_baseline(project: Project):
    p = project
    data = p.root / "data.txt"
    data.write_text("one\n")
    h = p.started(extra='[data]\nfiles = ["data.txt"]\n')
    assert p.record("E0001")["conditions"]["data"][0]["path"] == str(data)
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    data.write_text("two\n")
    assert "NEXT: outdated E0002" in p.arl("next")
    assert "no reference under contract v1 and the data as it is" in p.arl("exp", "run", draft, code=1)
    p.arl("exp", "abandon", draft, "--reason", "the data changed")
    assert "NEXT: baseline main-line" in p.arl("next")
    again = p.baseline()  # the same code and params on other data: a new measurement, not a known one
    assert p.record(again)["comparison"]["outcome"] == "reference"
    data.unlink()
    assert "nothing matches 'data.txt'" in p.arl("exp", "new", "--type", "tune", "--track", "main-line", "--title", "t", "--hyp", h, code=1)
    assert "NEXT:" in p.arl("next")  # reading goes on without the file


def test_verdict_does_not_revive_a_dropped_hypothesis(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("exp", "run", eid)
    p.arl("hyp", "set", h, "--status", "dropped", "--note", "the claim was badly put")
    assert "was dropped" in p.judge(eid, "keep", "--hyp", f"{h}=supported", code=1)
    p.judge(eid, "keep", "--hyp", f"{h}=open")
    assert p.ledger.hypothesis(h)[0]["status"] == "dropped"


def test_track_cut_off_between_its_branch_and_its_file(project: Project):
    p = project
    p.setup()
    git(p.root, "branch", "track/side")  # a killed 'track new' left the branch
    p.track("side")
    git(p.root, "branch", "track/far")  # or the branch and the worktree
    git(p.root, "worktree", "add", "-q", str(p.base / "wt" / "toy-far"), "track/far")
    p.track("far")
    git(p.root, "branch", "track/used", "HEAD~1")  # a branch somewhere else is not a leftover
    assert "exists already and is not at" in p.arl("track", "new", "used", code=1)
    (p.base / "wt" / "toy-plain").mkdir()
    assert "is not a worktree of track/plain" in p.arl("track", "new", "plain", code=1)
    assert sorted(t["name"] for t in p.ledger.tracks()) == ["far", "side"]


# --- found by a review from outside ------------------------------------------------------

QUIET = {"x": 0.5, "sleep": 60}  # a job that writes nothing, to its log or anywhere else, until it ends
DATA = '[data]\nfiles = ["data.txt"]\n'


def with_data(p: Project):
    data = p.root / "data.txt"
    data.write_text("one\n")
    return data, p.started(extra=DATA)


def test_verdict_given_while_a_rerun_is_going_stays(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "steps": 30, "step_s": 0.1})
    p.arl("exp", "run", eid)
    proc = launch(p, "exp", "reproduce", eid)
    job_pid(p, eid, "rerun-1-dev")  # the rerun is going, its evaluator has not started
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    out = proc.communicate(timeout=60)[0]
    assert proc.returncode == 0, out
    rec = p.record(eid)
    # the rerun adds its own fields to the record as it is now, not to the copy it started with
    assert rec["status"] == "judged" and rec["verdict"]["decision"] == "keep"
    assert rec["reruns"][0]["reproduced"] is True and "rerun" not in rec
    assert p.ledger.track("main-line")["head_experiment"] == eid
    assert p.arl("verify").strip() == "ok"


def test_verdict_given_while_a_rerun_is_going_stays_when_the_rerun_is_cut_short(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "steps": 100, "step_s": 0.1})
    p.arl("exp", "run", eid)
    proc = launch(p, "exp", "reproduce", eid)
    job = job_pid(p, eid, "rerun-1-dev")
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    os.kill(proc.pid, signal.SIGHUP)  # the terminal the rerun was started from goes away
    proc.communicate(timeout=60)
    assert proc.returncode == 130 and not pid_alive(job)
    rec = p.record(eid)
    assert rec["status"] == "judged" and rec["verdict"]["decision"] == "keep"
    assert rec["reruns"][0]["failed"] == "interrupted" and "rerun" not in rec
    assert p.arl("verify").strip() == "ok"


def test_rerun_says_which_data_it_read(project: Project):
    p = project
    data, _ = with_data(p)
    assert "reproduced: True" in p.arl("exp", "reproduce", "E0001")
    rec = p.record("E0001")
    assert rec["reruns"][0]["data_digest"] == rec["conditions"]["data_digest"]
    data.write_text("two\n")
    out = p.arl("exp", "reproduce", "E0001")  # the old code on other data: a number, not a reproduction
    rerun = p.record("E0001")["reruns"][1]
    assert "reproduced" not in rerun and rerun["data_digest"] != rec["conditions"]["data_digest"]
    assert "the data files are not the ones it first ran on" in out and "reproduced:" not in out
    data.unlink()  # a rerun reads the data too: without it nothing starts
    assert "nothing matches 'data.txt'" in p.arl("exp", "reproduce", "E0001", code=1)
    rec = p.record("E0001")
    assert len(rec["reruns"]) == 2 and "rerun" not in rec


def test_data_that_changes_during_a_run_fails_it(project: Project, monkeypatch):
    p = project
    data, h = with_data(p)
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    after_the_run(monkeypatch, lambda: data.write_text("two\n"))
    out = p.arl("exp", "run", eid, code=2)
    assert "the data files changed while the run was going" in out
    rec = p.record(eid)
    assert rec["status"] == "crashed" and "metrics" not in rec  # no score from an input nobody recorded
    out = p.arl("next")
    assert "NEXT: crashed E0002" in out and "has no reference any more" in out
    p.judge(eid, "crash")
    assert "NEXT: baseline main-line" in p.arl("next")


def test_data_that_changes_during_the_evaluation_fails_it(project: Project, monkeypatch):
    p = project
    data, h = with_data(p)
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    after_the_run(monkeypatch, lambda: data.write_text("two\n"), job=2)
    out = p.arl("exp", "run", eid, code=2)
    assert "the data files changed while the evaluator was running" in out
    rec = p.record(eid)
    assert rec["status"] == "crashed" and "metrics" not in rec


def test_data_that_changes_during_a_rerun_fails_it(project: Project, monkeypatch):
    p = project
    data, _ = with_data(p)
    after_the_run(monkeypatch, lambda: data.write_text("two\n"))
    out = p.arl("exp", "reproduce", "E0001", code=2)
    assert "the data files changed during the rerun" in out
    rerun = p.record("E0001")["reruns"][0]
    assert "metrics" not in rerun and "reproduced" not in rerun


def start_quiet_run(p: Project, h: str):
    """A run in a process of its own whose job says nothing: no line in the log, no file."""
    eid = p.draft("tune", hyps=(h,), params=QUIET)
    proc = launch(p, "exp", "run", eid)
    assert wait_for(lambda: "child" in p.record(eid)), proc.communicate(timeout=30)[0]
    return eid, proc, p.record(eid)["child"]["sid"]


def test_quiet_job_left_alone_is_charged_until_it_is_stopped(project: Project):
    p = project
    h = p.started()
    eid, proc, sid = start_quiet_run(p, h)
    time.sleep(1.0)
    proc.kill()  # arl is gone, the job goes on alone
    proc.communicate(timeout=30)
    time.sleep(3.5)
    assert runner.session_pids(sid)
    p.arl("exp", "fail", eid, "--reason", "not needed any more")  # finds the job and stops it
    assert not runner.session_pids(sid)
    attempt = p.record(eid)["attempts"][0]
    part = attempt["segments"][0]
    # the job ran for all of that time, whatever its log says
    assert part["ended_by"] == "interrupted" and part["seconds"] >= 4.0 and attempt["run_seconds"] >= 4.0


def test_quiet_job_that_went_down_with_arl_is_charged_until_then(project: Project):
    p = project
    h = p.started()
    eid, proc, sid = start_quiet_run(p, h)
    time.sleep(3.0)
    proc.kill()  # the machine goes down: arl and the job at the same moment
    for pid in runner.session_pids(sid):
        os.kill(pid, signal.SIGKILL)
    proc.communicate(timeout=30)
    time.sleep(3.0)
    p.arl("exp", "fail", eid, "--reason", "not needed any more")
    part = p.record(eid)["attempts"][0]["segments"][0]
    # arl's own last sign of life, not the log's last line and not the moment somebody looked
    assert part["ended_by"] == "interrupted" and 1.5 <= part["seconds"] <= 5.0


def test_quiet_rerun_left_alone_is_charged_until_it_is_stopped(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "sleep": 5})
    p.arl("exp", "run", eid)
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    proc = launch(p, "exp", "reproduce", eid)
    assert wait_for(lambda: "child" in p.record(eid).get("rerun", {})), proc.communicate(timeout=30)[0]
    sid = p.record(eid)["rerun"]["child"]["sid"]
    time.sleep(0.5)
    proc.kill()
    proc.communicate(timeout=30)
    time.sleep(3.0)
    assert runner.session_pids(sid)
    assert "there is nothing to judge" in p.judge(eid, "keep", code=1)  # refused, and it stops what was left all the same
    assert not runner.session_pids(sid)
    rerun = p.record(eid)["reruns"][0]
    assert rerun["failed"].startswith("interrupted") and rerun["interrupted"] is True and rerun["run_seconds"] >= 3.0
