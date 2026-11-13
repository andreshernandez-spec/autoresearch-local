"""The environment runs use, what an experiment records about its conditions, and
going back to an old experiment.

A contract can name the environment ([env] prefix) and the packages that must
stay the same for two results to be compared ([env] packages). These tests use
a real environment of their own, and fake a package version in it.
"""

from __future__ import annotations

import os
import shutil
import sys

from arl import __version__, cli
from conftest import Project, env_table, git, install, make_env, pause_record


def pinned(p: Project, *packages: str, **over) -> tuple:
    """A project whose contract names an environment of its own. Returns (env, hypothesis)."""
    env = make_env(p.base / "env")
    for name in packages:
        install(env, name, "1.0")
    return env, p.started(python="python", extra=env_table(env, *packages), **over)


def observe(p: Project, command: str, name: str) -> str:
    """What a job sees, through a diagnose experiment that writes it down."""
    eid = p.draft("diagnose", title=f"look at {name}", extra=("--command", f"{command} > {{out}}/{name}"))
    p.arl("exp", "run", eid)
    text = (p.ledger.exp_dir(eid) / "artifacts" / "run-1" / name).read_text()
    p.judge(eid, "finding")
    return text


# --- the environment the contract names ------------------------------------------------


def test_jobs_run_in_the_environment_the_contract_names(project: Project, monkeypatch):
    p = project
    # The shell that calls arl has other things active, as a person's terminal would.
    monkeypatch.setenv("CONDA_PREFIX", "/somewhere/else")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    monkeypatch.setenv("PYTHONPATH", str(p.base / "stray"))
    env, _ = pinned(p)
    rec = p.record("E0001")
    assert rec["status"] == "judged"  # run and evaluator found 'python' there
    assert rec["conditions"]["env"]["prefix"] == str(env)
    assert rec["conditions"]["env"]["python"] == sys.version.split()[0]
    assert os.path.realpath(observe(p, "python -c 'import sys; print(sys.prefix)'", "prefix.txt").strip()) == os.path.realpath(env)
    seen = dict(line.split("=", 1) for line in observe(p, "env", "env.txt").splitlines() if "=" in line)
    assert seen["PATH"].startswith(f"{env}/bin:")
    assert seen["VIRTUAL_ENV"] == str(env) and seen["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in seen and "CONDA_PREFIX" not in seen and "CONDA_DEFAULT_ENV" not in seen


def test_a_contract_that_names_no_environment_is_not_frozen_without_it(project: Project):
    p = project
    p.arl("init")
    p.contract(extra=env_table(p.base / "nowhere"))
    (p.research / "eval" / "evaluate.py").write_text("print('x')\n")
    p.fill(p.research / "PROGRAM.md")
    out = p.arl("freeze", "--reason", "initial", code=1)
    assert "is not on this machine" in out and "Nothing was frozen" in out
    assert not p.ledger.lock_path.exists()


def test_nothing_runs_while_the_environment_is_gone(project: Project):
    p = project
    env, h = pinned(p)
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    before = p.record(draft)
    away = env.rename(p.base / "env.away")
    out = p.arl("next")
    assert "NEXT: environment" in out and "STOP" in out and "is not on this machine" in out
    assert "do not install" in out and "another environment" in out
    for argv in (("exp", "run", draft), ("exp", "reproduce", "E0001"), ("exp", "new", "--type", "baseline", "--track", "main-line", "--title", "again")):
        refused = p.arl(*argv, code=1)
        assert "is not on this machine" in refused and "tell the person" in refused
    assert p.record(draft) == before  # a refusal leaves the draft as it was
    assert "the environment:" in p.arl("verify", code=1)
    away.rename(env)  # the person puts it back
    assert "NEXT: design E0002" in p.arl("next")
    p.arl("exp", "run", draft)
    assert p.arl("verify").strip() == "ok"


def test_a_python_that_is_not_the_environment_is_refused(project: Project):
    p = project
    fake = p.base / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin" / "python").symlink_to(os.path.realpath(sys.executable))  # a link into another environment
    p.arl("init")
    p.contract(python="python", extra=env_table(fake))
    (p.research / "eval" / "evaluate.py").write_text("print('x')\n")
    p.fill(p.research / "PROGRAM.md")
    out = p.arl("freeze", "--reason", "initial", code=1)
    assert "A job's python is" in out and "not the environment the contract names" in out


def test_an_interrupted_run_waits_for_the_environment(project: Project):
    p = project
    env, h = pinned(p)
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    pause_record(p, draft)
    away = env.rename(p.base / "env.away")
    assert "NEXT: environment" in p.arl("next")  # not 'resume': it could not be continued now
    away.rename(env)
    assert f"NEXT: resume {draft}" in p.arl("next")


def test_reload_says_where_runs_happen(project: Project):
    p = project
    env, _ = pinned(p)
    out = p.arl("reload")
    assert f"==> Environment: runs and the evaluator use {env}" in out
    assert "'python' in this shell is" in out and f"call {env}/bin/python" in out  # the tests run from another python


# --- packages that must stay the same ---------------------------------------------------


def test_a_changed_package_asks_for_a_new_baseline(project: Project):
    p = project
    env, h = pinned(p, "demo")
    first = p.record("E0001")
    assert first["conditions"]["env"]["versions"] == {"demo": "1.0"} and first["conditions"]["env_digest"]
    p.tune(0.5, "keep", h)  # compared with the baseline: same packages
    draft = p.draft("tune", hyps=(h,), params={"x": 0.6})
    install(env, "demo", "2.0")  # somebody upgrades it
    assert f"NEXT: outdated {draft}" in p.arl("next")
    out = p.arl("exp", "run", draft, code=1)
    assert "with other packages (demo 1.0 -> 2.0)" in out and "run a baseline first" in out
    p.arl("exp", "abandon", draft, "--reason", "the packages changed")
    out = p.arl("next")
    assert "NEXT: baseline main-line" in out and "measured by E0002 with other packages (demo 1.0 -> 2.0)" in out
    refused = p.arl("exp", "new", "--type", "tune", "--track", "main-line", "--title", "too early", "--hyp", h, code=1)
    assert "with other packages (demo 1.0 -> 2.0)" in refused
    again = p.baseline(params={"x": 0.5})  # the same code and params with another package: a new measurement
    assert p.record(again)["comparison"]["outcome"] == "reference"
    assert p.record(again)["conditions"]["env"]["versions"] == {"demo": "2.0"}
    p.tune(0.6, "keep", h)
    assert p.arl("verify").strip() == "ok"


def test_a_rerun_with_other_packages_says_so(project: Project):
    p = project
    env, _ = pinned(p, "demo")
    install(env, "demo", "2.0")
    out = p.arl("exp", "reproduce", "E0001")
    assert "not with the packages of the recorded run: demo 1.0 -> 2.0" in out
    rerun = p.record("E0001")["reruns"][0]
    assert rerun["packages_changed"] == ["demo 1.0 -> 2.0"] and rerun["env"]["versions"] == {"demo": "2.0"}
    assert "other packages (demo 1.0 -> 2.0)" in p.arl("exp", "show", "E0001")


def test_a_paused_run_is_not_continued_with_other_packages(project: Project):
    p = project
    env, h = pinned(p, "demo")
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    pause_record(p, draft)
    install(env, "demo", "2.0")
    out = p.arl("exp", "run", draft, code=1)
    assert "the packages changed since E0002 started (demo 1.0 -> 2.0), so it cannot be continued" in out


def test_a_named_package_has_to_be_installed(project: Project):
    p = project
    env = make_env(p.base / "env")
    p.arl("init")
    p.contract(python="python", extra=env_table(env, "nothere"))
    (p.research / "eval" / "evaluate.py").write_text("print('x')\n")
    p.fill(p.research / "PROGRAM.md")
    out = p.arl("freeze", "--reason", "initial", code=1)
    assert "names the package nothere" in out and "not installed" in out


def test_packages_are_asked_of_the_python_on_the_path_when_no_environment_is_named(project: Project, monkeypatch):
    p = project
    env = make_env(p.base / "env")
    install(env, "demo", "1.0")
    monkeypatch.setenv("PATH", f"{env}/bin:{os.environ['PATH']}")  # the shell has it active, the contract does not name it
    p.started(python="python", extra='[env]\npackages = ["demo"]\n')
    recorded = p.record("E0001")["conditions"]["env"]
    assert recorded["versions"] == {"demo": "1.0"} and "prefix" not in recorded


# --- what a record holds, and going back to an old experiment -----------------------------


def test_a_record_names_the_harness_that_ran_it(project: Project):
    p = project
    p.started()
    rec = p.record("E0001")
    assert rec["conditions"]["env"]["arl"].startswith(__version__)
    assert rec["attempts"][0]["segments"][0]["env"]["arl"].startswith(__version__)
    p.arl("exp", "reproduce", "E0001")
    assert p.record("E0001")["reruns"][0]["env"]["arl"].startswith(__version__)


def test_show_says_what_an_experiment_ran_with_and_how_to_go_back_to_it(project: Project):
    p = project
    env, h = pinned(p, "demo")
    eid = p.draft("feature", hyps=(h,), offset=0.4)
    p.arl("exp", "run", eid)
    p.judge(eid, "discard", "--hyp", f"{h}=open")
    out = p.arl("exp", "show", eid)
    assert f"environment: {env}, python {sys.version.split()[0]}, demo 1.0" in out
    assert f"ran under: contract v1, arl {__version__}" in out
    rec = p.record(eid)
    assert f"diff {rec['base_commit'][:10]} {rec['code_commit'][:10]} -- . ':(exclude)research'" in out
    assert f"exp reproduce {eid}" in out and f"track new <name> --from {eid}" in out
    analysis = (p.ledger.exp_dir(eid) / "analysis.md").read_text()
    assert f"Environment: {env}, python {sys.version.split()[0]}, demo 1.0." in analysis


def test_a_track_starts_from_an_old_experiment(project: Project, monkeypatch):
    p = project
    h = p.started()
    good = p.draft("feature", hyps=(h,), params={"x": 0.2}, offset=0.4)  # 0.9: was not kept at the time
    p.arl("exp", "run", good)
    p.judge(good, "discard", "--hyp", f"{h}=open")
    p.tune(0.3, "keep", h)  # the track went on another way
    assert "comes out of an introspection" in p.arl("track", "new", "back", "--from", good, code=1)
    monkeypatch.setattr(cli, "interactive", lambda: True)  # the person decides to go back to it
    out = p.arl("track", "new", "back", "--from", good)
    assert f"It starts from {good}" in out
    monkeypatch.setattr(cli, "interactive", lambda: False)
    p.fill(p.research / "tracks" / "back.md")
    track = p.ledger.track("back")
    assert track["base_commit"] == p.record(good)["code_commit"] and track["from_ref"] == good
    assert p.ledger.track_params(track) == {"x": 0.2}
    assert git(p.base / "wt" / "toy-back", "rev-parse", "HEAD") == p.record(good)["code_commit"]
    again = p.baseline("back", params={"x": 0.2})
    assert p.record(again)["metrics"] == p.record(good)["metrics"]  # the old result, measured again
    assert p.arl("verify").strip() == "ok"


def test_a_track_does_not_start_from_what_has_no_method(project: Project, monkeypatch):
    p = project
    h = p.started()
    monkeypatch.setattr(cli, "interactive", lambda: True)  # the person asks: what is refused is the source
    draft = p.draft("tune", hyps=(h,), params={"x": 0.5})
    assert "never ran" in p.arl("track", "new", "a", "--from", draft, code=1)
    p.arl("exp", "abandon", draft, "--reason", "not needed")
    look = p.draft("diagnose", title="look", extra=("--command", "echo seen > {out}/note.txt"))
    p.arl("exp", "run", look)
    p.judge(look, "finding")
    assert "its code observes" in p.arl("track", "new", "b", "--from", look, code=1)
    assert "no experiment E0999" in p.arl("track", "new", "c", "--from", "E0999", code=1)
    assert [t["name"] for t in p.ledger.tracks()] == ["main-line"]


def test_naming_another_environment_asks_for_a_new_baseline(project: Project, monkeypatch):
    p = project
    env, h = pinned(p)
    other = make_env(p.base / "other")
    arl_toml = p.research / "arl.toml"
    arl_toml.write_text(arl_toml.read_text().replace(str(env), str(other)))
    monkeypatch.setattr(cli, "interactive", lambda: True)  # the person moves the program to another environment
    assert "every track needs a new baseline" in p.arl("freeze", "--reason", "another environment", "--yes")
    monkeypatch.setattr(cli, "interactive", lambda: False)
    assert "NEXT: baseline main-line" in p.arl("next")
    again = p.baseline()
    assert p.record(again)["comparison"]["outcome"] == "reference"
    assert p.record(again)["conditions"]["env"]["prefix"] == str(other)


def test_a_cloned_project_goes_on_where_it_was(project: Project, monkeypatch):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h)
    broken = p.draft("feature", hyps=(h,), offset=0.1)
    run = p.base / "wt" / "toy-main-line" / "run.py"
    run.write_text(run.read_text().replace("a = p.parse_args()\n", "a = p.parse_args()\nsys.exit(3)  # a bug\n"))
    p.arl("exp", "commit", broken, "-m", "with a bug")
    p.arl("exp", "run", broken, code=2)  # failed: in flight, on a branch of its own
    # Another machine: what a clone brings. No local track branches, no worktrees, no artifacts.
    clone = p.base / "elsewhere" / "toy"
    clone.parent.mkdir()
    git(p.base, "clone", "-q", str(p.root), str(clone))
    shutil.rmtree(p.base / "wt")
    git(clone, "config", "user.email", "test@example.org")
    git(clone, "config", "user.name", "Test")
    monkeypatch.chdir(clone)
    q = Project(clone, p.base)
    assert "branch track/main-line is gone ('arl track worktree main-line' makes it again" in q.arl("verify", code=1)
    assert "Put it back with" in q.arl("exp", "run", broken, code=1)  # what an agent meets first
    out = q.arl("track", "worktree", "main-line")
    branch = q.record(broken)["branch"]
    assert "Branch track/main-line was not in this repository: made again at" in out
    assert f"Branch {branch} was not in this repository: made again at" in out and f"is back, on {branch}" in out
    wt = p.base / "wt" / "toy-main-line"
    assert git(wt, "rev-parse", "HEAD") == q.record(broken)["code_commit"]  # the experiment's code came along
    assert q.arl("verify").strip() == "ok"
    run = wt / "run.py"
    run.write_text(run.read_text().replace("sys.exit(3)  # a bug\n", ""))
    q.arl("exp", "commit", broken, "-m", "without the bug")
    q.arl("exp", "run", broken)
    assert abs(q.record(broken)["metrics"]["score"] - 0.9) < 1e-9  # x 0.5 from the track, 0.1 from its own change
    q.judge(broken, "keep", "--hyp", f"{h}=supported")
    q.arl("exp", "reproduce", "E0002")  # an old result, made again from its recorded commit
    assert q.record("E0002")["reruns"][0]["reproduced"] is True
    assert q.arl("verify").strip() == "ok"
