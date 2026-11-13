# autoresearch-local

A small harness for automated research on one machine. An agent proposes
hypotheses, designs experiments, runs them and judges the results, in a loop,
for hours or days. The harness (`arl`) owns the mechanics that an agent should
not be trusted to remember: the evaluation it may not touch, the record of what
was run under which conditions, the git branches, and the moments at which it
has to stop and reconsider.

It follows [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
(one program file, keep or discard on a branch, a fixed evaluation) and takes
the structure of larger, cloud-based systems (hypotheses and experiments as
separate records, experiment types, worktrees), without the cloud: no job
queue, no tracking server, no parallel fleet. One process, one git repository,
plain files. Python 3.11 or newer, standard library only, nothing to install.

    ~/private/open-source/autoresearch-local/bin/arl --help

The framework lives here. Each project keeps its own `research/` directory.

## The ideas and how each is enforced

| Idea | What arl does about it |
|---|---|
| **The evaluation is frozen** | `PROGRAM.md`, `arl.toml` and the evaluator files form the contract. `arl freeze` hashes them into `frozen.lock`; every run checks the hashes before and after. The evaluator runs from the home checkout, never from a track's worktree, on what the experiment wrote. Once an experiment exists, a new freeze needs a person at a terminal. Scores are compared only within one evaluator version. |
| **Hypothesis, design and evaluation are separate** | Three records written at three times. A hypothesis is a claim with an observable consequence, no plan. A design names the change and the prediction; its hash is taken when the run starts, so it cannot be adjusted to the result. The number comes from the frozen evaluator, the outcome (improve, neutral, worse, constraint not met) is computed, and the verdict has to be one that outcome allows. |
| **Experiments come in kinds** | `baseline` measures a track as it is. `tune` changes params only. `feature` changes code. `diagnose` observes: not scored, never merged. Each kind is held to what it may change. |
| **Parallel code tracks** | A track is one approach: a branch (`track/<name>`) with its own worktree. A code experiment gets a branch off the track head. Keep is a fast-forward of the track, discard leaves the branch where it is. arl never resets, rebases, deletes or pushes. |
| **Everything can be recalled** | Each experiment has a `record.json`: commit, params, command, contract version, package list, data hashes, timings, artifact hashes, metrics, comparison, verdict. The ledger is committed at every step. `arl exp show` reads it back, `arl exp reproduce` runs it again from the recorded commit, `arl verify` checks ledger, contract and git against each other. |
| **Periodic reconsideration** | After a set number of experiments, a run of failures on one track, or a number of days, no new experiment can be drafted until a review is written: where things stand, whether each track's premises still hold, the case against the current path, the alternatives, a decision. Decisions the agent may not take alone wait for a person. New tracks come out of a review. |
| **A document of ideas** | `research/IDEAS.md`. Whatever comes up that is outside the current experiment is written there instead of chased. Every review goes through the open ones. |

## A project, start to finish

```sh
cd ~/private/open-source/myproject          # a git repository
arl init                                    # creates research/ with templates
# write research/PROGRAM.md and research/arl.toml, put the evaluator in research/eval/
arl freeze --reason "initial"
arl track new first-approach                # branch + worktree; then write research/tracks/first-approach.md
arl next                                    # from here on, the loop
```

`arl next` names the one thing to do and prints the steps. An agent runs it,
does what it says, and runs it again:

| `next` says | Meaning |
|---|---|
| `baseline <track>` | the track has no reference under the current evaluator |
| `hypothesize` | nothing in flight: write a hypothesis, then draft an experiment |
| `design <id>` | a draft exists: write the design, change params or code, run |
| `running <id>` | wait |
| `judge <id>` | a result exists: write the analysis, record the verdict |
| `crashed <id>` | the run failed: fix and rerun, or judge it as a crash |
| `review` | the reconsideration is due |
| `wait`, `contract`, `budget`, `target`, `stopped` | stop: a person is needed |

One experiment, as commands:

```sh
arl hyp new --title "a larger margin keeps more coils clear of the plasma"   # then write the file
arl exp new --type tune --track first-approach --hyp H001 --title "margin 1.05"
# write design.md (the prediction), edit params.json
arl exp run E0002                           # checks, locks the design, runs, evaluates
# write analysis.md
arl exp judge E0002 --verdict keep --reason "..." --hyp H001=supported
```

For a `feature` or `diagnose` experiment the code is changed in the track's
worktree and committed with `arl exp commit E0003 -m "..."`.

## What a project provides

Two commands in `arl.toml`:

- `[run] command`: runs in the track's worktree and writes its results into
  `{out}`. Placeholders: `{params}` (the experiment's `params.json`), `{out}`,
  `{split}`, `{home}`, `{research}`, `{exp}`, `{worktree}`. The same values are
  in the environment as `ARL_PARAMS`, `ARL_OUT`, `ARL_SPLIT`, `ARL_HOME`,
  `ARL_RESEARCH`, `ARL_EXP`.
- `[eval] command`: runs in the home checkout, reads `{out}`, and writes one
  JSON object to `{metrics}` with a number for every `[[metric]]`.

The evaluator must not import code that experiments can change, and the run
must not compute its own score. One metric is `primary` (with `min_delta`, the
smallest change that counts). `constraint` metrics have a `limit` or a
`max_regression` and must hold for a result to be kept. `info` metrics are
recorded.

If `[eval] splits` names more than one split, the first scores every
experiment and the others are reached only through
`arl exp reproduce <id> --split <name>`: a check on data the loop never tuned
on. Those results are stored and never used for a verdict.

`[machine]` holds gates for a shared machine: a run does not start above a
load average, below a free amount of RAM or GPU memory, or while another run
holds the lock file (one file can be shared by every project on the machine).
A run in progress is stopped, with everything it started, when it passes its
time limit, when its processes hold more RAM than `max_run_rss_gb`, or when
the machine falls below `stop_below_free_ram_gb`.

## Layout of `research/`

```
arl.toml  PROGRAM.md  eval/      the contract (frozen)
frozen.lock                      its hashes, versions and history
IDEAS.md                         ideas outside the current path
STATUS.md                        written by arl: tracks, experiments, what is next
hypotheses/H003-<slug>.md
tracks/<name>.md                 approach, premises, "stop if"; <name>.params.json
experiments/E0007-<slug>/
    design.md  params.json       written before the run, locked when it starts
    record.json                  written by arl: conditions, result, verdict
    metrics.json                 the evaluator's output
    analysis.md                  written after
    kept/  run.tail.txt          small result files and the end of the log
    artifacts/  logs/            not in git (hashes are in the record)
reviews/R002.md
envs/<hash>.txt                  package listings, one per distinct content
```

The ledger lives on the project's home branch. Track branches fork from it and
carry a stale copy of `research/` that nothing reads: arl always writes the
home checkout, also when called from inside a worktree, and refuses experiment
code that touches `research/`.

## Commands

```
arl init | freeze | status | next | verify
arl track  new | set | sync | list
arl hyp    new | set | list
arl idea   add | set | list
arl exp    new | commit | run | judge | abandon | show | list | log | reproduce
arl review due | new | close | resolve
```

`arl <command> --help` for each. Exit codes of `exp run`: 0 a result, 2 the run
or the evaluator failed, 75 the machine is busy, 1 a gate refused.

## What it does not do

- No parallel runs. Tracks are parallel lines of code, runs are one at a time.
- No sweeps. A grid is several `tune` experiments.
- No judgment about noise. `min_delta` is the project's statement of what
  counts, and a repeat with `--allow-duplicate` or a second split is how to
  check it.
- No guard against a determined cheat. The hashes and gates stop drift and
  rationalization, not someone who edits `frozen.lock` by hand. Git shows that.

## For agents

Read [PROTOCOL.md](PROTOCOL.md) before the first command of a session. The
skill in `skill/SKILL.md` is the short version.

## Tests

```sh
python -m pytest
```

A toy project in a temporary git repository is driven through the real CLI:
every stage and every gate has a test.
