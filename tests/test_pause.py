"""Stopping work and taking it up again: a run, and the loop as a whole.

A run is made of parts. A pause, a closed terminal or a killed arl ends a part
without failing the run: it continues later in the same folder, from the state
the job last saved. The loop is paused with a note for whoever continues.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest

from arl import cli, runner
from conftest import Project, cut_off, git, job_pid, launch, pause_record, pid_alive, prediction, saved_step, wait_for

SLOW = {"x": 0.5, "steps": 60, "step_s": 0.1}  # six seconds of work, saved after every step


def start_slow_run(p: Project, h: str, params: dict | None = None):
    """A run in a process of its own, far enough along to have saved some state."""
    eid = p.draft("tune", hyps=(h,), params=params or SLOW)
    proc = launch(p, "exp", "run", eid)
    assert wait_for(lambda: saved_step(p, eid) >= 3), proc.communicate(timeout=30)[0]
    return eid, proc


# --- one run --------------------------------------------------------------------------


def test_run_is_paused_and_continued_from_its_saved_state(project: Project):
    p = project
    h = p.started()
    eid, proc = start_slow_run(p, h)
    assert "NEXT: running E0002" in p.arl("next") and f"exp pause {eid}" in p.arl("next")
    assert "was asked to save its state and stop" in p.arl("exp", "pause", eid)
    out = proc.communicate(timeout=60)[0]
    assert proc.returncode == 3 and "PAUSED" in out and f"exp run {eid}" in out
    rec = p.record(eid)
    attempt = rec["attempts"][0]
    assert rec["status"] == "paused" and len(rec["attempts"]) == 1 and "failed" not in attempt
    assert attempt["paused"] == "a pause was asked for" and attempt["segments"][0]["ended_by"] == "paused"
    assert "last_checkpoint" in attempt and not pid_alive(job_pid(p, eid))
    at = saved_step(p, eid)
    assert 3 <= at < 60
    out = p.arl("next")
    assert f"NEXT: resume {eid}" in out and f"exp fail {eid}" in out
    assert "PAUSED: a pause was asked for" in p.arl("exp", "show", eid)

    out = p.arl("exp", "run", eid)  # the same folder, the same attempt, from where it was
    assert "NEXT: judge E0002" in out
    rec = p.record(eid)
    attempt = rec["attempts"][0]
    assert rec["status"] == "evaluated" and len(rec["attempts"]) == 1
    assert [s["ended_by"] for s in attempt["segments"]] == ["paused", "finished"]
    assert prediction(p, eid)["continued_from"] == at  # ARL_RESUME was 1 and the job read its state
    assert attempt["run_seconds"] == pytest.approx(sum(s["seconds"] for s in attempt["segments"]), abs=0.02)
    assert "in 2 parts (paused and continued)" in (p.ledger.exp_dir(eid) / "analysis.md").read_text()
    assert "checkpoint/step.json" not in rec["kept"]  # saved state is not a result
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    assert p.arl("verify").strip() == "ok"


def test_closed_terminal_pauses_the_run(project: Project):
    p = project
    h = p.started()
    eid, proc = start_slow_run(p, h)
    job = job_pid(p, eid)
    os.kill(proc.pid, signal.SIGHUP)  # the terminal the run was started from goes away
    proc.communicate(timeout=60)
    assert proc.returncode == 130 and not pid_alive(job)
    rec = p.record(eid)
    assert rec["status"] == "paused" and rec["attempts"][0]["segments"][0]["ended_by"] == "interrupted"
    assert "child" not in rec and "pid" not in rec
    at = saved_step(p, eid)
    p.arl("exp", "run", eid)
    assert prediction(p, eid)["continued_from"] == at and len(p.record(eid)["attempts"]) == 1


def test_killed_arl_leaves_a_job_that_is_stopped_and_the_run_continues(project: Project):
    p = project
    h = p.started()
    eid, proc = start_slow_run(p, h, {"x": 0.5, "steps": 600, "step_s": 0.01})
    job = job_pid(p, eid)
    proc.kill()  # no chance to tidy up: the job goes on alone
    proc.communicate(timeout=30)
    assert pid_alive(job) and p.record(eid)["status"] == "running"
    assert f"NEXT: interrupted {eid}" in p.arl("next")
    p.arl("exp", "run", eid)  # stops what was left, then goes on from what it had saved
    assert not pid_alive(job)
    rec = p.record(eid)
    first = rec["attempts"][0]["segments"][0]
    assert rec["status"] == "evaluated" and len(rec["attempts"]) == 1
    assert first["ended_by"] == "interrupted" and first["note"] == "the job it left behind was stopped"
    assert prediction(p, eid)["continued_from"] >= 3


def test_job_that_does_not_listen_is_forced_after_the_grace(project: Project):
    p = project
    h = p.started(grace=1)
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "deaf": True, "sleep": 120})
    proc = launch(p, "exp", "run", eid)
    assert wait_for((p.ledger.exp_dir(eid) / "artifacts" / "run-1" / "deaf.txt").exists)  # it ignores SIGTERM from here on
    sid = p.record(eid)["child"]["sid"]
    assert runner.session_pids(sid)
    p.arl("exp", "pause")  # no id: whatever is running
    proc.communicate(timeout=60)
    assert proc.returncode == 3 and not runner.session_pids(sid)
    assert p.record(eid)["attempts"][0]["exit_code"] == -signal.SIGKILL
    assert "nothing is running" in p.arl("exp", "pause", code=1)
    # a paused run that should not go on is judged as it is
    p.judge(eid, "crash")
    assert "NEXT: hypothesize" in p.arl("next")


def test_giving_up_a_paused_run_starts_over(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    assert "Only a paused run" in p.arl("exp", "fail", eid, "--reason", "too early", code=1)
    pause_record(p, eid)
    out = p.arl("exp", "fail", eid, "--reason", "the saved state is from a broken step")
    assert "attempt 1 counts as failed" in out and "1 attempt(s) left" in out
    rec = p.record(eid)
    assert rec["status"] == "crashed" and rec["attempts"][0]["failed"] == "not continued: the saved state is from a broken step"
    p.arl("exp", "run", eid)
    assert (p.ledger.exp_dir(eid) / "artifacts" / "run-2" / "prediction.json").exists()
    assert prediction(p, eid, "run-2")["continued_from"] == 0 and len(p.record(eid)["attempts"]) == 2


def test_paused_run_continues_only_what_it_started(project: Project, monkeypatch):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,), offset=0.6)
    pause_record(p, eid)
    p.edit_code("main-line", 0.1)
    assert "has uncommitted changes" in p.arl("exp", "run", eid, code=1)
    assert "a run continues the code it started with" in p.arl("exp", "commit", eid, "-m", "a change in the middle", code=1)
    (wt / "run.py").write_text((wt / "run.py").read_text().replace("OFFSET = 0.1", "OFFSET = 0.6"))
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# stricter\n")
    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("freeze", "--reason", "stricter", "--yes")
    monkeypatch.setattr(cli, "interactive", lambda: False)
    out = p.arl("exp", "run", eid, code=1)
    assert "the contract was frozen anew since" in out and "--verdict crash" in out
    p.judge(eid, "crash")
    assert "NEXT: baseline main-line" in p.arl("next")


def test_time_limit_holds_across_the_parts_of_a_run(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    pause_record(p, eid, seconds=100.0)  # the limit is 60 s
    assert "passed its limit of 60 s over all its parts" in p.arl("exp", "run", eid, code=2)
    rec = p.record(eid)
    assert rec["status"] == "crashed" and rec["attempts"][0]["run_seconds"] == pytest.approx(100.0)


def test_interrupted_time_counts_toward_the_hours(project: Project):
    p = project
    h = p.started(budget="max_run_hours = 0.02")
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    pause_record(p, eid, seconds=100.0)  # 0.028 hours, of which nothing became a result
    assert p.ledger.cfg.max_run_hours == 0.02
    out = p.arl("next")
    assert "NEXT: budget" in out and "run hours used" in out
    assert "The budget is spent" in p.arl("exp", "run", eid, code=1)
    p.judge(eid, "crash")


def test_pause_that_arrives_during_the_evaluation(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    real, calls = runner.execute, []

    def execute(command, *args, **kwargs):
        calls.append(command)
        if len(calls) == 2:  # the evaluator: asked to stop before it wrote anything
            return {"exit_code": -15, "seconds": 0.5, "stopped": "", "paused": "a pause was asked for"}
        return real(command, *args, **kwargs)

    monkeypatch.setattr(runner, "execute", execute)
    out = p.arl("exp", "run", eid, code=3)
    assert "during the evaluation; the run itself is complete" in out
    rec = p.record(eid)
    assert rec["status"] == "paused" and rec["attempts"][0]["run_done"] and "metrics" not in rec
    p.arl("exp", "run", eid)
    assert ["run.py" in c for c in calls] == [True, False, False]  # the run itself was not done twice
    assert p.record(eid)["metrics"]["score"] == pytest.approx(0.8)


def test_machine_low_on_memory_pauses_instead_of_failing(project: Project, monkeypatch):
    p = project
    h = p.started(extra="[machine]\nstop_below_free_ram_gb = 4\n")
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "steps": 30, "step_s": 0.1})
    monkeypatch.setattr(runner, "_mem_available_gb", lambda: 1.0)
    out = p.arl("exp", "run", eid, code=3)
    assert "the machine was down to 1.0 GB of free RAM, the floor is 4" in out
    rec = p.record(eid)
    assert rec["status"] == "paused" and "failed" not in rec["attempts"][0]
    monkeypatch.setattr(runner, "_mem_available_gb", lambda: 50.0)
    p.arl("exp", "run", eid)
    assert prediction(p, eid)["continued_from"] >= 1 and len(p.record(eid)["attempts"]) == 1


def test_job_is_told_how_to_save_and_whether_it_continues(project: Project):
    p = project
    p.started()
    p.arl("exp", "new", "--type", "diagnose", "--track", "main-line", "--title", "what the job is told", "--command", "env > {out}/env.txt")
    p.fill(p.ledger.exp_dir("E0002") / "design.md")
    p.arl("exp", "run", "E0002")
    told = dict(line.split("=", 1) for line in (p.ledger.exp_dir("E0002") / "artifacts" / "run-1" / "env.txt").read_text().splitlines() if line.startswith("ARL_"))
    assert told["ARL_RESUME"] == "0" and told["ARL_CHECKPOINT_EVERY"] == "1"
    assert told["ARL_OUT"].endswith("artifacts/run-1") and told["ARL_ID"] == "E0002"


def test_long_part_without_saved_state_is_pointed_out(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "sleep": 4})  # state is due every second
    out = p.arl("exp", "run", eid)
    assert "saved no state under {out}/checkpoint: a pause loses that work" in out
    assert "warning" in p.record(eid)["attempts"][0]


# --- the loop as a whole ----------------------------------------------------------------


def test_loop_is_paused_with_a_note_and_resumed(project: Project):
    p = project
    h = p.started()
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    out = p.arl("pause", "--reason", "the laptop is needed", "--note", "E0002 is designed and ready; after it I wanted x = 0.6")
    assert "The loop is paused" in out and "No note was left" not in out
    out = p.arl("next")
    assert "NEXT: paused" in out and "STOP" in out and "the laptop is needed" in out
    assert p.arl("status", "--json").count('"reason": "the laptop is needed"') == 1
    for command in (
        ("exp", "run", draft),
        ("exp", "new", "--type", "tune", "--track", "main-line", "--title", "one more", "--hyp", h),
        ("exp", "abandon", draft, "--reason", "x"),
        ("track", "new", "side"),
        ("introspect", "new"),
        ("exp", "reproduce", "E0001"),
    ):
        assert "The loop is paused" in p.arl(*command, code=1)
    # what is seen or thought of meanwhile can still be written down
    p.arl("note", "--about", "problem", "x above 0.7 has not been tried")
    p.arl("idea", "add", "--title", "a negative offset", "--body", "cheap to test")
    assert "paused already" in p.arl("pause", "--reason", "again", code=1)
    assert "kept with the pause" in p.arl("pause", "--note", "Also: check the noise first.")  # thought of afterwards

    out = p.arl("resume")
    assert "the laptop is needed" in out and "E0002 is designed and ready; after it I wanted x = 0.6 Also: check the noise first." in out
    assert "What we know about the problem" in out and "how to work in this project" in out
    assert "NEXT: design E0002" in out
    assert "not paused" in p.arl("resume", code=1)
    p.arl("exp", "run", draft)
    kinds = [e["kind"] for e in p.ledger.activity()]
    assert "paused" in kinds and "resumed" in kinds
    assert p.arl("verify").strip() == "ok"
    assert git(p.root, "status", "--porcelain", "--", "research") == ""  # the pause and its end are in git too


def test_pause_set_at_a_terminal_is_lifted_at_a_terminal(project: Project, monkeypatch):
    p = project
    p.started()
    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("pause", "--reason", "away until Monday")
    assert p.ledger.paused()["by"] == "a person at a terminal"
    monkeypatch.setattr(cli, "interactive", lambda: False)
    out = p.arl("resume", code=1)  # the agent does not decide that Monday has come
    assert "paused by a person at a terminal" in out and p.ledger.paused() is not None
    assert "by a person at a terminal: away until Monday" in p.arl("next")
    monkeypatch.setattr(cli, "interactive", lambda: True)
    assert "NEXT: hypothesize" in p.arl("resume")


def test_pausing_the_loop_stops_the_run_that_is_going(project: Project):
    p = project
    h = p.started()
    eid, proc = start_slow_run(p, h)
    out = p.arl("pause")  # a stop is not refused for a missing reason or note
    assert f"{eid} was asked to save its state and stop" in out and "No note was left" in out
    assert p.ledger.paused()["reason"] == "not given"
    proc.communicate(timeout=60)
    assert proc.returncode == 3 and p.record(eid)["status"] == "paused"
    assert "The loop is paused" in p.arl("exp", "run", eid, code=1)
    out = p.arl("resume")
    assert f"NEXT: resume {eid}" in out
    p.arl("exp", "run", eid)
    assert p.record(eid)["status"] == "evaluated"


def test_interrupted_run_found_at_the_start_of_a_session(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    cut_off(p, eid)  # the machine went down in the middle of a run; nobody paused anything
    out = p.arl("next")
    assert f"NEXT: interrupted {eid}" in out and "Nothing is counted as a failure" in out
    p.arl("exp", "run", eid)
    rec = p.record(eid)
    assert rec["status"] == "evaluated" and len(rec["attempts"]) == 1 and len(rec["attempts"][0]["segments"]) == 2


def test_pausing_the_loop_stops_a_rerun_as_well(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "steps": 30, "step_s": 0.1})
    p.arl("exp", "run", eid)
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    proc = launch(p, "exp", "reproduce", eid)
    job = job_pid(p, eid, "rerun-1-dev")
    out = p.arl("pause", "--reason", "the machine is needed")
    assert f"The rerun of {eid} was asked to stop" in out and "run it again later" in out
    assert "a pause was asked for" in proc.communicate(timeout=60)[0]
    assert proc.returncode == 2 and not pid_alive(job)
    rec = p.record(eid)
    assert "rerun" not in rec and "A rerun is not continued" in rec["reruns"][0]["failed"]
    assert rec["status"] == "judged"  # the experiment itself is untouched
    p.arl("resume")
    assert "reproduced: True" in p.arl("exp", "reproduce", eid)


# --- the helper a job can copy ------------------------------------------------------------

HELPER = Path(__file__).resolve().parent.parent / "helpers" / "arl_job.py"

JOB_PY = """\
import json, os, pathlib, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arl_job import Job

