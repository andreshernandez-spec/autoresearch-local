"""The environment runs use, what an experiment records about its conditions, and
going back to an old experiment.

A contract names the environment runs use ([env] prefix). Which packages it
holds is not a setting: arl keeps the list with every run and says where two
runs differ. These tests use a real environment of their own, and fake a
package version in it.
"""

from __future__ import annotations

import os
import shutil
import sys

from arl import __version__, cli, runner
from conftest import Project, env_table, git, install, make_env, pause_record


def pinned(p: Project, *packages: str, **over) -> tuple:
    """A project whose contract names an environment of its own, with these packages
    in it at version 1.0. Returns (env, hypothesis)."""
    env = make_env(p.base / "env")
    for name in packages:
        install(env, name, "1.0")
    return env, p.started(python="python", extra=env_table(env), **over)


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


# --- packages are a record, not a setting ------------------------------------------------


def test_a_changed_package_stops_nothing_and_is_said(project: Project):
    p = project
    env, h = pinned(p, "demo")
    first = p.tune(0.5, "keep", h)
    draft = p.draft("tune", hyps=(h,), params={"x": 0.6})
    install(env, "demo", "2.0")  # somebody upgrades it
    assert f"NEXT: design {draft}" in p.arl("next")  # no gate: the draft runs as it is
    p.arl("exp", "run", draft)
    analysis = (p.ledger.exp_dir(draft) / "analysis.md").read_text()
    assert f"The environment is not the one the reference {first} ran in (1 difference): demo 1.0 -> 2.0." in analysis
    p.judge(draft, "keep", "--hyp", f"{h}=supported")
    # Did the upgrade move the score? Measuring the track again says: the same code and params, on purpose.
    again = p.draft("baseline", params={"x": 0.6})
    refused = p.arl("exp", "run", again, code=1)
    assert "already known" in refused and "or after the environment changed" in refused
    install(env, "demo", "3.0")
    p.arl("exp", "run", again, "--allow-duplicate", "demo was upgraded")
    rec = p.record(again)
    assert rec["comparison"]["outcome"] == "repeat" and rec["comparison"]["gain"] == 0
    analysis = (p.ledger.exp_dir(again) / "analysis.md").read_text()
    assert f"The environment is not the one the reference {draft} ran in (1 difference): demo 2.0 -> 3.0." in analysis
    p.judge(again, "finding")  # a repeat shows the noise, or here the effect of the upgrade: it replaces nothing
    assert p.arl("verify").strip() == "ok"


def test_a_rerun_says_what_differs_from_the_environment_it_first_ran_in(project: Project):
    """A rerun does not put the old environment back. It says what changed since."""
    p = project
    env, _ = pinned(p, "demo")
    out = p.arl("exp", "reproduce", "E0001")
    assert "the environment is the one it first ran in" in out and "env_changed" not in p.record("E0001")["reruns"][0]
    install(env, "demo", "2.0")
    install(env, "other", "0.3")
    out = p.arl("exp", "reproduce", "E0001")
    assert "the environment is not the one it first ran in (2 differences): demo 1.0 -> 2.0; other 0.3 added" in out
    assert "exp env E0001" in out
    rerun = p.record("E0001")["reruns"][1]
    assert rerun["env_changed"] == {"count": 2, "first": ["demo 1.0 -> 2.0", "other 0.3 added"]}
    assert rerun["env"]["listing"] != p.record("E0001")["conditions"]["env"]["listing"]
    assert "rerun 2 on dev" in p.arl("exp", "show", "E0001") and "in another environment (2 differences: demo 1.0 -> 2.0; other 0.3 added)" in p.arl("exp", "show", "E0001")


