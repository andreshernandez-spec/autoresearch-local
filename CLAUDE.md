# autoresearch-local

The harness (`arl`) behind automated research loops in local projects.
`../CLAUDE.md` applies in full. `README.md` says what it is and how each idea is
enforced. `PROTOCOL.md` is what an agent reads before using it.

## Working on the harness

- Standard library only. A project runs `bin/arl` from whatever Python 3.11+ it
  has, with nothing installed.
- `python -m pytest` runs everything (under two minutes). The tests drive a toy
  project in a temporary git repository through the real CLI.
- Every gate has a test in `tests/test_flow.py` that hits it through the CLI
  and checks the message. A new gate comes with one.
- `tests/test_hardening.py` holds the sequences that once broke a guarantee,
  dead-ended or ended in a traceback. A new one found by trying goes there
  with its fix.
- A command checks everything before it changes anything. A refusal leaves the
  ledger, the worktrees and git as they were.
- The briefs in `src/arl/templates/briefs/` and the `ArlError` texts are the
  interface to the agent. Write them as instructions to a colleague: what
  happened, what to do next, the exact command.
- `gitops.py` holds the only place git is called, behind an allow-list. arl
  never pushes, resets, rebases, amends or deletes a branch. Do not widen the
  list without the reason written next to it.
- Project-specific things (paths, metric names, machine limits, data rules)
  belong in the project's `research/arl.toml` and `PROGRAM.md`, never here.
- `../.claude/skills/research-loop/SKILL.md` is a symlink to `skill/SKILL.md`.

## Where things are

| File | Holds |
|---|---|
| `src/arl/cli.py` | every command; the order of checks in `exp run` and `exp judge` |
| `src/arl/rules.py` | outcome of a result, allowed verdicts, when a review is due, what comes next |
| `src/arl/contract.py` | `frozen.lock`: the two digests and who may freeze |
| `src/arl/ledger.py` | where each record lives, ids, the ideas file, ledger commits |
| `src/arl/runner.py` | running with a time limit, machine gates, conditions written into a record |
| `src/arl/render.py` | `STATUS.md`, the facts in reviews and analyses, the briefs |
| `src/arl/templates/` | what `arl init` and the record commands write |
