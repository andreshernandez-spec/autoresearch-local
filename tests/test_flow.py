"""The loop end to end on the toy project: every stage and every gate."""

from __future__ import annotations

import json
import sys
import time

import pytest

from arl import cli
from conftest import Project, git


def new_exp(p: Project, typ: str, h: str | None = None, code: int = 0) -> str:
    args = ["exp", "new", "--type", typ, "--track", "main-line", "--title", "next one"]
    return p.arl(*args, *(["--hyp", h] if h else []), code=code)


def test_setup_to_first_reference(project: Project):
    p = project
    assert "NEXT: setup" in p.arl("next")
    p.arl("init")
    assert "NEXT: freeze" in p.arl("next")
    assert "PROGRAM.md is not written yet" in p.arl("freeze", "--reason", "initial", code=1)
    (p.research / "arl.toml").unlink()
    (p.research / "PROGRAM.md").unlink()
    p.setup()
    assert "NEXT: track" in p.arl("next")

    wt = p.track()
    assert wt.exists() and git(wt, "branch", "--show-current") == "track/main-line"
    assert "NEXT: baseline main-line" in p.arl("next")
    assert "no reference" in new_exp(p, "tune", p.hyp(), code=1)

    eid = p.draft("baseline")
    assert "NEXT: design E0001" in p.arl("next")
    out = p.arl("exp", "run", eid)
    assert "outcome: reference" in out and "NEXT: judge E0001" in out
    rec = p.record(eid)
    assert rec["status"] == "evaluated" and rec["metrics"]["score"] == pytest.approx(0.3)
    assert rec["code_commit"] == git(wt, "rev-parse", "HEAD")
    assert rec["conditions"]["env"]["host"] and rec["contract"]["version"] == 1
    assert [a["path"] for a in rec["artifacts"]] == ["prediction.json", "weights.bin"]
    assert rec["kept"] == ["prediction.json"]

    p.judge(eid, "keep")
    assert p.ledger.track("main-line")["head_experiment"] == eid
    assert "NEXT: hypothesize" in p.arl("next")
    assert p.arl("verify").strip() == "ok"
    assert git(p.root, "status", "--porcelain", "--", "research") == ""
    assert "E0001" in (p.research / "STATUS.md").read_text()
    # big artifacts and logs stay out of git, the small result file goes in
    tracked = git(p.root, "ls-files", "research/experiments")
    assert "kept/prediction.json" in tracked and "weights.bin" not in tracked and "logs/" not in tracked


def test_tune_outcomes_and_verdicts(project: Project):
    p = project
    h = p.started()

    better = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("exp", "run", better)
    assert p.record(better)["comparison"]["outcome"] == "improve"
    assert "say what this run did to each hypothesis" in p.judge(better, "keep", code=1)
    p.judge(better, "keep", "--hyp", f"{h}=supported")
    assert json.loads((p.research / "tracks" / "main-line.params.json").read_text()) == {"x": 0.5}
    assert p.ledger.hypothesis(h)[0]["status"] == "supported"

    worse = p.draft("tune", hyps=(h,), params={"x": 0.2})
    p.arl("exp", "run", worse)
    assert p.record(worse)["comparison"]["outcome"] == "worse"
    assert "the verdict can be discard" in p.judge(worse, "keep", "--hyp", f"{h}=open", code=1)
    p.judge(worse, "discard", "--hyp", f"{h}=open")
    assert json.loads((p.research / "tracks" / "main-line.params.json").read_text()) == {"x": 0.5}

    same = p.draft("tune", hyps=(h,), params={"x": 0.505})
    p.arl("exp", "run", same)
    assert p.record(same)["comparison"]["outcome"] == "neutral"
    assert "--simpler" in p.judge(same, "keep", "--hyp", f"{h}=open", code=1)
    p.judge(same, "keep", "--simpler", "--hyp", f"{h}=open")
    assert p.ledger.track("main-line")["head_experiment"] == same

    outside = p.draft("tune", hyps=(h,), params={"x": -2.5})
    p.arl("exp", "run", outside)
    comp = p.record(outside)["comparison"]
    assert comp["outcome"] == "gate_fail" and "cost" in comp["gate_failures"][0]
    p.judge(outside, "discard", "--hyp", f"{h}=open")
    assert p.arl("verify").strip() == "ok"