def test_the_contract_does_not_name_packages(project: Project):
    p = project
    p.arl("init")
    p.contract(extra='[env]\npackages = ["numpy"]\n')
    (p.research / "eval" / "evaluate.py").write_text("print('x')\n")
    p.fill(p.research / "PROGRAM.md")
    out = p.arl("freeze", "--reason", "initial", code=1)
    assert "[env] packages is not an option" in out and "arl keeps the list of every package" in out


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
    assert f"environment: {env}, python {sys.version.split()[0]}; every package: research/envs/" in out
    assert "ran under: contract v1 (frozen in " in out and f", arl {__version__}" in out and f"exp env {eid}" in out
    rec = p.record(eid)
    assert f"diff {rec['base_commit'][:10]} {rec['code_commit'][:10]} -- . ':(exclude)research'" in out
    assert f"exp reproduce {eid}" in out and f"track new <name> --from {eid}" in out
    analysis = (p.ledger.exp_dir(eid) / "analysis.md").read_text()
    assert f"Environment: {env}, python {sys.version.split()[0]}." in analysis


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


# --- the package list of every run, and what changed between two runs -----------------------


def test_every_run_stores_the_packages_python_saw(project: Project):
    """No setting needed: arl asks the python of the job and keeps the list in the ledger."""
    p = project
    env = make_env(p.base / "env")
    install(env, "plain", "1.0")
    mine = p.base / "mine"  # an editable install is whatever its folder holds
    mine.mkdir()
    git(mine, "init", "-q", "-b", "main")
    git(mine, "config", "user.email", "test@example.org")
    git(mine, "config", "user.name", "Test")
    (mine / "code.py").write_text("as committed\n")
    git(mine, "add", "-A")
    git(mine, "commit", "-q", "-m", "mine")
    head = git(mine, "rev-parse", "HEAD")[:12]
    install(env, "mine", "0.1", editable=mine)
    (env / "conda-meta").mkdir()  # what conda installed has a build, also for libraries python does not list
    (env / "conda-meta" / "libblas-3.9.0-20_mkl.json").write_text("{}")
    p.started(python="python", extra=env_table(env))
    recorded = p.record("E0001")["conditions"]["env"]
    path = p.research / "envs" / f"{recorded['listing']}.txt"
    listing = path.read_text().splitlines()
    assert listing[0] == f"python=={sys.version.split()[0]}"
    assert "plain==1.0" in listing and f"mine==0.1 (editable {mine} at {head})" in listing and "conda: libblas=3.9.0=20_mkl" in listing
    assert f"research/envs/{path.name}" in git(p.root, "ls-files", "research/envs")  # part of the experiment's history
    out = p.arl("exp", "env", "E0001")
    assert "every package python saw: research/envs/" in out and "Compared with the environment as it is now: no difference" in out
    # Two changes no version number shows: the editable folder is edited, a library is rebuilt.
    (mine / "code.py").write_text("changed by hand\n")
    (env / "conda-meta" / "libblas-3.9.0-20_mkl.json").rename(env / "conda-meta" / "libblas-3.9.0-20_netlib.json")
    before = sorted(f.name for f in (p.research / "envs").iterdir())
    out = p.arl("exp", "env", "E0001")
    assert "Compared with the environment as it is now, 2 differences:" in out
    assert f"  mine 0.1 (editable {mine} at {head}) -> 0.1 (editable {mine} at {head}, modified)" in out
    assert "  conda libblas 3.9.0 20_mkl -> 3.9.0 20_netlib" in out
    assert sorted(f.name for f in (p.research / "envs").iterdir()) == before  # looking stores nothing


def test_a_project_that_names_no_environment_still_gets_the_list(project: Project):
    p = project
    p.started()
    recorded = p.record("E0001")["conditions"]["env"]
    assert "prefix" not in recorded and "packages" not in recorded
    assert (p.research / "envs" / f"{recorded['listing']}.txt").read_text().startswith("python==")


def test_the_output_of_a_command_is_stored_beside_the_list(project: Project):
    p = project
    p.started(extra='[env]\ncommand = "echo from-the-command"\n')
    recorded = p.record("E0001")["conditions"]["env"]
    assert (p.research / "envs" / f"{recorded['packages']}.txt").read_text() == "from-the-command\n"
    assert recorded["listing"] != recorded["packages"]
    assert "the output of [env] command: research/envs/" in p.arl("exp", "env", "E0001")