job = Job()
state = job.load() or {"step": 0, "starts": []}
state["starts"].append(state["step"])
while state["step"] < 50:
    time.sleep(0.1)
    state["step"] += 1
    if job.due():
        job.save(state)
    if job.stopping:
        job.leave()
pathlib.Path(os.environ["ARL_OUT"], "result.json").write_text(json.dumps({"starts": state["starts"], "saves": job.saves}))
"""


def test_job_written_with_the_helper_is_paused_and_continued(project: Project):
    p = project
    shutil.copy(HELPER, p.root / "arl_job.py")
    (p.root / "job.py").write_text(JOB_PY)
    git(p.root, "add", "-A")
    git(p.root, "commit", "-qm", "a job that saves its state")
    p.started()
    p.arl("exp", "new", "--type", "diagnose", "--track", "main-line", "--title", "a job that saves", "--command", f"{sys.executable} job.py")
    p.fill(p.ledger.exp_dir("E0002") / "design.md")
    out_dir = p.ledger.exp_dir("E0002") / "artifacts" / "run-1"
    proc = launch(p, "exp", "run", "E0002")
    assert wait_for((out_dir / "checkpoint" / "state.json").exists), proc.communicate(timeout=30)[0]  # a second in: the first save
    p.arl("exp", "pause", "E0002")
    proc.communicate(timeout=60)
    assert proc.returncode == 3 and p.record("E0002")["attempts"][0]["exit_code"] == 143
    at = json.loads((out_dir / "checkpoint" / "state.json").read_text())["step"]
    p.arl("exp", "run", "E0002")
    result = json.loads((out_dir / "result.json").read_text())
    assert result["starts"] == [0, at] and 5 <= at < 50
    assert 1 <= result["saves"] <= 8  # about once a second, not after each of the steps


def test_helper_without_arl(tmp_path, monkeypatch):
    sys.path.insert(0, str(HELPER.parent))
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        from arl_job import Job

        for name in ("ARL_OUT", "ARL_RESUME", "ARL_CHECKPOINT_EVERY"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(SystemExit, match="no output folder"):
            Job()
        job = Job(tmp_path)
        assert (job.every, job.resuming, job.due()) == (600.0, False, False)
        job.save({"step": 3})
        assert job.load() is None and job.saved() is None  # not told to resume: it starts from nothing
        os.kill(os.getpid(), signal.SIGTERM)  # asked to stop: a flag, not an exception
        assert job.stopping and job.due()
        with pytest.raises(SystemExit) as left:
            job.leave()
        assert left.value.code == 143

        monkeypatch.setenv("ARL_RESUME", "1")
        monkeypatch.setenv("ARL_CHECKPOINT_EVERY", "0")
        monkeypatch.setenv("ARL_OUT", str(tmp_path))
        job = Job()
        assert job.load() == {"step": 3} and job.due()  # an interval of 0 saves at every chance
        job.write(lambda tmp: tmp.write_bytes(b"\x00\x01"), "arrays.bin")
        assert job.saved("arrays.bin").read_bytes() == b"\x00\x01" and job.saves == 1
        assert sorted(path.name for path in (tmp_path / "checkpoint").iterdir()) == ["arrays.bin", "state.json"]
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)
        sys.path.remove(str(HELPER.parent))
