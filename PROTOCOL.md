# Protocol

How an agent works in a project that uses `arl`. Read it before the first
command of a session. The project's own rules are in `research/PROGRAM.md`;
where they are stricter, they win.

## Who owns what

- **The person** owns the contract: `PROGRAM.md` (goal, scope, rules),
  `arl.toml` (metrics, thresholds, budget, how often to introspect, the
  environment runs use) and the evaluator. They also own direction: pausing and resuming the loop, stopping
  the program, and anything `PROGRAM.md` lists under "Ask first".
- **arl** owns the mechanics: checking the contract, locking designs, running,
  pausing and continuing runs, scoring, comparing, committing, moving branches,
  deciding when an introspection is due.
- **You** own the thinking: hypotheses, designs, analyses, ideas,
  introspections, and what goes into the knowledge and lessons files.

## The loop

```
arl reload      # once, at the start of a session: what is known and how to work here
arl next        # says the one thing to do, with the steps
(do it)
arl next
```

Keep going until `next` says STOP. Do not stop earlier to ask whether to
continue: the person may be away and expects the loop to run. Do not go past a
STOP either. At a STOP, write a short report (below) and end.

`arl reload` prints three files in full, and you read them in full:

| File | Holds | Changed by |
|---|---|---|
| `research/KNOWLEDGE.md` | what the work has shown about the problem, distilled | part 3 of every introspection |
| `research/LESSONS.md` | how to work in this project | `arl lesson add --scope project` |
| `skill/LESSONS.md` beside the arl skill (this machine only, not in git) | how to work with arl in any project | `arl lesson add --scope general` |

Then read `research/PROGRAM.md`, `research/STATUS.md`, the files of the active
tracks and `research/IDEAS.md`. The ledger is the memory. What you remember
from earlier in a conversation may be out of date or compacted away.

When `arl next` says `reload` in the middle of the work, one of the three files
is no longer what you read: an introspection changed it, the person corrected
it, or a loop in another project added a general lesson. Run `arl reload` and
read it again before going on.

Hypotheses, tracks and introspections are files with a block between `+++`
lines at the top. That block is arl's. Write below it, and edit such a file in
place rather than replacing it whole.

## Stopping and continuing

The work can be interrupted at any point and taken up again later. Nothing is
lost that was written down.

**When the person says to stop or pause**, whatever you are in the middle of:

```
arl pause --reason "<why>" --note "<where you were and what you were about to do>"
```

A run that is going is asked to save its state and stop. Nothing starts, runs
or is judged while the loop is paused. Then end your turn. The note is what the
next session starts from: write it for someone who was not there. If the pause
had to be quick, the note can follow: `arl pause --note "..."` adds to it.

The person can also pause the loop themselves, from a terminal, while you work.
You find out at your next command: `arl next` says STOP. Write your note and end
the turn.

**When the person says to continue:** `arl resume`. It prints the note, the
knowledge and the lessons. Read them, then `arl next`. Only the person lifts a
pause, and one they set at a terminal they lift there.

**A run is made of parts.** A part ends early when the run is paused
(`arl exp pause`), the terminal closes, arl is killed, the machine restarts or
runs low on memory. None of that is a failure. `arl next` says "resume" or
"interrupted", and `arl exp run <id>` continues the run in the same folder from
the state the job last saved. If the saved state is no good, or the code needs
a fix first, `arl exp fail <id> --reason "..."` gives the run up: it counts as a
failed attempt and the next one starts from nothing.

Stop a run only with `arl exp pause`. Never kill its processes by hand: arl
cannot tell a job you killed from one that broke.

**If a session just ends** (no pause, no note), the ledger still holds
everything that was committed, and `arl next` finds the place. What is lost is
what you had only in your head. So write things down when they happen:
`arl note`, `arl idea add`, the analysis before the verdict.

## Rules

1. **The contract is not yours to change.** Not the evaluator, not a metric,
   not a threshold, not the budget, not `PROGRAM.md`. If one looks wrong, say
   so in an introspection and escalate. Never work around it.
