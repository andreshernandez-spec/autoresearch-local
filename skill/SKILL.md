---
name: research-loop
description: Run, resume, pause or set up an automated research loop with arl (autoresearch-local), the local harness with a frozen evaluation, a git ledger of hypotheses and experiments, one branch and worktree per code track, runs that can be paused and continued, and a periodic introspection step (is the path still worth it, what was learned about the way of working, what is now known about the problem). Use when the user asks to start, continue, pause, stop or check on automated research or an experiment loop in a project that has a research/ directory (stellarator-coils is the first), to set such a loop up for another local project, to do the introspection, or to say from the ledger what was tried and what was found. Also use when the user mentions arl, autoresearch, the research loop, tracks, the ideas document, the knowledge or lessons files, or the frozen evaluator.
---

# Research loop with arl

`arl` is a small command-line harness at
`~/private/open-source/autoresearch-local/bin/arl` (no install, call it by
path). It owns the mechanics of an automated research loop; you own the
thinking. The framework is generic. Each project keeps its own `research/`
directory with the contract, the ledger and the evaluator.

**Before the first command of a session, read
`~/private/open-source/autoresearch-local/PROTOCOL.md`.** It is short and it is
the rulebook. This file only says how to get going. `LESSONS.md` next to this
file holds what earlier loops learned about the way of working; `arl reload`
prints it.

## Running or resuming a loop

1. `cd` to the project's home checkout (the repository that holds `research/`).
2. If the user says to continue a loop that was paused: `arl resume`. Otherwise
   `arl reload`. Either prints, in full, what is known about the problem
   (`research/KNOWLEDGE.md`) and the lessons about how to work (the project's
   and the general ones), and `resume` also prints the note the last session
   left. Read all of it. Then read `research/PROGRAM.md` (goal, scope, rules,
   when to ask), `research/STATUS.md` and `research/IDEAS.md`. The ledger is
   the memory: do not rely on what you remember from earlier in the
   conversation.
3. Run `arl next`. It names the one thing to do and prints the steps with the
   exact commands and paths. Do it, then run `arl next` again.
4. Keep going until `next` says STOP (a person is needed, the loop is paused,
   the budget is spent, the target is reached). Do not stop earlier to ask
   whether to continue, and do not go past a STOP. At a STOP, report in plain
   words and end.

## When the user says stop, pause, or "that is enough for now"

Do this first, whatever you are in the middle of:

```
arl pause --reason "<why>" --note "<where you were, what you were about to do, what you would check first>"
```

A run that is going saves its state and stops by itself; do not kill anything.
Then report and end the turn. Do not finish "just this one step" first. When
the user later says to go on, start at step 2 above with `arl resume`.

## Things that go wrong most often

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
- **Exit codes of `arl exp run`:** 0 a result, 2 the run failed, 3 the run was
  paused (continue it later with the same command), 75 the machine is busy
  (wait and try again; do not stop other jobs, do not lower the limits).
- **An interrupted run is not a failed run.** If `next` says `interrupted` or
  `resume`, `arl exp run <id>` continues it from its saved state.
- **A job that runs for more than a few minutes saves its state.** Copy
  `~/private/open-source/autoresearch-local/helpers/arl_job.py` next to the run
  script and use it (the file says how). Without it a pause loses the run.
- **Write it down when you notice it.** A new idea: `arl idea add`. Something
  the problem showed: `arl note --about problem "..."`. A slip or a waste in
  the way of working: `arl note --about work "..."`. The introspection reads
  all three.
- **The block between `+++` lines at the top of a hypothesis, track or
  introspection file is arl's.** Write below it; edit the file, do not replace
  it whole.
- **Tracks follow introspections.** After the first verdict, opening, parking
  or reopening a track takes `--introspection <id>` of the latest closed one.

## The introspection

When `arl next` says `introspect`: `arl introspect new` writes the facts into
`research/introspections/N00x.md`, including what arl refused and what was
noted since the last one. Four parts, in order:

1. **The path.** Continue, change, or change radically. Do what `PROGRAM.md`
   lists under "At every introspection" first. Argue against the current path.
2. **How the work went.** Behaviours and errors worth a standing instruction
   become lessons: `arl lesson add --scope general|project --title "..."
   --body "what happened, what to do instead" --from <id>`. General ones land
   in `LESSONS.md` next to this file and reach every project.
3. **What we know.** Revise `research/KNOWLEDGE.md`: what the work has revealed
   about the problem, distilled. Merge and rewrite, do not append.
4. **Reload.** After `arl introspect close <id> --decision ...`, run
   `arl reload` and read what it prints. Any other agent working on the loop
   does the same.

Give the writing to a subagent that has not done the work when you can: pass
it the paths (`research/PROGRAM.md`, `STATUS.md`, `KNOWLEDGE.md`, `IDEAS.md`,
`tracks/`, the introspection file, the experiment folders in the window), not
your conclusions, and have it write its findings into the file as it goes. A
decision the agent may not take alone leaves the loop waiting for the user.

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
   - the budget, how often to introspect, which decisions the agent may take
     alone;
   - how long a run may take, how often a job saves its state
     (`checkpoint_every_s`) and how long it gets to stop (`pause_grace_s`);
   - the limits of the machine (load, RAM, GPU memory) and the shared lock file;
   - when to stop and ask.
3. Put the evaluator under `research/eval/`. It reads what a run wrote and
   writes a JSON object of metrics. It must not import code the experiments can
   change, and it should check its own inputs (all instances present, no
   non-finite values) and fail loudly.
4. Write what is already known about the problem into `research/KNOWLEDGE.md`,
   each statement with what it rests on.
5. Run the evaluator once by hand on a trivial result.
6. `arl freeze --reason "initial"`, then `arl track new <name>`, write the
   track file, and start the loop. The first experiment of a track is always a
   baseline.

State back to the user, in one sentence, which resource a comparison holds
equal and which are free, before building a gate around it.

## Answering "what happened"

`arl status` for the overview, `arl exp list`, `arl exp show <id>` for one
experiment's conditions and result, the `analysis.md` files for the reasoning,
`research/introspections/` for the decisions, `research/KNOWLEDGE.md` for what
it all adds up to. Report findings first, in plain words, and define a name
before using it: "E0012" means nothing to a reader.
