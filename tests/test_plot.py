"""`arl plot`: one metric against the experiment number, in the terminal and as an image."""

from __future__ import annotations

from arl import plot
from arl.commands import common
from conftest import Project


def rows(text: str) -> list[str]:
    """The lines of the chart between the title and the axis."""
    lines = text.splitlines()
    axis = next(i for i, line in enumerate(lines) if line.lstrip().startswith("+--"))
    return lines[1:axis]


def test_the_primary_metric_by_experiment(project: Project):
    p = project
    h = p.started()  # E0001: the baseline at x = 0, score 0.3
    better = p.tune(0.5, "keep", h, "supported")  # score 0.8
    worse = p.tune(0.2, "discard", h)  # score 0.5
    out = p.arl("plot", "--width", "70")
    chart = rows(out)

    assert out.splitlines()[0] == "score by experiment, higher is better"
    assert sum(line.count("*") for line in chart) == 2 and sum(line.count("o") for line in chart) == 1
    assert chart[0].split("|")[0].strip() == "0.8" and "*--" in chart[0] and chart[0].endswith("-")  # the best one, and its line to the right edge
    assert chart[-1].split("|")[0].strip() == "0.3" and chart[-1].split("|")[1].startswith("*--")
    assert "o" in chart[len(chart) - 1 - round(0.2 / 0.5 * (len(chart) - 1))]  # 0.5 lies 0.2 above the lowest of a span of 0.5
    numbers = out.splitlines()[len(chart) + 2].split()
    assert numbers == ["1", "2", "3"]
    assert "* kept   o not kept   - the best kept so far" in out
    assert f"best kept {better} 0.8, latest {worse} 0.5" in out
    assert all(len(line) <= 70 for line in out.splitlines())
    assert p.arl("verify").strip() == "ok"  # it changed nothing


def test_a_metric_where_lower_is_better_and_one_nobody_declared(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h, "supported")
    out = p.arl("plot", "cost")
    assert out.splitlines()[0] == "cost by experiment, lower is better"
    assert "best kept E0001 0," in out  # the baseline costs nothing: the second kept one is no step

    assert "no experiment has a value called nothing. Known are the evaluator's: cost, score." in p.arl("plot", "nothing", code=1)
    assert "there is no track nowhere" in p.arl("plot", "--track", "nowhere", code=1)
    assert "score by experiment, higher is better, track main-line" in p.arl("plot", "--track", "main-line")


def test_what_runs_report_about_themselves(project: Project):
    p = project
    h = p.started()
    for x, loss in ((0.5, 0.4), (0.6, 0.25), (0.7, 0.3)):
        eid = p.draft("tune", hyps=(h,), params={"x": x, "report": {"loss": loss, "steps": 40}})
        p.arl("exp", "run", eid)
        p.judge(eid, "keep", "--hyp", f"{h}=open")
    out = p.arl("plot", "loss")
    assert out.splitlines()[0] == "loss by experiment, as the runs report it"
    assert "best kept" not in out and "latest E0004 0.3" in out
    assert "without a value: E0001 (judged)" in out
    assert "--goal max or --goal min draws the best so far" in out
    out = p.arl("plot", "loss", "--goal", "min")
    assert out.splitlines()[0] == "loss by experiment, lower is better, as the runs report it"
    assert "best kept E0003 0.25, latest E0004 0.3" in out
    assert "no experiment has a value called score" in p.arl("plot", "score", "--train", code=1)
    assert "Known are the evaluator's: cost, score; what runs report about themselves: loss, steps." in p.arl("plot", "nothing", code=1)


def test_before_any_result_and_under_an_earlier_evaluator(project: Project, monkeypatch):
    p = project
    p.setup()
    p.track()
    assert p.arl("plot").strip() == "No experiment has a score yet."
    assert "No experiment has a result yet." in p.arl("plot", "nothing", code=1)

    p.baseline()
    evaluator = p.research / "eval" / "evaluate.py"
    evaluator.write_text(evaluator.read_text() + "\n# another way to score\n")
    monkeypatch.setattr(common, "interactive", lambda: True)
    p.arl("freeze", "--reason", "another way to score", "--yes")
    p.baseline()
    out = p.arl("plot")
    assert ". scored under an earlier evaluator" in out and "best kept E0002 0.3, latest E0002 0.3" in out
    drawn = "".join(line.split("|", 1)[1] for line in rows(out))
    assert drawn.count(".") == 1 and drawn.count("*") == 1  # the first one is not compared with the second


def test_the_image(project: Project, tmp_path):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h, "supported")
    p.tune(0.2, "discard", h)
    target = tmp_path / "score.svg"
    out = p.arl("plot", "--svg", target, "--height", "8")
    assert len(rows(out)) == 8 and f"wrote {target}" in out
    image = target.read_text()
    assert image.startswith("<svg ") and image.rstrip().endswith("</svg>")
    assert image.count("<title>") == 3 and "<title>E0002 0.8</title>" in image
    assert "score by experiment, higher is better" in image and ">experiment<" in image
    assert image.count("<path ") == 1  # the best kept so far


def test_the_best_so_far_and_the_labels():
    pts = [plot.Point(n, f"E{n:04d}", v, kind) for n, v, kind in [(1, 0.2, plot.KEPT), (2, 0.9, plot.SCORED), (3, 0.5, plot.KEPT), (4, 0.4, plot.KEPT), (5, 0.6, plot.EARLIER)]]
    assert [s.n for s in plot.best_so_far(pts, "max")] == [1, 3]  # only what was kept, under the evaluator as it is
    assert [s.n for s in plot.best_so_far(pts, "min")] == [1]
    assert plot.best_so_far(pts, None) == []
    assert [plot._every(count, 10) for count in (5, 10, 11, 25, 60, 400)] == [1, 1, 2, 5, 10, 50]
    one = plot.text([plot.Point(7, "E0007", 0.25, plot.KEPT)], [], "one point", width=50, height=6)
    assert one.count("*") == 2 and "7" in one.splitlines()[-2]  # the point and the legend
    many = plot.text([plot.Point(n, f"E{n:04d}", n / 300, plot.SCORED) for n in range(1, 301)], [], "many", width=80)
    labelled = [int(n) for n in many.splitlines()[-2].split()]
    assert all(len(line) <= 80 for line in many.splitlines()) and labelled == sorted(labelled) and 5 <= len(labelled) <= 20
