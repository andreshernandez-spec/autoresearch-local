"""The introspection step, part by part.

Part 1 (the path) and its gates are in test_flow.py. Here: part 2 leaves
lessons in the instructions agents load, part 3 leaves a revised knowledge
file, part 4 is the reload, and the facts each part starts from.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from arl import cli, runner
from arl import ledger as ledger_module
from conftest import NOTHING_NEW, Project, git


def lesson(p: Project, scope: str, title: str, body: str = "What happened. What to do instead.", *extra: str) -> str:
    return p.arl("lesson", "add", "--scope", scope, "--title", title, "--body", body, *extra)


def test_closing_asks_for_every_part(project: Project):
    p = project
    h = p.started()
    p.tune(0.5, "keep", h, "supported")
    p.arl("introspect", "new", "--trigger", "asked")
    path = p.research / "introspections" / "N001.md"
    text = path.read_text()
    for part in ("# Part 1. The path", "# Part 2. How the work went", "# Part 3. What we know about the problem", "# Part 4. Reload"):
        assert part in text
    assert "is not written yet" in p.arl("introspect", "close", "N001", "--decision", "continue", code=1)
    p.fill(path)

    out = p.arl("introspect", "close", "N001", "--decision", "continue", code=1)
    assert "Part 2 of N001 is not done" in out and "lesson add" in out and "--no-lessons" in out
    lesson(p, "project", "run the evaluation in the background", "It takes minutes. A foreground call times out.", "--from", "N001")

    out = p.arl("introspect", "close", "N001", "--decision", "continue", code=1)
    assert "Part 3 of N001 is not done" in out and "KNOWLEDGE.md" in out and "--knowledge-unchanged" in out
    knowledge = p.research / "KNOWLEDGE.md"
    knowledge.write_text(knowledge.read_text().replace("## Likely\n", "## Likely\n\n- x near 0.7 scores best (E0002).\n"))

    out = p.arl("introspect", "close", "N001", "--decision", "continue")
    assert "N001 closed: continue" in out and "NEXT: reload N001" in out
    meta = p.ledger.introspection("N001")[0]
    assert meta["lessons_added"] == 1 and meta["knowledge_changed"] is True and "reloaded" not in meta

    # part 4: nothing new starts until what changed has been read again
    assert "has not been read again" in p.arl("exp", "new", "--type", "tune", "--track", "main-line", "--title", "t", "--hyp", h, code=1)
    out = p.arl("reload")
    assert "x near 0.7 scores best (E0002)" in out and "L001: run the evaluation in the background" in out
    assert "PROTOCOL.md" in out and "research/tracks/main-line.md" in out
    assert "reloaded" in p.ledger.introspection("N001")[0]
    assert "NEXT: hypothesize" in p.arl("next")
    assert git(p.root, "status", "--porcelain", "--", "research") == ""
    assert "x near 0.7" in git(p.root, "show", "HEAD~1:research/KNOWLEDGE.md")  # the close committed it


def test_a_part_with_nothing_new_says_why(project: Project):
    p = project
    p.started()
    p.arl("introspect", "new", "--trigger", "asked")
    p.fill(p.research / "introspections" / "N001.md")
    assert "--no-lessons is empty" in p.arl("introspect", "close", "N001", "--decision", "continue", "--no-lessons", " ", "--knowledge-unchanged", "x", code=1)
    p.arl("introspect", "close", "N001", "--decision", "continue", "--no-lessons", "one baseline, nothing went wrong", "--knowledge-unchanged", "one baseline says nothing new")
    meta = p.ledger.introspection("N001")[0]
    assert meta["no_lessons"] == "one baseline, nothing went wrong" and meta["knowledge_unchanged"] == "one baseline says nothing new"
    assert meta["lessons_added"] == 0 and meta["knowledge_changed"] is False


def test_lessons_go_into_the_files_agents_load(project: Project):
    p = project
    p.started()
    general = ledger_module.GENERAL_LESSONS
    assert not general.exists()
    out = lesson(p, "general", "never switch git hooks off to get a commit through", "A commit was refused.\n## Not a heading\nCommit the normal way.", "--from", "N001")
    assert f"G001 added to {general}" in out
    lesson(p, "general", "a check handed to another agent writes its findings to a file as it goes")
    assert "L001 added" in lesson(p, "project", "scripts started with python -I add their own folder to sys.path")
    text = general.read_text()
    assert text.startswith("# Lessons: working with arl") and "## G001: never switch git hooks off" in text and "## G002: a check handed" in text
    assert "From: N001." in text and "\\## Not a heading" in text  # typed text cannot start an entry of its own
    assert [l["id"] for l in p.ledger.lessons("general")] == ["G001", "G002"]
    project_file = (p.research / "LESSONS.md").read_text()
    assert project_file.startswith("# Lessons: how to work in this project") and "## L001: scripts started with python -I" in project_file
    assert "L001" in git(p.root, "log", "-1", "--format=%s")  # the project's lessons are part of its ledger

    listing = p.arl("lesson", "list")
    assert "G001" in listing and "G002" in listing and "L001" in listing
    assert "G001" not in p.arl("lesson", "list", "--scope", "project")
    out = p.arl("reload")  # at the start of a session, with no introspection behind it
    assert "## G002: a check handed" in out and "## L001: scripts started" in out
    assert "1 for this project (LESSONS.md), 2 general" in p.arl("status")
    err = io.StringIO()  # a scope that does not exist is refused by the parser, which leaves through SystemExit
    with contextlib.redirect_stderr(err), contextlib.suppress(SystemExit):
        cli.main(["lesson", "add", "--scope", "everyone", "--title", "t", "--body", "b"])
    assert "invalid choice: 'everyone'" in err.getvalue()


def test_recent_activity_is_laid_out_for_parts_two_and_three(project: Project):
    p = project
    h = p.started()
    for title in ("no claim", "still none"):
        assert "tests a hypothesis" in p.arl("exp", "new", "--type", "tune", "--track", "main-line", "--title", title, code=1)
    p.arl("note", "--about", "work", "ran the evaluator in the foreground and it timed out")
    assert "part 3" in p.arl("note", "--about", "problem", "the score is flat", "for x below 0.2")
    crashed = p.draft("tune", hyps=(h,), params={"x": 0.5, "crash": True})
    p.arl("exp", "run", crashed, code=2)
    p.arl("exp", "run", crashed, code=2)
    p.judge(crashed, "crash")

    p.arl("introspect", "new", "--trigger", "asked")
    facts = (p.research / "introspections" / "N001.md").read_text()
    assert "Commands arl refused: 2." in facts
    assert "- 2 x a tune experiment tests a hypothesis" in facts and "`arl exp new --type tune" in facts
    assert "- E0002 attempt 1: the run exited with code 3" in facts and "- E0002 attempt 2:" in facts
    assert "ran the evaluator in the foreground and it timed out" in facts
    assert "the score is flat for x below 0.2" in facts
    assert "Lessons on file: 0 for this project, 0 general." in facts
    p.fill(p.research / "introspections" / "N001.md")
    p.arl("introspect", "close", "N001", "--decision", "continue", *NOTHING_NEW)
    p.arl("reload")

    # the next one starts from what happened since
    assert "no experiment E0999" in p.arl("exp", "show", "E0999", code=1)
    p.arl("introspect", "new", "--trigger", "asked")
    facts = (p.research / "introspections" / "N002.md").read_text()
    assert "Commands arl refused: 1." in facts and "no experiment <id>" in facts
    assert "ran the evaluator in the foreground" not in facts
    assert "Notes about the way of working: none." in facts and "Runs that failed or were cut short: none." in facts


def test_files_changed_by_someone_else_are_read_again(project: Project):
    p = project
    p.started()
    knowledge = p.research / "KNOWLEDGE.md"
    knowledge.write_text(knowledge.read_text() + "\n- A statement the person added by hand.\n")
    assert "NEXT: hypothesize" in p.arl("next")  # never reloaded here: the start of a session asks for it
    p.arl("reload")
    assert "NEXT: hypothesize" in p.arl("next")
    knowledge.write_text(knowledge.read_text().replace("added by hand", "corrected by hand"))
    out = p.arl("next")
    assert "NEXT: reload" in out and "KNOWLEDGE.md changed since the last reload" in out
    assert "corrected by hand" in p.arl("reload")
    assert "NEXT: hypothesize" in p.arl("next")
    lesson(p, "general", "a lesson that another project recorded")
    out = p.arl("next")
    assert "NEXT: reload" in out and "the general lessons changed since the last reload" in out
    assert "Next: reload (the general lessons changed" in p.arl("status")
    p.arl("reload")
    assert "NEXT: hypothesize" in p.arl("next")


def test_a_busy_machine_is_not_a_slip(project: Project, monkeypatch):
    p = project
    h = p.started()
    eid = p.draft("tune", hyps=(h,), params={"x": 0.5})
    monkeypatch.setattr(runner, "busy", lambda cfg: ["load average 30.0 is above 10"])
    assert "The machine is busy" in p.arl("exp", "run", eid, code=75)
    assert [e for e in p.ledger.activity() if e["kind"] == "refused"] == []


def test_knowledge_and_lessons_of_a_project_set_up_before_they_existed(project: Project):
    p = project
    h = p.started()
    for name in ("KNOWLEDGE.md", "LESSONS.md"):
        (p.research / name).unlink()
    p.arl("status")  # reading does not create them
    assert not (p.research / "KNOWLEDGE.md").exists()
    p.arl("hyp", "new", "--title", "any command that writes")
    assert (p.research / "KNOWLEDGE.md").read_text().startswith("# What we know about the problem")
    assert (p.research / "LESSONS.md").exists() and h
    assert p.arl("verify").strip() == "ok"


def test_note_is_one_line_in_a_project(project: Project):
    p = project
    assert "Run 'arl init'" in p.arl("note", "--about", "work", "too early", code=1)
    p.setup()
    p.arl("note", "--about", "work", "two\nlines", "and  spaces")
    events = p.ledger.activity()
    assert events[-1] == {"at": events[-1]["at"], "kind": "note", "text": "two lines and spaces", "about": "work"}


def test_a_lesson_for_every_project_is_committed_nowhere(project: Project, monkeypatch):
    """The arl repository is public and the projects are not: what a loop writes stays out of it."""
    p = project
    p.started()
    checkout = p.base / "arl"  # stands for the arl checkout, a git repository of its own
    (checkout / "skill").mkdir(parents=True)
    git(checkout, "init", "-q", "-b", "main")
    git(checkout, "config", "user.email", "test@example.org")
    git(checkout, "config", "user.name", "Test")
    (checkout / "README.md").write_text("the harness\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "harness")
    monkeypatch.setattr(ledger_module, "GENERAL_LESSONS", checkout / "skill" / "LESSONS.md")
    before = git(checkout, "rev-parse", "HEAD")
    lesson(p, "general", "what the loop learned in a private project")
    assert "what the loop learned" in (checkout / "skill" / "LESSONS.md").read_text()
    assert git(checkout, "rev-parse", "HEAD") == before and git(checkout, "rev-list", "--count", "HEAD") == "1"
    assert git(checkout, "ls-files") == "README.md"  # written, and not made part of the repository
    assert "what the loop learned" not in git(p.root, "log", "-p", "--all")  # nor of the project's history


def test_the_arl_repository_does_not_track_what_loops_write():
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        pytest.skip("not a git checkout")
    assert "skill/LESSONS.md" not in git(root, "ls-files", "skill").splitlines()
    assert git(root, "check-ignore", "skill/LESSONS.md") == "skill/LESSONS.md"
