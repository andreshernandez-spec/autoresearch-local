---
name: research-loop
description: Run, resume or set up an automated research loop with arl (autoresearch-local), the local harness with a frozen evaluation, a git ledger of hypotheses and experiments, one branch and worktree per code track, and periodic reviews of whether the current path is still worth it. Use when the user asks to start, continue or check on automated research or an experiment loop in a project that has a research/ directory (stellarator-coils is the first), to set such a loop up for another local project, to do the periodic review, or to say from the ledger what was tried and what was found. Also use when the user mentions arl, autoresearch, the research loop, tracks, the ideas document or the frozen evaluator.
---

# Research loop with arl

`arl` is a small command-line harness at
`~/private/open-source/autoresearch-local/bin/arl` (no install, call it by
path). It owns the mechanics of an automated research loop; you own the
thinking. The framework is generic. Each project keeps its own `research/`
directory with the contract, the ledger and the evaluator.

**Before the first command of a session, read
`~/private/open-source/autoresearch-local/PROTOCOL.md`.** It is short and it is
the rulebook. This file only says how to get going.

## Running or resuming a loop

1. `cd` to the project's home checkout (the repository that holds `research/`).
2. Read `research/PROGRAM.md` (goal, scope, rules, when to ask),
   `research/STATUS.md`, the newest file in `research/reviews/`, and
   `research/IDEAS.md`. The ledger is the memory: do not rely on what you
   remember from earlier in the conversation.
3. Run `arl next`. It names the one thing to do and prints the steps with the
   exact commands and paths. Do it, then run `arl next` again.
4. Keep going until `next` says STOP (a person is needed, the budget is spent,
   the target is reached). Do not stop earlier to ask whether to continue, and
   do not go past a STOP. At a STOP, report in plain words and end.

Things that go wrong most often:

- **The evaluator and the contract are not yours.** If `arl` says the frozen
  files changed, put them back or stop and tell the user. Never try to freeze.
- **Scores come only from `arl exp run`.** No side runs of the evaluator.
- **Write the prediction before the run.** `design.md` is locked when the run
  starts, `params.json` too.
- **Code changes go in the track's worktree** (the path is in the brief), are
  committed with `arl exp commit <id> -m "..."`, and never touch `research/`
  there. No git writes by hand.
- **Long runs go in the background** (Bash with `run_in_background`), and you
  wait for the notification. Read logs with `arl exp log <id>`, never whole.
- **Exit code 75 means the machine is busy.** Wait and try again later. Do not
  stop other jobs and do not lower the limits in `arl.toml`.
- **New idea in the middle of something:** `arl idea add --title ... --body ...`
  and carry on. Reviews read that file.
- **The block between `+++` lines at the top of a hypothesis, track or review
  file is arl's.** Write below it; edit the file, do not replace it whole.
- **Tracks follow reviews.** After the first verdict, opening, parking or
  reopening a track takes `--review <id>` of the latest closed review.

## The review

When `arl next` says `review`: `arl review new` writes the facts into
`research/reviews/R00N.md`. Give the writing to a subagent that has not done
the work when you can: pass it the paths (`research/PROGRAM.md`, `STATUS.md`,
`IDEAS.md`, `tracks/`, the review file, the experiment folders in the window)
and ask it to fill sections 1 to 7 and propose a decision. Give it paths, not
your conclusions. Do what `PROGRAM.md` lists under "At every review" first.
Then `arl review close <id> --decision ...`. A decision the agent may not take
alone leaves the loop waiting for the user.

## Setting up a new project

1. In the project root (a git repository whose identity is right for the tree):
   `arl init`.
2. Write the contract with the user. Ask, do not guess:
   - the goal, and what "done" looks like;
   - what is measured, on which data, and which data is held back (a second
     split for confirmation, if there is enough);
   - the primary metric, the smallest change that counts (`min_delta`), any
     constraints;
   - what the agent may change and what it may not;
   - the budget, how often to review, which decisions the agent may take alone;
   - the limits of the machine (load, RAM, GPU memory) and the shared lock file;
   - when to stop and ask.
3. Put the evaluator under `research/eval/`. It reads what a run wrote and
   writes a JSON object of metrics. It must not import code the experiments can
   change, and it should check its own inputs (all instances present, no
   non-finite values) and fail loudly.
4. Run the evaluator once by hand on a trivial result.
5. `arl freeze --reason "initial"`, then `arl track new <name>`, write the
   track file, and start the loop. The first experiment of a track is always a
   baseline.

State back to the user, in one sentence, which resource a comparison holds
equal and which are free, before building a gate around it.

## Answering "what happened"

`arl status` for the overview, `arl exp list`, `arl exp show <id>` for one
experiment's conditions and result, the `analysis.md` files for the reasoning,
`research/reviews/` for the decisions. Report findings first, in plain words,
and define a name before using it: "E0012" means nothing to a reader.
