"""The parts that can be checked without a project."""

from __future__ import annotations

import pytest

from arl import config, rules, runner
from arl.gitops import Git
from arl.util import ArlError, join_front, split_front, unfilled

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
    return {"id": "E0001", "metrics": {"score": score, "seconds": seconds}, "comparison": {"gate_failures": list(fails)}}


def test_compare(cfg):
    def outcome(score, seconds, reference):
        return rules.compare(cfg, {"score": score, "seconds": seconds}, reference)["outcome"]

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
    assert rules.compare(cfg, {"score": 0.4, "seconds": 1.0}, ref(0.5, 1.0))["outcome"] == "improve"
    assert rules.compare(cfg, {"score": 0.6, "seconds": 1.0}, ref(0.5, 1.0))["outcome"] == "worse"


@pytest.mark.parametrize(
    "broken, message",
    [
        (TOML.replace('role = "primary"', 'role = "info"'), "exactly one"),
        (TOML.replace("limit = 10.0\nmax_regression = 2.0", ""), "needs 'limit' or 'max_regression'"),
        (TOML.replace('command = "eval"', ""), "needs 'command'"),
        (TOML + '[review]\nagent_may = ["continue", "give up"]\n', "unknown decisions"),
    ],
)
def test_bad_config(tmp_path, broken, message):
    path = tmp_path / "arl.toml"
    path.write_text(broken)
    with pytest.raises(ArlError, match=message):
        config.load(path)


def test_metrics_must_be_finite(cfg, tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text('{"score": 0.5, "seconds": 1, "note": "text", "extra": 3}')
    assert runner.read_metrics(path, cfg) == {"score": 0.5, "seconds": 1.0, "extra": 3.0}
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