def test_feature_keep_moves_the_track_discard_does_not(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    start = git(wt, "rev-parse", "HEAD")

    kept = p.draft("feature", hyps=(h,), offset=0.6)
    assert git(wt, "branch", "--show-current").startswith("exp/E0002")
    assert git(wt, "log", "-1", "--format=%s") == "E0002: set offset to 0.6"
    p.arl("exp", "run", kept)
    p.judge(kept, "keep", "--hyp", f"{h}=supported")
    head = git(wt, "rev-parse", "HEAD")
    assert git(wt, "branch", "--show-current") == "track/main-line"
    assert head != start and head == p.record(kept)["code_commit"]

    dropped = p.draft("feature", hyps=(h,), offset=0.1)
    p.arl("exp", "run", dropped)
    p.judge(dropped, "discard", "--hyp", f"{h}=open")
    assert git(wt, "rev-parse", "HEAD") == head
    assert "OFFSET = 0.6" in (wt / "run.py").read_text()
    assert len(git(p.root, "branch", "--list", "exp/*").splitlines()) == 2  # nothing is deleted
    assert p.arl("verify").strip() == "ok"


def test_feature_runs_only_committed_code(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("feature", hyps=(h,))
    assert "no code change is committed" in p.arl("exp", "run", eid, code=1)
    p.edit_code("main-line", 0.3)
    assert "uncommitted changes" in p.arl("exp", "run", eid, code=1)
    (wt / "research" / "note.txt").write_text("in the wrong checkout")
    assert "belongs to the home checkout" in p.arl("exp", "commit", eid, "-m", "x", code=1)
    (wt / "research" / "note.txt").unlink()
    p.arl("exp", "commit", eid, "-m", "use offset 0.3")
    p.arl("exp", "run", eid)


def test_diagnose_is_not_scored_and_never_merged(project: Project):
    p = project
    p.started()
    wt = p.base / "wt" / "toy-main-line"
    eid = p.draft("diagnose", title="look inside", offset=0.3)
    out = p.arl("exp", "run", eid)
    assert "outcome:" not in out and "It was an observation" in out
    rec = p.record(eid)
    assert rec["status"] == "evaluated" and "metrics" not in rec
    assert "the verdict can be finding" in p.judge(eid, "keep", code=1)
    p.judge(eid, "finding")
    assert git(wt, "branch", "--show-current") == "track/main-line"
    assert "OFFSET = 0.0" in (wt / "run.py").read_text()

    command = sys.executable + " -c \"import pathlib, sys; pathlib.Path(sys.argv[1], 'seen.json').write_text('[1]')\" {out}"
    custom = p.draft("diagnose", title="own script", extra=("--command", command))
    p.arl("exp", "run", custom)
    assert p.record(custom)["kept"] == ["seen.json"]
    p.judge(custom, "finding")
    assert "--command is for diagnose" in p.arl("exp", "new", "--type", "baseline", "--track", "main-line", "--title", "t", "--command", "true", code=1)


def test_contract_is_frozen(project: Project, monkeypatch):
    p = project
    h = p.started()
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# a kinder score\n")
    assert "changed: research/eval/evaluate.py" in new_exp(p, "tune", h, code=1)
    assert "NEXT: contract" in p.arl("next")
    assert "needs a person" in p.arl("freeze", "--reason", "kinder", code=1)
    # the ledger still commits, the changed evaluator is not swept into it
    p.arl("idea", "add", "--title", "noted", "--body", "text")
    assert git(p.root, "status", "--porcelain", "--", "research") == "M research/eval/evaluate.py"

    monkeypatch.setattr(cli, "interactive", lambda: True)
    assert "needs a new baseline" in p.arl("freeze", "--reason", "kinder", "--yes")
    assert "NEXT: baseline main-line" in p.arl("next")
    assert "no reference under contract v2" in new_exp(p, "tune", h, code=1)
    p.baseline()

    # the budget is part of the contract but not of the evaluator: no new baseline
    toml = p.research / "arl.toml"
    toml.write_text(toml.read_text().replace("max_experiments = 40", "max_experiments = 41"))
    assert "changed: research/arl.toml" in new_exp(p, "tune", h, code=1)
    p.arl("freeze", "--reason", "more budget", "--yes")
    assert "NEXT: hypothesize" in p.arl("next")
    lock = json.loads((p.research / "frozen.lock").read_text())
    assert lock["version"] == 3 and [v["version"] for v in lock["history"]] == [1, 2]

    program = p.research / "PROGRAM.md"
    program.write_text(program.read_text() + "\nThe goal is now easier.\n")
    assert "changed: research/PROGRAM.md" in new_exp(p, "tune", h, code=1)


def test_a_draft_contract_can_be_refrozen_before_any_experiment(project: Project):
    p = project
    p.setup()
    p.contract(every=9)
    p.arl("freeze", "--reason", "fix the draft")
    assert "nothing changed" in p.arl("freeze", "--reason", "again", code=1)


def test_design_is_locked_when_the_run_starts(project: Project):
    p = project
    h = p.started()
    assert "is not written yet" in (new_exp(p, "tune", h) and p.arl("exp", "run", "E0002", code=1))
    p.arl("exp", "abandon", "E0002", "--reason", "test")

    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    p.arl("exp", "run", eid)
    design = p.ledger.exp_dir(eid) / "design.md"
    original = design.read_text()
    design.write_text(original + "\nI expected exactly this.\n")
    assert "changed after the run started" in p.judge(eid, "keep", "--hyp", f"{h}=supported", code=1)
    assert "design.md changed after the run started" in p.arl("verify", code=1)
    design.write_text(original)
    p.judge(eid, "keep", "--hyp", f"{h}=supported")


def test_known_result_is_not_run_twice(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h)
    again = p.draft("tune", hyps=(h,), params={"x": 0.5})
    assert "already known" in p.arl("exp", "run", again, code=1)
    p.arl("exp", "run", again, "--allow-duplicate", "see the noise")
    assert p.record(again)["duplicate_of"] == "E0002"


def test_tracks_run_side_by_side(project: Project):
    p = project
    p.setup()
    a, b = p.track("alpha"), p.track("beta")
    p.baseline("alpha")
    p.baseline("beta")
    h = p.hyp()
    on_a = p.draft("feature", "alpha", hyps=(h,), offset=0.6)
    assert "in flight on track alpha" in p.arl("exp", "new", "--type", "tune", "--track", "alpha", "--title", "t", "--hyp", h, code=1)
    on_b = p.draft("tune", "beta", hyps=(h,), params={"x": 0.4})
    p.arl("exp", "run", on_b)
    p.arl("exp", "run", on_a)
    p.judge(on_b, "keep", "--hyp", f"{h}=open")
    p.judge(on_a, "keep", "--hyp", f"{h}=supported")
    assert "OFFSET = 0.6" in (a / "run.py").read_text() and "OFFSET = 0.0" in (b / "run.py").read_text()
    assert p.record(on_a)["comparison"]["reference"] == "E0001" and p.record(on_b)["comparison"]["reference"] == "E0002"
    assert p.arl("verify").strip() == "ok"


def test_review_gate_and_new_tracks(project: Project):
    p = project
    h = p.started(every=5, stagnation=2)
    assert "No review due" in p.arl("review", "due")
    p.tune(-0.2, "discard", h)
    p.tune(-0.3, "discard", h)
    blocked = new_exp(p, "tune", h, code=1)
    assert "A review comes first" in blocked and "2 scored experiments in a row without a keep" in blocked
    assert "NEXT: review" in p.arl("next")
    assert "comes out of a review" in p.arl("track", "new", "other", code=1)

    p.arl("idea", "add", "--title", "Change the basis", "--body", "What: another basis. Cheapest test: one tune.")
    p.arl("review", "new")
    review = p.research / "reviews" / "R001.md"
    text = review.read_text()
    assert "## Facts" in text and "E0002" in text and "E0003" in text and "I001 Change the basis" in text
    assert "is not finished yet" in p.arl("review", "new", code=1)
    assert "is not written yet" in p.arl("review", "close", "R001", "--decision", "branch", code=1)
    review.write_text(text.replace("> FILL: results by the next review that would change this decision", "If two more tunes fail, park the track."))
    p.fill(review)
    assert "NEXT: review R001" in p.arl("next")
    p.arl("review", "close", "R001", "--decision", "branch")

    assert "No review due" in p.arl("review", "due")  # the count of failures starts again
    p.track("other", "--review", "R001")
    assert p.ledger.track("other")["opened_by"] == "R001"
    p.tune(-0.4, "discard", h)
    p.arl("review", "new", "--trigger", "asked")
    assert "If two more tunes fail, park the track." in (p.research / "reviews" / "R002.md").read_text()


def test_some_decisions_wait_for_a_person(project: Project, monkeypatch):
    p = project
    h = p.started()
    p.arl("review", "new", "--trigger", "asked")
    p.fill(p.research / "reviews" / "R001.md")
    assert "may not decide alone" in p.arl("review", "close", "R001", "--decision", "stop")
    assert "NEXT: wait R001" in p.arl("next")
    assert "waits for a person" in new_exp(p, "tune", h, code=1)
    assert "in a terminal" in p.arl("review", "resolve", "R001", "--decision", "continue", "--note", "go on", code=1)

    monkeypatch.setattr(cli, "interactive", lambda: True)
    p.arl("review", "resolve", "R001", "--decision", "stop", "--note", "enough")
    assert "NEXT: stopped R001" in p.arl("next")
    assert "decided to stop" in new_exp(p, "tune", h, code=1)
    p.arl("review", "new", "--trigger", "reopened")
    p.fill(p.research / "reviews" / "R002.md")
    p.arl("review", "close", "R002", "--decision", "continue")
    assert "NEXT: hypothesize" in p.arl("next")


def test_crash_retry_and_limit(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    assert "exited with code 3" in p.arl("exp", "run", eid, code=2)
    assert p.record(eid)["status"] == "crashed"
    assert "NEXT: crashed E0002" in p.arl("next")
    params = p.ledger.exp_dir(eid) / "params.json"
    params.write_text(json.dumps({"x": 0.5}))
    assert "The design is locked" in p.arl("exp", "run", eid, code=1)
    params.write_text(json.dumps({"x": 0.5, "crash": True}))
    p.arl("exp", "run", eid, code=2)
    assert "the limit" in p.arl("exp", "run", eid, code=1)
    assert "the verdict can be crash" in p.judge(eid, "discard", code=1)
    p.judge(eid, "crash")
    assert "predicted" not in p.arl("exp", "log", eid)

    wt = p.base / "wt" / "toy-main-line"
    fixable = p.draft("feature", hyps=(h,))
    (wt / "run.py").write_text("import sys\nsys.exit(7)\n")
    p.arl("exp", "commit", fixable, "-m", "break it")
    p.arl("exp", "run", fixable, code=2)
    (wt / "run.py").write_text(conftest_run_py().replace("OFFSET = 0.0", "OFFSET = 0.6"))
    p.arl("exp", "commit", fixable, "-m", "fix it")
    p.arl("exp", "run", fixable)
    rec = p.record(fixable)
    assert rec["status"] == "evaluated" and len(rec["attempts"]) == 2 and rec["attempts"][0]["failed"]


def conftest_run_py() -> str:
    from conftest import RUN_PY

    return RUN_PY


def test_time_limit_stops_the_run(project: Project):
    p = project
    h = p.started(timeout=3)
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "sleep": 60})
    start = time.time()
    assert "passed its limit" in p.arl("exp", "run", eid, code=2)
    assert time.time() - start < 30


def test_memory_limit_stops_the_run(project: Project):
    p = project
    h = p.started(extra="[machine]\nmax_run_rss_gb = 0.2\n")
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "hog_mb": 400, "sleep": 60})
    start = time.time()
    out = p.arl("exp", "run", eid, code=2)
    assert "GB of RAM, the limit is 0.2" in out and time.time() - start < 30
    assert p.record(eid)["attempts"][0]["failed"].startswith("the run was stopped")


