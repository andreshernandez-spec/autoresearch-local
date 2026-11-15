"""The parts that can be checked without a project."""

from __future__ import annotations

import pytest

from arl import config, record, rules, runner
from arl.gitops import Git
from arl.record import Comparison, Record
from arl.util import ArlError, canonical, join_front, split_front, unfilled

TOML = """
[project]
name = "t"
[run]
command = "run"
[eval]
command = "eval"
frozen = ["research/eval"]
[[metric]]
name = "score"
goal = "max"
role = "primary"
min_delta = 0.01
[[metric]]
name = "seconds"
goal = "min"
role = "constraint"
limit = 10.0
max_regression = 2.0
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "arl.toml"
    path.write_text(TOML)
    return config.load(path)


def ref(score, seconds, fails=()):
    """A kept experiment, as far as a comparison looks at it."""
    comparison = Comparison(metric="score", value=score, gate_failures=list(fails), target_met=False, outcome="reference")
    made = dict(title="the reference", type="baseline", track="main-line", hypotheses=[], status="judged", created="", base_commit="", branch="")
    return Record(id="E0001", metrics={"score": score, "seconds": seconds}, comparison=comparison, **made)


def test_compare(cfg):
    def outcome(score, seconds, reference):
        return rules.compare(cfg, {"score": score, "seconds": seconds}, reference).outcome

    assert outcome(0.5, 1.0, None) == "reference"
    assert outcome(0.52, 1.0, ref(0.5, 1.0)) == "improve"
    assert outcome(0.505, 1.0, ref(0.5, 1.0)) == "neutral"
    assert outcome(0.495, 1.0, ref(0.5, 1.0)) == "neutral"
    assert outcome(0.48, 1.0, ref(0.5, 1.0)) == "worse"
    assert outcome(0.9, 11.0, ref(0.5, 1.0)) == "gate_fail"  # over the absolute limit
    assert outcome(0.9, 4.0, ref(0.5, 1.0)) == "gate_fail"  # more than 2 slower than the reference
    assert outcome(0.4, 5.0, ref(0.5, 12.0, ["seconds"])) == "improve"  # inside beats outside


def test_min_goal(tmp_path):
    path = tmp_path / "arl.toml"
    path.write_text(TOML.replace('goal = "max"', 'goal = "min"'))
    cfg = config.load(path)
    assert rules.compare(cfg, {"score": 0.4, "seconds": 1.0}, ref(0.5, 1.0)).outcome == "improve"
    assert rules.compare(cfg, {"score": 0.6, "seconds": 1.0}, ref(0.5, 1.0)).outcome == "worse"


@pytest.mark.parametrize(
    "broken, message",
    [
        (TOML.replace('role = "primary"', 'role = "info"'), "exactly one"),
        (TOML.replace("limit = 10.0\nmax_regression = 2.0", ""), "needs 'limit' or 'max_regression'"),
        (TOML.replace('command = "eval"', ""), "needs 'command'"),
        (TOML + '[review]\nagent_may = ["continue", "give up"]\n', "unknown decisions"),
        (TOML + "[introspection]\nevery = 0\n", "every must be at least 1"),
        (TOML + "[introspection]\nevery = 3\n[review]\nevery = 4\n", "old name of"),
        (TOML.replace('command = "run"', 'command = "run"\ntimeout_s = "soon"'), "timeout_s must be a number"),
        (TOML.replace('command = "run"', 'command = "run"\nmax_attempts = 2.5'), "max_attempts must be a whole number"),
        (TOML.replace('command = "run"', 'command = "run"\nkeep = "*.json"'), "keep must be a list"),
        (TOML.replace('command = "run"', "command = 3"), "command must be text"),
        (TOML.replace("min_delta = 0.01", "min_delta = -1"), "min_delta must be at least 0"),
        (TOML + "[machine]\nmax_loadavg = true\n", "max_loadavg must be a number"),
        ("run = 1\n" + TOML.replace("[run]", "[other]"), "must be a table"),
        (TOML + '[env]\npackages = ["numpy"]\n', "packages is not an option"),
        (TOML + "[env]\nprefix = 3\n", "prefix must be text"),
    ],
)
def test_bad_config(tmp_path, broken, message):
    path = tmp_path / "arl.toml"
    path.write_text(broken)
    with pytest.raises(ArlError, match=message):
        config.load(path)


def test_old_and_new_name_of_the_introspection_table(tmp_path):
    path = tmp_path / "arl.toml"
    for name in ("introspection", "review"):
        path.write_text(TOML + f'[{name}]\nevery = 3\nstagnation = 2\nmax_days = 1.5\nagent_may = ["continue"]\n')
        cfg = config.load(path)
        assert (cfg.intro_every, cfg.intro_stagnation, cfg.intro_max_days, cfg.agent_may) == (3, 2, 1.5, ["continue"])
    path.write_text(TOML)
    cfg = config.load(path)
    assert (cfg.intro_every, cfg.checkpoint_every, cfg.pause_grace) == (8, 600.0, 60.0)


def test_the_environment_table(tmp_path, cfg):
    assert (cfg.env_prefix, cfg.env_command) == (None, None)
    path = tmp_path / "arl.toml"
    path.write_text(TOML + '[env]\nprefix = "~/envs/x"\ncommand = "pip freeze"\n')
    named = config.load(path)
    assert (named.env_prefix, named.env_command) == ("~/envs/x", "pip freeze")


def test_what_differs_between_two_package_lists():
    old = (
        "python==3.13.1\nPillow==11.0\nscikit_learn==1.7.0\nmine==0.1 (editable /src/mine at aaaaaaaaaaaa)\n"
        "conda: libblas=3.9.0=20_mkl\nconda: pillow=11.0=py313_0\n"
    )
    new = (
        "python==3.13.1\npillow==11.1\nscikit-learn==1.7.0\nmine==0.1 (editable /src/mine at aaaaaaaaaaaa, modified)\ntqdm==4.67\n"
        "conda: libblas=3.9.0=20_netlib\nconda: pillow=11.1=py313_0\n"
    )
    assert runner.listing_changes(old, new) == [
        "mine 0.1 (editable /src/mine at aaaaaaaaaaaa) -> 0.1 (editable /src/mine at aaaaaaaaaaaa, modified)",
        "pillow 11.0 -> 11.1",  # one line: what conda says about it repeats the version
        "conda libblas 3.9.0 20_mkl -> 3.9.0 20_netlib",  # a library python does not list, rebuilt
        "tqdm 4.67 added",
    ]
    assert runner.listing_changes(old, old) == []
    # the lines pip freeze writes
    then = "jax==0.11.0\n-e git+ssh://git@host/me/pkg.git@765853d57a44a7c9bf22#egg=pkg\nold @ file:///build/1/work\n"
    now_ = "jax==0.12.1\n-e git+ssh://git@host/me/pkg.git@0955987aaaabbbbcccc#egg=pkg\n"
    assert runner.listing_changes(then, now_) == ["jax 0.11.0 -> 0.12.1", "pkg editable at 765853d57a44 -> editable at 0955987aaaab", "old from file:///build/1/work removed"]
    assert runner.short(["a", "b", "c"], 2) == "a; b; and 1 more" and runner.differences(1) == "1 difference"


def test_what_a_run_reports_about_itself(tmp_path):
    path = tmp_path / "train_metrics.json"
    assert runner.read_train_metrics(path) == ({}, "")
    path.write_text('{"loss": 0.25, "steps": 40, "note": "text", "bad": NaN, "flag": true}')
    assert runner.read_train_metrics(path) == ({"loss": 0.25, "steps": 40.0}, "")
    path.write_text("not json")
    said, why = runner.read_train_metrics(path)
    assert said == {} and "is not JSON" in why
    path.write_text("[1, 2]")
    assert "one JSON object" in runner.read_train_metrics(path)[1]


def test_metrics_must_be_finite(cfg, tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text('{"score": 0.5, "seconds": 1, "note": "text", "extra": 3}')
    assert runner.read_metrics(path, cfg) == {"score": 0.5, "seconds": 1.0, "extra": 3.0}
    path.write_text('{"score": 0.5, "seconds": 1, "extra": NaN, "more": Infinity}')
    assert runner.read_metrics(path, cfg) == {"score": 0.5, "seconds": 1.0}  # what is only reported may be missing
    path.write_text('{"score": NaN, "seconds": 1}')
    with pytest.raises(ArlError, match="score = nan"):
        runner.read_metrics(path, cfg)
    path.write_text('{"score": 0.5}')
    with pytest.raises(ArlError, match="did not report 'seconds'"):
        runner.read_metrics(path, cfg)


def test_git_allow_list(tmp_path):
    g = Git(tmp_path)
    for bad in (
        ("push",),
        ("reset", "--hard"),
        ("checkout", "."),
        ("clean", "-fd"),
        ("rebase", "main"),
        ("stash",),
        ("branch", "-D", "x"),
        ("merge", "x"),
        ("commit", "--amend"),
        ("switch", "--discard-changes", "x"),
        ("worktree", "remove", "x"),
        ("config", "user.email", "x"),
    ):
        with pytest.raises(ArlError, match="arl"):
            g(*bad)


def test_front_matter_round_trip():
    meta = {"id": "H001", "title": 'He said "x = y", then left\\', "tags": ["a", "b"], "n": 3, "ok": True, "skip": None}
    text = join_front(meta, "Body\n")
    back, body = split_front(text)
    assert back == {k: v for k, v in meta.items() if v is not None} and body == "Body\n"
    assert split_front("no front matter") == ({}, "no front matter")


def test_fill_markers():
    assert unfilled("## A\n\n> FILL: say it\n\ntext\nFILL: again\n") == ["> FILL: say it", "FILL: again"]
    assert unfilled("We FILL: nothing here") == []


def test_placeholders_are_quoted():
    out = runner.substitute("run --out {out} --keep {other} ${HOME}", {"out": "/tmp/a b"})
    assert out == "run --out '/tmp/a b' --keep {other} ${HOME}"


# --- records as types -------------------------------------------------------------

RECORD = {
    "id": "E0007",
    "title": "x 0.5",
    "type": "tune",
    "track": "main-line",
    "hypotheses": ["H001"],
    "status": "judged",
    "created": "2026-01-01T10:00:00+00:00",
    "base_commit": "a" * 40,
    "branch": "track/main-line",
    "command": "",
    "attempts": [
        {
            "n": 1,
            "started": "2026-01-01T10:01:00+00:00",
            "code_commit": "a" * 40,
            "segments": [
                {"started": "2026-01-01T10:01:00+00:00", "env": {"host": "here"}, "ended": "2026-01-01T10:01:02+00:00", "ended_by": "paused", "seconds": 1.5},
                {"started": "2026-01-01T10:05:00+00:00", "env": {"host": "here"}, "ended": "2026-01-01T10:05:01+00:00", "ended_by": "finished", "seconds": 1},
            ],
            "exit_code": 0,
            "run_seconds": 2.5,
            "run_done": True,
            "eval_seconds": 0.2,
            "ended": "2026-01-01T10:05:02+00:00",
        }
    ],
    "contract": {"version": 1, "digest": "d", "eval_digest": "e"},
    "conditions": {"command": "run", "split": "dev", "worktree": "/wt", "data": [], "data_digest": "dd", "env": {"host": "here"}},
    "code_commit": "a" * 40,
    "dedup": "k",
    "design": {"design_sha256": "1", "params_sha256": "2", "locked_at": "2026-01-01T10:01:00+00:00"},
    "artifacts": [{"path": "prediction.json", "bytes": 12, "sha256": "3"}],
    "metrics": {"score": 0.8, "cost": 0.5},
    "comparison": {"metric": "score", "value": 0.8, "gate_failures": [], "target_met": False, "reference": "E0001", "reference_value": 0.3, "gain": 0.5, "min_delta": 0.01, "outcome": "improve"},
    "kept": ["prediction.json"],
    "verdict": {"decision": "keep", "reason": "it is better", "at": "2026-01-01T10:06:00+00:00", "hypotheses": {"H001": "supported"}, "track_head": {"before": "a" * 40, "after": "a" * 40}},
    "reruns": [
        {"n": 1, "at": "2026-01-02T10:00:00+00:00", "split": "confirm", "eval_digest": "e", "data_digest": "dd", "env_changed": {"count": 1, "first": ["python 3.12 -> 3.13"]}, "run_seconds": 1.0, "metrics": {"score": 0.78}, "meets_target": False},
    ],
}


def test_a_record_is_written_back_as_it_was_read():
    rec = record.load(Record, RECORD)
    assert canonical(record.dump(rec)) == canonical(RECORD) and list(record.dump(rec)) == list(RECORD)
    assert rec.attempts[0].segments[0].seconds == 1.5 and rec.attempts[0].ran() == 2.5
    assert (rec.outcome, rec.decision, rec.eval_digest, rec.data_digest) == ("improve", "keep", "e", "dd")
    assert rec.reruns[0].env_changed.count == 1 and rec.rerun is None and rec.pid is None
    assert record.contradiction(rec) == ""


@pytest.mark.parametrize(
    "change, said",
    [
        (lambda r: r.update(staus="judged"), "this arl does not know staus"),
        (lambda r: r["attempts"][0]["segments"][1].update(secs=3), "this arl does not know attempts[0].segments[1].secs"),
        (lambda r: [r.pop("title"), r.pop("branch")], "no title, branch"),
        (lambda r: r.update(staus=r.pop("status")), "no status; this arl does not know staus"),
        (lambda r: r["attempts"][0].pop("n"), "no attempts[0].n"),
        (lambda r: r.update(attempts={}), "attempts should be a list, not {}"),
        (lambda r: r.update(status=3), "status should be text, not 3"),
        (lambda r: r["attempts"][0].update(n=True), "attempts[0].n should be a whole number, not True"),
        (lambda r: r.update(metrics=[0.8]), "metrics should be a table"),
        (lambda r: r.update(reruns={"n": 1}), "reruns should be a list"),
    ],
)
def test_a_record_that_cannot_be_read_says_why(change, said):
    data = record.dump(record.load(Record, RECORD))
    change(data)
    with pytest.raises(ArlError, match=said.replace("[", r"\[").replace("]", r"\]")):
        record.load(Record, data)


@pytest.mark.parametrize(
    "change, said",
    [
        (lambda r: setattr(r, "status", "done"), "its status is 'done'"),
        (lambda r: setattr(r, "verdict", None), "it is judged and has no verdict"),
        (lambda r: setattr(r, "code_commit", None), "it is judged and has no attempt or no code_commit"),
        (lambda r: setattr(r, "conditions", None), "it has run and has no conditions"),
        (lambda r: setattr(r, "design", None), "it has a result and no design"),
        (lambda r: setattr(r, "comparison", None), "it has metrics and no comparison"),
    ],
)
def test_what_a_status_promises(change, said):
    rec = record.load(Record, RECORD)
    change(rec)
    assert record.contradiction(rec).startswith(said)


def test_a_misspelt_field_fails_where_it_is_written():
    rec = record.load(Record, RECORD)
    with pytest.raises(AttributeError):
        rec.staus = "judged"  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="record.dump"):
        canonical({"experiment": rec})  # a record that reaches a file unconverted is refused, not written as text