def test_a_changed_environment_is_said_where_a_score_is_read(project: Project):
    """Nothing is gated, and nothing goes unsaid."""
    p = project
    env = make_env(p.base / "env")
    install(env, "extra", "1.0")
    h = p.started(python="python", extra=env_table(env))
    first = p.tune(0.5, "keep", h)  # same environment as the baseline
    assert "The environment is not" not in (p.ledger.exp_dir(first) / "analysis.md").read_text()
    install(env, "extra", "2.0")  # somebody upgrades a package the contract does not name
    second = p.draft("tune", hyps=(h,), params={"x": 0.6})
    p.arl("exp", "run", second)
    analysis = (p.ledger.exp_dir(second) / "analysis.md").read_text()
    assert f"The environment is not the one the reference {first} ran in (1 difference): extra 1.0 -> 2.0." in analysis
    assert "A difference in the scores may come from there." in analysis and f"`arl exp env {first} {second}`" in analysis
    p.judge(second, "keep", "--hyp", f"{h}=supported")
    out = p.arl("exp", "env", first, second)
    assert f"Compared with the one {second} ran in, 1 difference:" in out and "  extra 1.0 -> 2.0" in out
    assert f"Compared with the one {first} ran in: no difference" in p.arl("exp", "env", "E0001", first)
    p.arl("introspect", "new", "--trigger", "asked")
    facts = (p.research / "introspections" / "N001.md").read_text()
    assert "The environment changed between runs" in facts
    assert f"- between {first} and {second} (1 difference): extra 1.0 -> 2.0" in facts
    assert "between E0001" not in facts  # the first two runs had the same one


def test_an_introspection_says_when_nothing_changed(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h)
    p.arl("introspect", "new", "--trigger", "asked")
    assert "Environment: the same for every run of this window" in (p.research / "introspections" / "N001.md").read_text()


def test_what_a_run_reports_about_itself_is_part_of_the_record(project: Project):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5, "report": {"loss": 0.25, "steps": 40, "note": "text"}})
    out = p.arl("exp", "run", eid)
    assert p.record(eid)["train_metrics"] == {"loss": 0.25, "steps": 40.0}
    assert "reported by the run itself (not scored): loss 0.25, steps 40" in out
    assert "Reported by the run itself (not scored): loss 0.25, steps 40." in (p.ledger.exp_dir(eid) / "analysis.md").read_text()
    assert p.record(eid)["comparison"]["outcome"] == "improve"  # the evaluator's score decides, as before
    p.judge(eid, "keep", "--hyp", f"{h}=supported")
    p.arl("exp", "reproduce", eid)
    assert p.record(eid)["reruns"][0]["train_metrics"] == {"loss": 0.25, "steps": 40.0}
    broken = p.draft("tune", hyps=(h,), params={"x": 0.6, "report": "not json"})
    out = p.arl("exp", "run", broken)  # a report that cannot be read does not fail the run
    assert "train_metrics" not in p.record(broken) and "train_metrics.json is not JSON" in out


def test_show_names_the_commit_that_froze_the_contract(project: Project):
    p = project
    p.started()
    frozen = git(p.root, "log", "-1", "--format=%h", "--grep", "freeze contract v1$")
    assert f"contract v1 (frozen in {frozen}: git show {frozen}:research/arl.toml)" in p.arl("exp", "show", "E0001")
    assert "[project]" in git(p.root, "show", f"{frozen}:research/arl.toml")


def test_a_run_continued_in_a_changed_environment_says_so(project: Project):
    p = project
    env = make_env(p.base / "env")
    install(env, "extra", "1.0")
    h = p.started(python="python", extra=env_table(env))
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    pause_record(p, eid)
    rec = p.record(eid)  # the first part of the run, with the environment it had
    rec["attempts"][-1]["segments"][0]["env"] = rec["conditions"]["env"] = runner.environment(p.ledger, runner.probe_env(p.ledger))
    p.ledger.save_record(rec)
    install(env, "extra", "2.0")  # changed while the run was paused
    p.arl("exp", "run", eid)
    analysis = (p.ledger.exp_dir(eid) / "analysis.md").read_text()
    assert "The environment changed between the parts of this run (1 difference): extra 1.0 -> 2.0." in analysis
    parts = p.record(eid)["attempts"][-1]["segments"]
    assert parts[0]["env"]["listing"] != parts[1]["env"]["listing"]