def test_budget_ends_new_work(project: Project):
    p = project
    h = p.started(max_experiments=2)
    p.tune(0.5, "keep", h)
    assert "budget is spent" in new_exp(p, "tune", h, code=1)
    assert "NEXT: budget" in p.arl("next")


def test_reproduce_and_confirmation_split(project: Project):
    p = project
    h = p.started()
    eid = p.tune(0.5, "keep", h)
    assert "reproduced: True" in p.arl("exp", "reproduce", eid)
    assert "score = 0.78" in p.arl("exp", "reproduce", eid, "--split", "confirm")
    reruns = p.record(eid)["reruns"]
    assert [r["split"] for r in reruns] == ["dev", "confirm"] and "reproduced" not in reruns[1]
    assert not list((p.base / "wt").glob("toy-rerun-*"))
    assert "knows the splits" in p.arl("exp", "reproduce", eid, "--split", "test", code=1)
    assert p.record(eid)["verdict"]["decision"] == "keep"


def test_ideas_document(project: Project):
    p = project
    p.setup()
    assert "I001" in p.arl("idea", "add", "--title", "Another basis", "--body", "What: a basis from the data.", "--from", "reading", "--kind", "approach")
    p.arl("idea", "add", "--title", "Check the evaluator grid", "--body", "A suspicion.", "--kind", "evaluator")
    p.arl("idea", "set", "I001", "--status", "promoted", "--note", "became H001")
    listed = p.arl("idea", "list", "--status", "open")
    assert "I002" in listed and "I001" not in listed
    text = (p.research / "IDEAS.md").read_text()
    assert "Status: promoted" in text and "became H001" in text and "A suspicion." in text
    assert "1 open, 1 promoted" in p.arl("status")


