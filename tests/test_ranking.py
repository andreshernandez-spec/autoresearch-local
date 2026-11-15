"""Ordered objectives: exact ties, verdicts, and reproduction of every ranking key."""

from __future__ import annotations

import pytest

from arl import config, record, rules, runner
from arl.record import Comparison, Rerun
from arl.util import ArlError
from conftest import EVAL_PY, Project, env_table, make_env
from test_units import TOML, ref


SECONDARY = '\n[[metric]]\nname = "quality"\ngoal = "max"\nrole = "secondary"\n'


@pytest.fixture
def ranked(tmp_path):
    path = tmp_path / "arl.toml"
    path.write_text(SECONDARY + TOML)
    cfg = config.load(path)
    cfg.metric("quality").min_delta = 0.05
    reference = ref(0.5, 1.0)
    reference.metrics["quality"] = 0.4
    return cfg, reference


@pytest.mark.parametrize(
    "score, quality, outcome, metric",
    [
        (0.5, 0.6, "improve", "quality"),
        (0.5, 0.2, "worse", "quality"),
        (0.52, 0.2, "improve", "score"),
        (0.48, 0.9, "worse", "score"),
        (0.505, 0.9, "neutral", "score"),
        (0.495, 0.9, "neutral", "score"),
        (0.5, 0.42, "neutral", "quality"),
        (0.5, 0.4, "neutral", "score"),
    ],
)
def test_secondary_breaks_only_an_exact_tie(ranked, score, quality, outcome, metric):
    cfg, reference = ranked
    assert [m.name for m in cfg.ranking] == ["score", "quality"]
    comparison = rules.compare(cfg, {"score": score, "quality": quality, "seconds": 1.0}, reference)
    assert (comparison.outcome, comparison.metric) == (outcome, metric)
    assert comparison.value == (score if metric == "score" else quality)
    assert comparison.ranking == ["score", "quality"]
    assert record.load(Comparison, record.dump(comparison)) == comparison


def test_secondary_order_thresholds_and_minimization(ranked):
    cfg, reference = ranked
    cfg.metrics.append(config.Metric("size", "min", "secondary"))
    reference.metrics["size"] = 10.0
    values = {"score": 0.5, "quality": 0.4, "size": 9.0, "seconds": 1.0}
    comparison = rules.compare(cfg, values, reference)
    assert (comparison.outcome, comparison.metric) == ("improve", "size")
    values["quality"] = 0.42
    assert rules.compare(cfg, values, reference).outcome == "neutral"
    values["quality"] = 0.2
    assert rules.compare(cfg, values, reference).outcome == "worse"
    cfg.primary.goal = "min"
    values["score"] = 0.48
    assert rules.compare(cfg, values, reference).outcome == "improve"


@pytest.mark.parametrize("metric", ["score", "quality"])
@pytest.mark.parametrize("before, after, expected", [(0.11, 0.12, "improve"), (0.12, 0.11, "worse"), (0.11, 0.119, "neutral")])
def test_decimal_change_at_the_evidence_threshold(ranked, metric, before, after, expected):
    cfg, reference = ranked
    cfg.metric(metric).min_delta = 0.01
    reference.metrics[metric] = before
    values = dict(reference.metrics)
    values[metric] = after
    assert rules.compare(cfg, values, reference).outcome == expected


def test_constraints_repeats_and_targets_still_use_their_own_rules(ranked):
    cfg, reference = ranked
    cfg.primary.target = 0.5
    values = {"score": 0.5, "quality": 0.6, "seconds": 11.0}
    comparison = rules.compare(cfg, values, reference)
    assert comparison.outcome == "gate_fail" and not comparison.target_met
    values["seconds"] = 1.0
    assert rules.compare(cfg, values, reference).target_met
    comparison = rules.compare(cfg, values, reference, repeat=True)
    assert comparison.outcome == "repeat" and not comparison.target_met
    assert rules.compare(cfg, values, None).outcome == "reference"


def test_the_evaluator_must_report_secondary_metrics(ranked, tmp_path):
    cfg, _ = ranked
    path = tmp_path / "metrics.json"
    path.write_text('{"score": 0.5, "seconds": 1}')
    with pytest.raises(ArlError, match="did not report 'quality'"):
        runner.read_metrics(path, cfg)
    path.write_text('{"score": 0.5, "seconds": 1, "quality": NaN}')
    with pytest.raises(ArlError, match="quality = nan"):
        runner.read_metrics(path, cfg)


def test_tied_primary_better_secondary_is_kept_and_reproduced(project: Project, monkeypatch):
    p = project
    env = make_env(p.base / "env")
    evaluator = EVAL_PY.replace("import argparse, json, pathlib", "import argparse, json, pathlib, os")
    evaluator = evaluator.replace(
        '"score": 1 - abs(x - target)',
        '"score": round(1 - abs(x - target), 1), "quality": x + float(os.environ.get("TEST_QUALITY_SHIFT", "0"))',
    )
    p.setup(evaluator=evaluator, python="python", extra=env_table(env) + SECONDARY)
    p.track()
    p.baseline()
    h = p.hyp()

    p.arl("exp", "new", "--type", "tune", "--track", "main-line", "--title", "better quality", "--hyp", h)
    eid = "E0002"
    design = p.ledger.exp_dir(eid) / "design.md"
    assert "ranking metric (score, then quality)" in design.read_text()
    p.fill(design)
    (p.ledger.exp_dir(eid) / "params.json").write_text('{"x": 0.04}')
    out = p.arl("exp", "run", eid)
    assert "ranking: score, then quality" in out
    assert "against E0001: quality 0 -> 0.04" in out
    assert p.record(eid)["comparison"]["outcome"] == "improve"
    assert "Ranking: score, then quality" in (p.ledger.exp_dir(eid) / "analysis.md").read_text()
    p.judge(eid, "keep", "--hyp", f"{h}=supported")  # no --simpler needed
    status = p.arl("status")
    assert "| score | quality |" in status and "+0.04 (quality)" in status
    assert p.ledger.track("main-line").head_experiment == eid

    worse = p.draft("tune", hyps=(h,), params={"x": 0.02})
    p.arl("exp", "run", worse)
    assert "the verdict can be discard" in p.judge(worse, "keep", "--hyp", f"{h}=open", code=1)
    p.judge(worse, "discard", "--hyp", f"{h}=open")

    out = p.arl("exp", "reproduce", eid)
    assert "reproduced: True" in out
    assert p.record(eid)["reruns"][-1]["differences"] == {"score": 0.0, "quality": 0.0}
    # Simulate a nondeterministic secondary result without changing the evaluator or inputs.
    monkeypatch.setenv("TEST_QUALITY_SHIFT", "0.1")
    out = p.arl("exp", "reproduce", eid)
    rerun = p.record(eid)["reruns"][-1]
    assert rerun["difference"] == 0 and not rerun["reproduced"]
    assert rerun["differences"]["quality"] == pytest.approx(0.1)
    assert "quality 0.1, reproduced: False" in out
    assert p.arl("verify").strip() == "ok"
    assert record.load(Rerun, rerun).differences == rerun["differences"]
