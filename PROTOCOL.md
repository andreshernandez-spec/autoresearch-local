# Protocol

How an agent works in a project that uses `arl`. Read it before the first
command of a session. The project's own rules are in `research/PROGRAM.md`;
where they are stricter, they win.

## Who owns what

- **The person** owns the contract: `PROGRAM.md` (goal, scope, rules),
  `arl.toml` (metrics, thresholds, budget, review cadence) and the evaluator.
  They also own direction: stopping the program, and anything `PROGRAM.md`
  lists under "Ask first".
- **arl** owns the mechanics: checking the contract, locking designs, running,
  scoring, comparing, committing, moving branches, deciding when a review is due.
- **You** own the thinking: hypotheses, designs, analyses, ideas, reviews.

## The loop

```
arl next        # says the one thing to do, with the steps
(do it)
arl next
```

Keep going until `next` says STOP. Do not stop earlier to ask whether to
continue: the person may be away and expects the loop to run. Do not go past a
STOP either. At a STOP, write a short report (below) and end.

At the start of a session, before `arl next`: read `research/PROGRAM.md`,
`research/STATUS.md`, the latest file in `research/reviews/`, and
`research/IDEAS.md`. The ledger is the memory. What you remember from earlier in
a conversation may be out of date or compacted away.

Hypotheses, tracks and reviews are files with a block between `+++` lines at
the top. That block is arl's. Write below it, and edit such a file in place
rather than replacing it whole.

## Rules

1. **The contract is not yours to change.** Not the evaluator, not a metric,
   not a threshold, not the budget, not `PROGRAM.md`. If one looks wrong, say
   so in a review and escalate. Never work around it.
2. **A number counts only if `arl exp run` produced it.** The frozen evaluator
   is run by arl and by nothing else. Debug runs on small inputs are fine;
   scoring on the evaluation data outside arl is tuning in the dark, and its
   numbers never appear in an analysis or a review.
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
7. **An idea outside the current experiment goes to IDEAS.md**, in the moment,
   in a few sentences. Do not chase it now and do not lose it.
8. **When a review is due, it comes first.** Do it from the files with fresh
   eyes, and argue against the current path as well as you can.
9. **Git writes go through arl** (`arl exp commit`, `arl exp judge`). No commit,
   switch, reset, merge or push by hand in the project or its worktrees. Reading
   with git is fine. The one exception is the one arl asks for: putting a file
   back with `git restore <file>` when it says a file must go back.
10. **Output goes to files.** Runs log to disk; read the end with
    `arl exp log`. Do not pull whole logs or data files into the conversation.
11. **A busy machine is not yours to free.** If a run does not start because
    the machine is busy, wait and try again. Never stop someone else's job.

## Temptations

| If you catch yourself thinking | Do this instead |
|---|---|
| "The evaluator misses what my change improves." | It may. Write it in IDEAS.md (kind: evaluator) and raise it at the next review. The score stands. |
| "The gain is just under the threshold but it looks real." | Neutral is neutral. Repeat it with `--allow-duplicate` to see the noise, or discard. |
| "A quick score outside arl first, to see if it is worth designing." | That is the experiment. Draft it. |
| "This is only a small tune, the design can be one word." | The prediction is what makes the result worth anything. Write it. |
| "The result is odd, I will adjust the design text to match what I meant." | The design is locked. Say in the analysis what you meant and what happened. |
| "A review is due, but one more experiment first." | The gate is closed for a reason. Review. |
| "The review is a formality, the plan is fine." | Then section 3 will be easy to write. Write the strongest case against the plan anyway. |
| "This new idea is better than what I am doing, I will switch now." | IDEAS.md, then the review decides. If it cannot wait, open a review now with `--trigger`. |
| "The run crashed for an unrelated reason, I will tweak params to get past it." | Params are locked. Fix the code, or judge it as a crash and draft again. |
| "The budget is nearly spent, I will make the last runs bigger." | The budget is the person's. Say what more would buy, and stop. |

## The steps

**Hypothesis** (`arl hyp new`). A claim about the problem, with why you believe
it, what should be seen if it is true, and what would refute it. No code, no
plan. Look at the open hypotheses and at IDEAS.md before writing a new one.

**Design** (`arl exp new`). Pick the kind:

| Kind | Use it when | Changes | Scored | Merged |
|---|---|---|---|---|
| `baseline` | a track has no reference, the evaluator changed, or you want to see the noise | nothing | yes | it becomes the reference |
| `tune` | the claim is about a setting | `params.json` | yes | params, on keep |
| `feature` | the claim needs new code | code in the worktree | yes | code and params, on keep |
| `diagnose` | you do not know why something happens | instrumentation, or its own command | no | never |

Reach for `diagnose` when two results contradict each other, when a change did
the opposite of what you predicted, or before a feature whose mechanism you
have not seen. An hour of looking often saves five experiments.

**Run** (`arl exp run`). Start long runs in the background and wait for them to
end. While one runs you may write hypotheses and ideas.

**Judge** (`arl exp judge`). Work from `design.md` and `arl exp show`, not from
what you hoped. Write what happened against the prediction, the most likely
reason, and what you did not check. Before believing a good number, check that
the work was done: the run time is plausible, the instances were all scored,
nothing was skipped. Results that flatter the method deserve the hardest look.

**Review** (`arl review new`, `arl review close`). If you can start a subagent,
give the review to one that has not done the work. Pass it paths, not
conclusions: `PROGRAM.md`, `STATUS.md`, `IDEAS.md`, `tracks/`, the review file,
the experiments in the window. The decisions:

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
later review waits for a person too. Tripwires written in one review are
checked first in the next.

Tracks follow reviews. Once the first experiment is judged, opening a track,
parking one or reopening one needs `--review <id>` of the latest closed
review. Parking a track that stalled does not make its review go away.

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
fails; a result is too good to be explained by the change; you would need to
touch something `PROGRAM.md` puts out of scope. An escalation is a review:

```
arl review new --trigger "escalation: <what>"
(write it: what happened, the options, what you recommend)
arl review close <id> --decision escalate
```
