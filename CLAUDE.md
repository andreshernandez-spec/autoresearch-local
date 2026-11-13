# autoresearch-local

The harness (`arl`) behind automated research loops in local projects.
`../CLAUDE.md` applies in full. `README.md` says what it is and how each idea is
enforced. `PROTOCOL.md` is what an agent reads before using it.

## Working on the harness

- Standard library only. A project runs `bin/arl` from whatever Python 3.11+ it
  has, with nothing installed. The same holds for `helpers/arl_job.py`, which
  projects copy.
- `python -m pytest` runs everything (about four minutes). The tests drive a
  toy project in a temporary git repository through the real CLI.
- Every gate has a test that hits it through the CLI and checks the message:
  `tests/test_flow.py` for the loop, `tests/test_introspection.py` for the
  introspection step, `tests/test_pause.py` for stopping and continuing (with
  real jobs in processes of their own). A new gate comes with one.
- `tests/test_hardening.py` holds the sequences that once broke a guarantee,
  dead-ended or ended in a traceback. A new one found by trying goes there
  with its fix.
- `tests/test_walk.py` walks the loop at random. After a change to the stages,
  the statuses or what a command checks, run it wide before committing:
  `ARL_WALK_SEEDS=40 ARL_WALK_STEPS=150 python -m pytest tests/test_walk.py`.
  A new stage or command goes into its `follow` or its list of typed commands.
- A command checks everything before it changes anything. A refusal leaves the
  ledger, the worktrees and git as they were.
- Where a change takes several steps, the intent is written first and any later
  command can finish it (`_finish_judge`, `_settle`). Nothing may be left that
  only the command that started it can complete.
- An interruption is not a failure. A part of a run that ends because arl or
  the job was stopped from outside leaves the run `paused`; only what the job
  itself did wrong uses up an attempt.
- The briefs in `src/arl/templates/briefs/` and the `ArlError` texts are the
  interface to the agent. Write them as instructions to a colleague: what
  happened, what to do next, the exact command.
- `gitops.py` holds the only place git is called, behind an allow-list. arl
  never pushes, resets, rebases, amends or deletes a branch. Do not widen the
  list without the reason written next to it.
- Project-specific things (paths, metric names, machine limits, data rules)
  belong in the project's `research/arl.toml` and `PROGRAM.md`, never here.
- `skill/LESSONS.md` is written by `arl lesson add --scope general`, from any
  project on the machine, and committed here by that command. Prune it by
  hand; what has proved itself moves into `PROTOCOL.md`.
- `../.claude/skills/research-loop/` holds symlinks to `skill/SKILL.md` and
  `skill/LESSONS.md`.

## Where things are

| File | Holds |
|---|---|
| `src/arl/cli.py` | every command; the order of checks in `exp run` and `exp judge`; what a dead or interrupted process left (`_settle`) |
| `src/arl/rules.py` | outcome of a result, allowed verdicts, when an introspection is due, the budget, what comes next |
| `src/arl/contract.py` | `frozen.lock`: the two digests and who may freeze |
| `src/arl/ledger.py` | where each record lives, ids, the ideas, lessons and activity files, the pause file, ledger commits |
| `src/arl/runner.py` | running a job in its own session with limits, stopping it, machine gates, conditions written into a record |
| `src/arl/config.py` | `arl.toml`, every value checked when it is read |
| `src/arl/render.py` | `STATUS.md`, the facts in introspections and analyses, what a reload prints, the briefs |
| `src/arl/templates/` | what `arl init` and the record commands write |
| `helpers/arl_job.py` | what a job copies to save its state and stop when asked |
