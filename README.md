# autoresearch-local

A small harness for automated research on one machine. An agent proposes
hypotheses, designs experiments, runs them and judges the results, in a loop,
for hours or days. The harness (`arl`) owns the mechanics that an agent should
not be trusted to remember: the evaluation it may not touch, the record of what
was run under which conditions, the git branches, the moments at which it has
to stop and reconsider, and the state needed to stop the work and take it up
again later.

It follows [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
(one program file, keep or discard on a branch, a fixed evaluation) and takes
the structure of larger, cloud-based systems (hypotheses and experiments as
separate records, experiment types, worktrees), without the cloud: no job
queue, no tracking server, no parallel fleet. One process, one git repository,
plain files. Python 3.11 or newer, standard library only, nothing to install.

    ~/private/open-source/autoresearch-local/bin/arl --help

The framework lives here. Each project keeps its own `research/` directory.
The examples use the path it has on the machine it was written on, which is
also the path the skill file names. Cloned anywhere else it works the same:
`arl` is called by its path.

## The ideas and how each is enforced

| Idea | What arl does about it |
|---|---|
| **The evaluation is frozen** | `PROGRAM.md`, `arl.toml` and the evaluator files form the contract. `arl freeze` hashes them into `frozen.lock`; every run checks the hashes before and after. The evaluator runs from the home checkout, never from a track's worktree, on what the experiment wrote. Once an experiment exists, a new freeze needs a person at a terminal. Scores are compared only within one evaluator version, on the same data files and with the same versions of the packages the contract names. |
| **Hypothesis, design and evaluation are separate** | Three records written at three times. A hypothesis is a claim with an observable consequence, no plan. A design names the change and the prediction; its hash is taken when the run starts, so it cannot be adjusted to the result. The number comes from the frozen evaluator, the outcome (improve, neutral, worse, constraint not met) is computed, and the verdict has to be one that outcome allows. |
| **Experiments come in kinds** | `baseline` measures a track as it is. `tune` changes params only. `feature` changes code. `diagnose` observes: not scored, never merged. Each kind is held to what it may change: a baseline with other params than its track's is refused, and so is a tune with a code change. |
| **Parallel code tracks** | A track is one approach: a branch (`track/<name>`) with its own worktree. A code experiment gets a branch off the track head. Keep is a fast-forward of the track, discard leaves the branch where it is. arl never resets, rebases or pushes, and never deletes a branch. |
| **Everything can be recalled** | Each experiment has a `record.json`: commit, params, command, contract version, the environment and its package list, the version of arl, data hashes, timings, the parts the run was made of, artifact hashes, metrics, comparison, verdict. A run whose code or contract changed while it was going is failed, not recorded. The ledger is committed at every step. `arl exp show` reads it back, `arl exp reproduce` runs it again from the recorded commit, `arl track new --from <id>` starts a new line of work from it, `arl verify` checks ledger, contract and git against each other. Below. |
| **One environment** | The contract names the environment runs use (`[env] prefix`). arl puts it first on the PATH of every job, whoever started the command, asks its python what it is before each run, and refuses to start when it is not that environment. The contract also names the packages that must not change between two results that are compared (`[env] packages`). Below. |
| **The introspection step** | After a set number of experiments, a run of failures on one track, or a number of days, no new experiment can be drafted until an introspection is written and closed. It has four parts, below. Decisions the agent may not take alone wait for a person, and nothing runs while they wait. Tracks are opened, parked and reopened only by the latest closed introspection or by a person. After a decision to stop, only a person reopens the program. |
| **A document of ideas** | `research/IDEAS.md`. Whatever comes up that is outside the current experiment is written there instead of chased. Every introspection goes through the open ones. |
| **Stop and continue** | The work can be interrupted and taken up later. A run is made of parts: paused, cut off by a closed terminal or a restart, it continues in the same folder from the state its job last saved, and no attempt is lost. The loop as a whole is paused with a note for whoever continues. Below. |

## The introspection step

`arl introspect new` writes a file with the facts filled in: the experiments
judged since the last one, the state of every track, the budget, the open
hypotheses and ideas, the tripwires the last introspection set, and what the
activity log holds (commands arl refused, runs that failed or were cut short,
notes written in passing). The agent writes the rest, in four parts:

| Part | Question | What it leaves behind |
|---|---|---|
| 1. The path | Is the current approach still worth it, does it need a bigger change, or is a different approach due? | A decision: continue, adjust, branch, pivot, stop or escalate, with tripwires for the next time |
| 2. How the work went | Which behaviours or errors of the agents are worth a standing instruction? Not about the problem: about the way of working | Lessons, added with `arl lesson add` to the instructions agents load: `skill/LESSONS.md` here for what holds in any project, `research/LESSONS.md` for what holds in one |
| 3. What we know | What did the work reveal about the problem itself? | `research/KNOWLEDGE.md`, revised: a distillation of everything found so far, in sections by how sure it is (established, likely, ruled out, open) |
| 4. Reload | | `arl reload` prints the knowledge and the lessons as they now are. Until an agent has run it, nothing new starts |

`arl introspect close` refuses while a part is missing: no lesson recorded and
no reason given why there is none, or a knowledge file that is byte for byte
what it was when the introspection was opened. The raw material for parts 2 and
3 is collected as the work goes: every refused command is logged with its
message, and `arl note --about work|problem "..."` takes one line at the moment
something is noticed.

`arl reload` is also how a session starts: it is the one command that brings
an agent up to date with what earlier sessions learned. And when one of the
three files changes in between, whoever changed it (the person correcting the
knowledge file, a loop in another project adding a general lesson), `arl next`
asks for a reload before anything else.

## Stop and continue

Two levels, and both survive a closed terminal or a restart of the machine.

**A run.** `arl exp pause` asks the running job to save its state and stop;
`arl exp run <id>` continues it later. The same happens without being asked
when the terminal that runs arl closes, when arl is interrupted or killed, when
the machine restarts, or when free memory falls below the floor in `arl.toml`:
the part ends, the run is marked paused, and the next `arl exp run` goes on in
the same output folder. Only a real failure (a non-zero exit, the time limit
over all parts, the run's own memory limit) uses up an attempt. If arl was
killed outright, the next run first stops the job it left behind.

A job is told what it needs through its environment: `ARL_CHECKPOINT_EVERY`
(seconds between two saves, from `[run] checkpoint_every_s`: often enough that
a pause costs little, seldom enough that saving costs nothing), `ARL_RESUME`
(1 when an earlier part exists) and SIGTERM as the request to stop, with
`[run] pause_grace_s` to finish saving. State goes into `{out}/checkpoint/`.
[helpers/arl_job.py](helpers/arl_job.py) is a file to copy next to a run
script that does this in five lines. A job that saves nothing still runs; arl
points out a part that ran long without a save, and a pause then costs that
job its work.

**The loop.** `arl pause --reason "..." --note "where I was, what comes next"`
stops everything: a run that is going is asked to pause, and nothing starts,
runs or is judged until `arl resume`. The agent runs it when told to stop, or
the person runs it from a terminal while the agent works, which then finds a
STOP at its next command. A pause set at a terminal is lifted at a terminal.
The note is printed again at the resume, with the knowledge and the lessons, so
a new session (or a new agent) starts from what the last one wrote and not from
its memory. If a session ends with no pause at all, the ledger has every step
that was committed and `arl next` finds the place.

## The environment

Without `[env] prefix` a job gets whatever `python` the calling shell has: the
agent's session, or the terminal of a person who has another environment
active. With it, that no longer matters:

```toml
[env]
prefix = "~/anaconda3/envs/project"     # a conda env or a venv
packages = ["numpy", "jax", "optax"]    # must be the same for two results to be compared
command = "python -m pip freeze"        # the whole list, stored with every run
```

- **`prefix`.** Every run, evaluation and rerun starts with `<prefix>/bin`
  first on its PATH, with `CONDA_PREFIX` or `VIRTUAL_ENV` set to it, and
  without what would point python elsewhere (`PYTHONPATH`, `PYTHONHOME`,
  packages installed with `--user`). Before each of them arl asks that python
  for its `sys.prefix`. If the environment is not on the machine, or the
  answer is another one, nothing starts: `arl next` says `environment`, a
  STOP, and the brief tells the agent not to create it, install into it or
  move the work elsewhere. A contract that names an environment that is not
  there is not frozen.
- **`packages`.** Their versions, and python's, are written into every record.
  A track's reference counts only while they are what they were when it was
  measured. When one changes (an upgrade, an install for other work in a
  shared environment), `next` asks for a new baseline before the next scored
  experiment, a draft made before the change is `outdated`, and a paused run
  is not continued. Name the packages the method and the evaluator compute
  with, not everything: a list that changes daily asks for a baseline daily.
- **`command`.** Its output is the full list, stored once per distinct content
  under `research/envs/`. It gates nothing. It is what rebuilds the
  environment on another machine.

What an agent types itself (a quick check in python, a test run) uses its own
shell. `arl reload`, the first command of a session, says where runs happen
and, when the shell's `python` is another one, which interpreter to call.

## Going back to an experiment

Say an experiment from last week turns out to matter: it scored well on a
metric nobody was watching, or it was discarded and now looks right.

```sh
arl exp list                        # every experiment: result, outcome, verdict
arl exp show E0007                  # what it was, what it ran with, what came of it
arl exp reproduce E0007             # run it again as it was, compare with the record
arl exp reproduce E0007 --split confirm
arl track new again --from E0007    # a new line of work that starts there
```

- **Look.** `exp show` prints the scores, the verdict and its reason, the
  commit, the params, the environment, the contract version, the arl version,
  and the git command that shows its code change. The folder
  `research/experiments/E0007-<slug>/` holds the design with the prediction
  written before the run, the analysis written after, the small result files,
  and the end of the logs. `git log -- research/experiments/E0007-*` shows
  the steps as they were committed.
- **Run it again.** `exp reproduce` checks the recorded commit out into a
  folder of its own, runs it with its params, evaluates it, and stores the
  result next to the first one, with the difference when the evaluator is
  still the one that scored it. The code of every experiment stays reachable:
  a kept one is in its track, a discarded one on its own branch
  (`exp/E0007-<slug>`), and arl never deletes a branch. If named packages
  changed since, the rerun says which.
- **Start from it.** `track new --from E0007` opens a track at the code E0007
  ran, with its params. The first experiment there is a baseline, which
  measures that state again, and work goes on from it. Like every change of
  direction after the first verdict, it takes the latest closed introspection
  (`--introspection N004`) or a person at a terminal.

What is not in git: the output files of a run (`artifacts/`) and the full
logs. Their names, sizes and hashes are in the record; the files stay on the
machine that ran them. On another machine `exp reproduce` makes them again.

## On another machine

Everything the harness is lives in this repository: the code, the templates,
the helper, the protocol, the skill and the lessons agents have added.

```sh
git clone <url> ~/private/open-source/autoresearch-local
cd ~/private/open-source/autoresearch-local && python -m pytest    # about five minutes
# make the skill visible to Claude Code sessions started under ~/private/open-source
mkdir -p ../.claude/skills/research-loop
ln -s ../../../autoresearch-local/skill/SKILL.md ../.claude/skills/research-loop/SKILL.md
ln -s ../../../autoresearch-local/skill/LESSONS.md ../.claude/skills/research-loop/LESSONS.md
```

The skill names the path above. In another place, change the paths in
`skill/SKILL.md`.

A project is its own repository, and what was committed there comes along
with a clone:

```sh
git clone <url> myproject && cd myproject
arl track worktree <name>        # for each track: its branch and its worktree, where the ledger says they stood
arl verify                       # ledger, contract and git agree
arl exp reproduce E0007          # an old result, made again here
```

Put its data where `[data] files` says and create the environment `[env]
prefix` names; the lists under `research/envs/` say what was in it. Then a
baseline: another machine is another condition, and the data files are
compared by path and content. Records say which arl ran them (version and
commit), so a result can be traced to the harness that produced it.

## A project, start to finish

```sh
cd ~/private/open-source/myproject          # a git repository
arl init                                    # creates research/ with templates
# write research/PROGRAM.md and research/arl.toml, put the evaluator in research/eval/,
# say what is already known in research/KNOWLEDGE.md
arl freeze --reason "initial"
arl track new first-approach                # branch + worktree; then write research/tracks/first-approach.md
arl reload                                  # at the start of every session
arl next                                    # from here on, the loop
```

`arl next` names the one thing to do and prints the steps. An agent runs it,
does what it says, and runs it again:

| `next` says | Meaning |
|---|---|
| `setup`, `freeze` | no `research/` yet, or the contract is written and not frozen |
| `track` | no active track: open one |
| `baseline <track>` | the track has no reference under the current evaluator, data and named packages |
| `hypothesize` | nothing in flight: write a hypothesis, then draft an experiment |
| `design <id>` | a draft exists: write the design, change params or code, run |
| `outdated <id>` | a draft whose reference no longer counts: drop it, measure the track again |
| `running <id>` | wait |
| `interrupted <id>` | the arl that ran it is gone: continue the run |
| `resume <id>` | the run was paused: continue it, or give it up |
| `judge <id>` | a result exists: write the analysis, record the verdict |
| `unfinished <id>` | a verdict was cut off halfway: one command completes it |
| `crashed <id>` | the run failed: fix and rerun, or judge it as a crash |
| `introspect` | the introspection is due, or one is open |
| `reload` | an introspection was closed, or the knowledge or lessons files changed since they were last read (a person corrected one, another project added a general lesson): read them again |
| `paused`, `wait`, `contract`, `environment`, `budget`, `target`, `stopped` | STOP: a person is needed |

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
  `ARL_RESEARCH`, `ARL_EXP`, with `ARL_RESUME` and `ARL_CHECKPOINT_EVERY`.
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
A run in progress is stopped, with every process it started, when it passes
its time limit or when its processes hold more RAM than `max_run_rss_gb`
(a failure), and paused when the machine falls below `stop_below_free_ram_gb`
(not a failure: it continues when there is room again).

The budget counts experiments that ran and the hours of every run: failed
ones, interrupted parts and reruns included. Without `max_run_hours` nothing
bounds reruns.

Every value in `arl.toml` is checked when the file is read: a wrong type or an
impossible number is named before the contract is frozen.

## Layout of `research/`

```
arl.toml  PROGRAM.md  eval/      the contract (frozen)
frozen.lock                      its hashes, versions and history
KNOWLEDGE.md                     what the work has shown about the problem, distilled
LESSONS.md                       how to work in this project
IDEAS.md                         ideas outside the current path
STATUS.md                        written by arl: tracks, experiments, what is next
activity.jsonl                   refused commands and notes, read at introspections
PAUSED                           there while the loop is paused, with the note
hypotheses/H003-<slug>.md
tracks/<name>.md                 approach, premises, "stop if"; <name>.params.json
experiments/E0007-<slug>/
    design.md  params.json       written before the run, locked when it starts
    record.json                  written by arl: conditions, parts, result, verdict
    metrics.json                 the evaluator's output
    analysis.md                  written after
    kept/  run.tail.txt          small result files and the end of the log
    artifacts/  logs/            not in git (hashes are in the record)
    artifacts/run-1/checkpoint/  the state a job saves to be continued
introspections/N002.md
envs/<hash>.txt                  package listings, one per distinct content
```

The ledger lives on the project's home branch. Track branches fork from it and
carry a stale copy of `research/` that nothing reads: arl always writes the
home checkout, also when called from inside a worktree, and refuses experiment
code that touches `research/`.

Lessons that hold for any project live in this repository, in
[skill/LESSONS.md](skill/LESSONS.md), next to the skill that agents load. A
person prunes that file from time to time and moves what has proved itself
into [PROTOCOL.md](PROTOCOL.md).

## Commands

```
arl init | freeze | status | next | verify
arl reload | pause | resume | note
arl track      new | set | sync | worktree | list
arl hyp        new | set | list
arl idea       add | set | list
arl lesson     add | list
arl exp        new | commit | run | pause | fail | judge | abandon | show | list | log | reproduce
arl introspect due | new | close | resolve
```

`arl <command> --help` for each. Exit codes of `exp run`: 0 a result, 2 the run
or the evaluator failed, 3 the run was paused and can be continued, 75 the
machine is busy, 1 a gate refused.

## What it does not do

- No parallel runs. Tracks are parallel lines of code, runs are one at a time.
- No sweeps. A grid is several `tune` experiments.
- No judgment about noise. `min_delta` is the project's statement of what
  counts, and a repeat with `--allow-duplicate` or a second split is how to
  check it.
- No checkpoints of its own. arl tells a job when to save and when to stop;
  what the state is and how to continue from it is the job's business.
- No driver. arl does not start the agent or wake it up. A session runs the
  loop until `next` says STOP, and the next session picks it up from the
  ledger.
- No full activation of an environment. A job gets the environment's `bin`
  first on its PATH and the variables above, not what `conda activate` would
  add from the environment's own scripts. A project that needs those sets them
  in its command.
- No watch over packages the contract does not name. Their change is on
  record (the stored lists differ) and gates nothing.
- No guard against a determined cheat. The hashes and gates stop drift and
  rationalization, not someone who edits `frozen.lock` by hand. Git shows that.
  "A person at a terminal" is a check that the command runs in a terminal; a
  program can fake one. The protocol forbids it, nothing prevents it.
- No way to stop a `diagnose` command from calling the evaluator itself. That
  is rule 2 of the protocol, not a gate.

## For agents

Read [PROTOCOL.md](PROTOCOL.md) before the first command of a session. The
skill in `skill/SKILL.md` is the short version.

## Tests

```sh
python -m pytest
```

Linux only: the tests, like arl itself, read `/proc` and use sessions and
signals to stop jobs.

A toy project in a temporary git repository is driven through the real CLI.
Every stage and every gate has a test (`tests/test_flow.py`,
`tests/test_introspection.py`, `tests/test_env.py` with a real environment of
its own, `tests/test_pause.py` with real jobs that are paused, hung up on and
killed). Every way of breaking a guarantee
that was found by trying has one too (`tests/test_hardening.py`). And
`tests/test_walk.py` walks the loop at random the way an agent would, with
typed mistakes, cut-off runs, pauses, hand edits, a missing environment and
changed packages in between, and checks after every step that nothing is stuck
or inconsistent.
