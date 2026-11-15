# autoresearch-local

The harness (`arl`) behind automated research loops in local projects.
`../CLAUDE.md` applies in full. `README.md` says what it is and how each idea is
enforced. `PROTOCOL.md` is what an agent reads before using it.

## Working on the harness

- Standard library only. A project runs `bin/arl` from whatever Python 3.11+ it
  has, with nothing installed. The same holds for `helpers/arl_job.py`, which
  projects copy.
- `python -m pytest` runs everything (about seven minutes). The tests drive a
  toy project in a temporary git repository through the real CLI.
- Every pull request and every commit on main runs `.github/workflows/ci.yml`:
  the suite on each Python from 3.11 to the newest with nothing installed but
  pytest, `pyright src`, and the wide walk. A new Python goes into its matrix
  when the runners have it. pyright is pinned there, and the pin moves in a
  change of its own, so that a new checker never fails a change that did not
  touch it.
- Every gate has a test that hits it through the CLI and checks the message:
  `tests/test_flow.py` for the loop, `tests/test_introspection.py` for the
  introspection step, `tests/test_env.py` for the environment and for going
  back to an old experiment (with a venv of its own), `tests/test_pause.py`
  for stopping and continuing (with real jobs in processes of their own). A
  new gate comes with one.
- `tests/test_hardening.py` holds the sequences that once broke a guarantee,
  dead-ended or ended in a traceback. A new one found by trying goes there
  with its fix.
- `tests/test_walk.py` walks the loop at random. CI walks it wide on every
  pull request (40 walks of 150 steps). After a change to the stages, the
  statuses or what a command checks, do the same before pushing:
  `ARL_WALK_SEEDS=40 ARL_WALK_STEPS=150 python -m pytest tests/test_walk.py`
  (about a quarter of an hour). A new stage or command goes into its `follow`
  or its list of typed commands.
- A command checks everything before it changes anything. A refusal leaves the
  ledger, the worktrees and git as they were.
- Where a change takes several steps, the intent is written first and any later
  command can finish it (`common.finish_judge`, `jobs.settle`). Nothing may be left that
  only the command that started it can complete.
- A command in the middle of a job does not write back the copy of a record
  it started with: another command may have written to the record since (a
  verdict given while a rerun is going). It changes the record as it is
  now, under the ledger lock (`jobs.amend`). The one exception is `exp run`,
  because every other command refuses a record that says `running`.
- A run and a rerun take one path from the start of the job to its metrics,
  `jobs.measure`: the job within its limits, what must still be as recorded
  (the code in the worktree, and in `jobs.moved` the contract and the data
  files), the evaluator, the same questions again, the metrics. A new check
  on a job goes there, never into one of the two commands. What a step took
  is booked the moment it ends, whatever ends it (`run_ended`,
  `evaluation_ended`).
- A record is a typed object, not a dictionary (`record.py`): an experiment,
  a track, a hypothesis, an introspection, and what an experiment holds
  inside. A new field is declared there first, with a comment that says what
  it is. The names of the fields are the keys in the files, and a field that
  is None is not written: a field that is renamed or removed still has to be
  read from old files. What a status promises (`contradiction`) is checked
  when a record is read, which is what lets a command say `need(...)`.
  `pyright src` stays at zero errors, read as Python 3.11 reads the code
  (`pyproject.toml`). CI runs it, and so does a test where pyright is
  installed.
- An interruption is not a failure. A part of a run that ends because arl or
  the job was stopped from outside leaves the run `paused`; only what the job
  itself did wrong uses up an attempt.
- The briefs in `src/arl/templates/briefs/` and the `ArlError` texts are the
  interface to the agent. Write them as instructions to a colleague: what
  happened, what to do next, the exact command.
- `gitops.py` holds the only place git is called, behind an allow-list. arl
  never pushes, resets, rebases, amends or deletes a branch. Do not widen the
  list without the reason written next to it.
- Project-specific things (paths, metric names, machine limits, data rules,
  the environment) belong in the project's `research/arl.toml` and
  `PROGRAM.md`, never here.
- Every job arl starts gets its environment from `runner.job_env`. A new
  place that starts a process on a project's behalf uses it too.
- Every record names the arl that ran it (`__version__` and the commit). Raise
  `__version__` in `src/arl/__init__.py` and `pyproject.toml` when what a
  command checks or records changes.
- The repository is public (`andreshernandez-spec/autoresearch-local`), and
  the projects that use it are not. Nothing a research loop writes may end up
  in a tracked file here: no experiment, no result, no project name beyond an
  example, nothing about other people's systems. That holds for commit
  messages too. `main` on GitHub moves only through pull requests.
- `skill/LESSONS.md` is the one file a loop writes inside this checkout
  (`arl lesson add --scope general`, from any project on the machine). It is
  ignored by git and never committed; a test checks that. What has proved
  itself is written into `PROTOCOL.md` by hand, in words that fit any
  project.
- `../.claude/skills/research-loop/` holds symlinks to `skill/SKILL.md` and
  `skill/LESSONS.md`.

## Where things are

| File | Holds |
|---|---|
| `src/arl/cli.py` | the parser, and what happens to what was typed |
| `src/arl/commands/common.py` | what every command leans on: who is asking, the gates, the worktrees, the ledger lock that finishes a verdict left half applied |
| `src/arl/commands/jobs.py` | a job arl started: the one path a run and a rerun take (`measure`), what may not change under it (`moved`), writing to a record in the middle of one (`amend`), keeping its outcome (`keep_outcome`), what a dead or interrupted process left (`settle`) |
| `src/arl/commands/runs.py` | `exp run` and `exp reproduce`: the checks before a job starts, and where each writes what came of it |
| `src/arl/commands/experiments.py` | an experiment from its draft to its verdict (`exp judge` and the order of its checks), and looking one up |
| `src/arl/commands/` the rest | `project.py` (init, freeze, status, next, verify), `session.py` (pause, resume, reload, notes, lessons), `tracks.py`, `hypotheses.py` (and ideas), `introspections.py` |
| `src/arl/rules.py` | outcome of a result, allowed verdicts, when an introspection is due, the budget, what comes next |
| `src/arl/contract.py` | `frozen.lock`: the two digests and who may freeze |
| `src/arl/record.py` | what an experiment's record, a track, a hypothesis and an introspection hold, as types; reading and writing them; what a status promises |
| `src/arl/ledger.py` | where each record lives, ids, the ideas, lessons and activity files, the pause file, ledger commits |
| `src/arl/runner.py` | running a job in its own session with limits, stopping it, machine gates, the environment a job gets, conditions written into a record |
| `src/arl/config.py` | `arl.toml`, every value checked when it is read |
| `src/arl/render.py` | `STATUS.md`, the facts in introspections and analyses, what a reload prints, the briefs |
| `src/arl/templates/` | what `arl init` and the record commands write |
| `helpers/arl_job.py` | what a job copies to save its state and stop when asked |