def test_called_from_a_track_worktree_it_writes_home(project: Project, monkeypatch):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    monkeypatch.chdir(wt)
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    assert list((p.research / "experiments").glob(f"{eid}-*"))
    assert not list((wt / "research").glob("experiments/*"))


def test_track_changed_by_hand_needs_a_new_baseline(project: Project):
    p = project
    h = p.started()
    wt = p.base / "wt" / "toy-main-line"
    p.edit_code("main-line", 0.2)
    git(wt, "commit", "-qam", "a fix made by hand")
    assert "moved outside arl" in new_exp(p, "tune", h, code=1)
    assert "branch moved outside arl" in p.arl("verify", code=1)
    p.arl("track", "sync", "main-line", "--note", "manual fix")
    assert "NEXT: baseline main-line" in p.arl("next")


def test_interrupted_run_is_picked_up(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    rec = p.record(eid)
    rec.update(status="running", pid=2**22 - 7, attempts=[{"n": 1, "started": "2026-01-01T00:00:00+00:00"}])
    p.ledger.save_record(rec)
    assert "NEXT: interrupted E0002" in p.arl("next")
    p.arl("exp", "run", eid)
    rec = p.record(eid)
    assert rec["status"] == "evaluated" and rec["attempts"][0]["failed"].startswith("interrupted")


def test_wrong_identity_or_branch_is_refused(project: Project):
    p = project
    p.setup()
    git(p.root, "config", "user.email", "work@example.com")
    assert "Not committing" in p.arl("track", "new", "x", code=1)
    git(p.root, "config", "user.email", "test@example.org")
    git(p.root, "switch", "-q", "-c", "elsewhere")
    assert "the ledger lives on 'main'" in p.arl("track", "new", "x", code=1)


def test_target_ends_the_program(project: Project):
    p = project
    p.setup()
    toml = p.research / "arl.toml"
    toml.write_text(toml.read_text().replace("min_delta = 0.01", "min_delta = 0.01\ntarget = 0.9"))
    p.arl("freeze", "--reason", "add a target")
    p.track()
    p.baseline()
    h = p.hyp()
    p.tune(0.65, "keep", h, "supported")
    assert "NEXT: target E0002" in p.arl("next")
    assert "reached the target" in new_exp(p, "tune", h, code=1)