2. **A number counts only if `arl exp run` produced it.** The frozen evaluator
   is run by arl and by nothing else. Debug runs on small inputs are fine;
   scoring on the evaluation data outside arl is tuning in the dark, and its
   numbers never appear in an analysis or an introspection.
3. **The prediction comes before the result.** Write `design.md` before the
   run, with what you expect and what would count against the hypothesis.
   After the run starts the design is locked.
4. **One change per experiment, of the kind the experiment declares.** A tune
   changes params. A feature changes code. A diagnose observes and is never
   merged. If two things change, the result says nothing about either.
5. **The verdict follows the recorded outcome.** You choose among the verdicts
   it allows, and the reason goes in the analysis. An improvement may be
   discarded when it buys little and adds much. A neutral result may be kept
   only when it makes things simpler.
6. **A hypothesis is judged separately from the change.** "Kept" does not mean
   "supported". Say what each run did to each hypothesis it tested.
7. **What you notice is written down when you notice it**, in a sentence or
   two, and taken up at the next introspection:
   - something that could be tried: `arl idea add` (goes to `IDEAS.md`);
   - something the problem showed: `arl note --about problem "..."`;
   - a slip or a waste in the way of working, yours or arl's:
     `arl note --about work "..."`.
   Do not chase it now and do not lose it.
8. **When an introspection is due, it comes first.** All four parts, from the
   files, with fresh eyes.
9. **Git writes go through arl** (`arl exp commit`, `arl exp judge`). No commit,
   switch, reset, merge or push by hand in the project or its worktrees. Reading
   with git is fine. The one exception is the one arl asks for: putting a file
   back with `git restore <file>` when it says a file must go back.
10. **Output goes to files.** Runs log to disk; read the end with
    `arl exp log`. Do not pull whole logs or data files into the conversation.
11. **A busy machine is not yours to free.** If a run does not start because
    the machine is busy, wait and try again. Never stop someone else's job.
12. **A long run saves its state.** A job that runs longer than a few minutes
    saves what it needs to continue (below), so that a pause costs minutes and
    not the run.
13. **The environment is the contract's.** Runs and the evaluator use the
    environment `arl.toml` names, and arl starts them there. Do not install,
    upgrade or remove a package in it, do not create another one, and do not
    route a run around it (another interpreter in the command, a venv of your
    own). A package that is missing or broken is not a result and not yours to
    repair: stop and tell the person. For what you run yourself outside arl (a
    quick check, a test), use the interpreter `arl reload` names.
14. **A run is decided by what is on record.** Whatever decides the result is
    in `params.json` or in the committed code: the random seed, the number of
    steps, every setting. Nothing comes from the clock, an environment
    variable of your shell or a file that is not tracked. Only then can
    `arl exp reproduce` say whether a result holds.
15. **A target reached once is a claim.** `arl next` says `confirm`, and the
    experiment is run one more time where the loop never looked. One run
    decides: do not repeat it to get another answer, and do not send
    experiments to a confirming split to see how they would do there. A
    split that is looked at until it agrees has been tuned on.

## Temptations

