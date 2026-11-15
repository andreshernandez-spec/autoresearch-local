"""arl guard: a command of the agent's own, under the limits a run has.

It runs in the contract's environment, under the memory watch, with the run
lock and the machine gates, and records nothing. The commands run in a process
of their own here, where their output and exit code can be read as an agent
reads them.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

from arl import runner
from conftest import LAUNCHER, Project, env_table, git, launch, make_env, wait_for


def guard(p: Project, *argv: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LAUNCHER), "guard", *argv], cwd=cwd or p.root, capture_output=True, text=True, timeout=120)


def test_output_and_exit_code_pass_through(project: Project):
    p = project
    env = make_env(p.base / "env")
    p.started(python="python", extra=env_table(env))
    got = guard(p, "--", "python", "-c", "import sys; print(sys.prefix); print('to stderr', file=sys.stderr); sys.exit(5)")
    assert got.returncode == 5
    assert os.path.realpath(got.stdout.strip()) == os.path.realpath(env)  # the contract's environment, as a run gets it
    assert "to stderr" in got.stderr and "arl guard: exit 5" in got.stderr and "peak memory" in got.stderr
    assert "holding the run lock" in got.stderr
    one = guard(p, "--", "echo one && echo two | tr a-z A-Z")  # one argument is a command line
    assert one.returncode == 0 and one.stdout.split() == ["one", "TWO"]
    wt = p.base / "wt" / "toy-main-line"
    assert guard(p, "--", "pwd", cwd=wt).stdout.strip() == str(wt)  # where it was called, also in a worktree
    assert "say what to run" in guard(p).stderr


def test_a_command_past_the_memory_limit_is_stopped(project: Project):
    p = project
    p.started(extra="[machine]\nmax_run_rss_gb = 0.2\n")
    start = time.time()
    got = guard(p, "--", sys.executable, "-c", "import time; hog = b'x' * (400 << 20); time.sleep(60)")
    assert time.time() - start < 30 and got.returncode in (137, 143)
    assert "arl guard: STOPPED, its processes held" in got.stderr and "GB of RAM, the limit is 0.2" in got.stderr
    assert "arl guard: exit" in got.stderr


def test_the_run_lock_is_shared_with_runs(project: Project):
    p = project
    h = p.started()
    path = p.research / ".run.lock"
    with open(path, "a+") as held:  # another holder: a run of another project, say
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "another run holds" in p.arl("guard", "--", "true", code=75)  # busy, as a run would be
        p.arl("guard", "--no-lock", "--", "true")  # beside it, on purpose
    proc = launch(p, "guard", "--", "sleep", "20")
    assert wait_for(lambda: "arl guard" in path.read_text())
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    out = p.arl("exp", "run", eid, code=75)  # and a run waits for a check, as it waits for another run
    assert "another run holds" in out and "arl guard" in out
    proc.terminate()  # stopped from outside: it takes the command down with it
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 130 and "interrupted" in out
    p.arl("exp", "run", eid)


def test_the_machine_gates_come_first(project: Project, monkeypatch):
    p = project
    p.started()
    monkeypatch.setattr(runner, "busy", lambda cfg: ["load average 30.0 is above 10"])
    out = p.arl("guard", "--", "true", code=75)
    assert "The machine is busy" in out and "load average 30.0" in out


def test_the_state_of_the_loop_stops_nothing_and_nothing_is_recorded(project: Project):
    p = project
    p.started()
    p.arl("pause", "--reason", "the person is away")
    p.arl("introspect", "new", "--trigger", "asked", code=1)  # refused while paused: the loop is stopped
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# edited\n")  # the contract no longer matches its lock
    def ledger() -> tuple[str, str, str]:
        return git(p.root, "rev-parse", "HEAD"), git(p.root, "status", "--porcelain"), (p.research / "activity.jsonl").read_text()

    before = ledger()
    got = guard(p, "--", "echo", "checked")
    assert got.returncode == 0 and got.stdout == "checked\n"
    assert ledger() == before