| If you catch yourself thinking | Do this instead |
|---|---|
| "The evaluator misses what my change improves." | It may. Write it in IDEAS.md (kind: evaluator) and raise it at the next introspection. The score stands. |
| "The gain is just under the threshold but it looks real." | Neutral is neutral. Repeat it with `--allow-duplicate` to see the noise, or discard. |
| "A quick score outside arl first, to see if it is worth designing." | That is the experiment. Draft it. |
| "This is only a small tune, the design can be one word." | The prediction is what makes the result worth anything. Write it. |
| "The result is odd, I will adjust the design text to match what I meant." | The design is locked. Say in the analysis what you meant and what happened. |
| "An introspection is due, but one more experiment first." | The gate is closed for a reason. Introspect. |
| "The introspection is a formality, the plan is fine." | Then section 3 will be easy to write. Write the strongest case against the plan anyway. |
| "This new idea is better than what I am doing, I will switch now." | IDEAS.md, then the introspection decides. If it cannot wait, open one now with `--trigger`. |
| "The run crashed for an unrelated reason, I will tweak params to get past it." | Params are locked. Fix the code, or judge it as a crash and draft again. |
| "The budget is nearly spent, I will make the last runs bigger." | The budget is the person's. Say what more would buy, and stop. |
| "That command was refused, I will find another way to get the same thing." | The refusal is the rule speaking. Do what its message says, and note it (`arl note --about work`). |
| "Everyone knows that lesson, no need to record it." | The next session knows nothing. `arl lesson add`. |
| "I will add a line to KNOWLEDGE.md for this result." | It is a distillation, not a log. Revise the statement the result changes, or move it between sections. |
| "The confirming run missed the target by a hair. One more and it may pass." | One run decides, and a later, better one does not count. The result did not hold where the loop never looked: that is what the introspection is for. |
| "A package is missing. One install and the run goes through." | The environment is the person's, and an install changes every later comparison. Stop and say what is missing. |
| "It works in another environment, I will run it there." | Then the result is not this program's. Runs use the environment the contract names. Stop and say what is wrong with it. |
| "I was told to stop, but this run is nearly done." | `arl pause` now. The run saves its state and continues later. |
| "This needs a person at a terminal. I could run it so that it looks like one." | That forges the person's decision. Stop, say what is waiting for them and why, and end the turn. |

## The steps

**Hypothesis** (`arl hyp new`). A claim about the problem, with why you believe
it, what should be seen if it is true, and what would refute it. No code, no
plan. Look at the open hypotheses, at `KNOWLEDGE.md` and at `IDEAS.md` before
writing a new one.

**Design** (`arl exp new`). Pick the kind:

| Kind | Use it when | Changes | Scored | Merged |
|---|---|---|---|---|
| `baseline` | a track has no reference, the evaluator or the data changed, the environment changed and you need to know whether that moved the score, or you want to see the noise | nothing | yes | the first one becomes the reference, a repeat never replaces it |
| `tune` | the claim is about a setting | `params.json` | yes | params, on keep |
| `feature` | the claim needs new code | code in the worktree | yes | code and params, on keep |
| `diagnose` | you do not know why something happens | instrumentation, or its own command | no | never |

Reach for `diagnose` when two results contradict each other, when a change did
the opposite of what you predicted, or before a feature whose mechanism you
have not seen. An hour of looking often saves five experiments.

**Run** (`arl exp run`). Start long runs in the background and wait for them to
end. While one runs you may write hypotheses, ideas and notes. Exit codes:
0 a result, 2 the run failed, 3 the run was paused, 75 the machine was busy.

A job that arl runs is told three things, and a long job acts on them:

| It is told | Meaning | What the job does |
|---|---|---|
| `ARL_CHECKPOINT_EVERY` | seconds between two saves (`[run] checkpoint_every_s`) | saves its state into `{out}/checkpoint/` about that often, not after every step |
| `ARL_RESUME=1` | an earlier part of this run was paused or cut off | reads the saved state and goes on from there; with `0` it starts from nothing |
| the signal SIGTERM | arl asks it to stop | saves and ends within `[run] pause_grace_s`, after which it is killed |

A run that has something to report about itself (training loss, steps, time)
writes it to `{out}/train_metrics.json`, one JSON object of numbers. It goes
into the record and is never scored.

`helpers/arl_job.py` in the arl repository does all three in a few lines; copy
it next to the run script. arl points out a part that ran long without saving
anything. A job that ignores all this still works: a pause then costs it
everything it had done.

**Judge** (`arl exp judge`). Work from `design.md` and `arl exp show`, not from
what you hoped. Write what happened against the prediction, the most likely
reason, and what you did not check. If the facts say the environment is not
the one the reference ran in, a changed package is a candidate reason for the
difference, before the change you made: `arl exp env <reference> <id>` lists
what differs, and a baseline in the environment as it is now settles it. Before believing a good number, check that
the work was done: the run time is plausible, the instances were all scored,
nothing was skipped. Results that flatter the method deserve the hardest look.

**Confirm** (`arl exp reproduce <id> --split <name>`). Only when the contract
sets a target. A kept result that reaches it on the first split is a claim,
and `arl next` says `confirm`. The run it names takes the recorded code and
params once more, on a split no experiment was scored on (on the first split
again when the contract has no other). Met there as well, the program ends
at `target`, a STOP. Missed, the target counts as not reached: the result
stays the reference of its track, and an introspection is due. Its part 1
asks why one split said yes and the other no: a gain smaller than the noise,
or a method fitted to the split it was tuned on. A result reached under
another evaluator or on other data files makes no claim: after either
changes, the tracks are measured again.

**Introspection** (`arl introspect new`, `arl introspect close`, `arl reload`).
Due after a set number of judged experiments, after a run of experiments
without a keep on one track, after a set number of days, or when you open one
yourself with `--trigger`. It has four parts, in this order:

1. **The path.** Is the current approach still worth it, does it need a bigger
   change, or is a completely different approach due? Argue against the current
   path as well as you can, and go through every open idea.
2. **How the work went.** Not about the problem: about the way of working. The
   introspection file lists the commands arl refused, the runs that failed or
   were cut short, and your notes. Whatever deserves a standing instruction is
   recorded with `arl lesson add`: `--scope general` when it would hold in any
   project (a wrong git action, a habit that wasted a run), `--scope project`
   when it is about this project's tools and data.
3. **What we know.** About the problem itself. Bring `research/KNOWLEDGE.md`
   up to date with what the window established, made likely or ruled out. It is
   a distillation of everything the work has revealed so far: revise and merge,
   move statements between its sections, remove what turned out wrong.
4. **Reload.** After closing, `arl reload`, and read what it prints. Every
   agent that goes on working does the same. Nothing new starts before.

If you can start a subagent, give the writing to one that has not done the
work. Pass it paths, not conclusions. A part that truly has nothing new is
closed with its reason (`--no-lessons "why"`, `--knowledge-unchanged "why"`).

The decisions of part 1:

| Decision | Meaning |
|---|---|
| `continue` | same tracks, same emphasis. Needs a reason like any other |
| `adjust` | same approach, different emphasis or next steps |
| `branch` | open a new track to test another approach beside the current one |
| `pivot` | another approach becomes the main line; park the current track |
| `stop` | end the program: the goal is met or out of reach |
| `escalate` | a person has to decide something first |

`arl.toml` says which of these you may take alone. A decision outside that
list waits for a person, and nothing runs while it waits. After a `stop`, any
later introspection waits for a person too. Tripwires written in one
introspection are checked first in the next.

Tracks follow introspections. Once the first experiment is judged, opening a
track, parking one or reopening one needs `--introspection <id>` of the latest
closed one. Parking a track that stalled does not make its introspection go
away.

**Going back.** Nothing that ran is lost. `arl exp show <id>` says what an old
experiment was and what it ran with, `arl exp env <id>` what has changed in
the environment since, `arl exp reproduce <id>` runs it again from its
recorded commit and params (in the environment and on the data files as they
are now: it says what differs, and on other data it compares nothing), and
`arl track new <name> --from <id>`
opens a track at the code and params of that experiment, kept or not. When an
earlier result looks better in hindsight than the path taken since, that is a
`branch` for the next introspection, not a reason to rework the current track
by hand.

## Reporting to the person

At a STOP, or when asked how it is going:

- Lead with what was found, in plain words. Then the numbers, from the ledger.
- Define every name before using it. "E0012" means nothing to a reader: say
  what was tried.
- Say what the claim rests on: which data, how many runs, what was not checked.
- Failures and dead ends are results. Report them with the same care.
- If you need a decision, give the options, the one you would take, and why.

## When something is wrong

Stop and say so, with what you saw, when: a metric is NaN or far outside its
usual range; the same failure repeats across experiments; the contract check
fails; `arl next` says `environment` (the one the contract names is gone, or
its python is another one); a run fails on an import; a run fails because the
data files changed while it was going (somebody is writing them: find out
who before running again); a result is too good
to be explained by the change; you would need to touch something `PROGRAM.md`
puts out of scope. An escalation is an
introspection:

```
arl introspect new --trigger "escalation: <what>"
(write it: what happened, the options, what you recommend)
arl introspect close <id> --decision escalate
```
