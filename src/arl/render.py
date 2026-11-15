"""Text the harness writes: STATUS.md, the facts in an introspection or an analysis,
what a reload prints, and the briefs."""

from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import Any

from . import contract, rules, runner
from .ledger import Ledger
from .record import Record, dump, need
from .util import ArlError, fmt, now, read_text

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "bin" / "arl"
TEMPLATES = Path(__file__).parent / "templates"


def template(name: str) -> str:
    return read_text(TEMPLATES / name)


def fill(text: str, **values: Any) -> str:
    """Replace {key} for the given keys only, so other braces survive."""
    if not values:
        return text
    pattern = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    return pattern.sub(lambda m: str(values[m.group(1)]), text)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "none\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


# --- experiments and tracks as tables ---------------------------------------------


def _primary(ledger: Ledger, rec: Record | None) -> str:
    name = ledger.cfg.primary.name
    metrics = (rec.metrics if rec else None) or {}
    return fmt(metrics[name]) if name in metrics else ""


def _gain(rec: Record) -> str:
    gain = rec.comparison.gain if rec.comparison else None
    return f"{gain:+.6g}" if gain is not None else ""


def _verdict(rec: Record) -> str:
    return need(rec.verdict).decision if rec.status == "judged" else f"({rec.status})"


def exp_table(ledger: Ledger, exps: list[Record]) -> str:
    rows = [
        [
            e.id,
            e.track,
            e.type,
            " ".join(e.hypotheses),
            _primary(ledger, e),
            _gain(e),
            e.outcome or "",
            _verdict(e),
            e.title,
        ]
        for e in exps
    ]
    head = ["id", "track", "type", "tests", ledger.cfg.primary.name, "gain", "outcome", "verdict", "title"]
    return table(head, rows)


def track_table(ledger: Ledger, lock: dict[str, Any] | None) -> str:
    exps = ledger.experiments()
    rows = []
    for t in ledger.tracks():
        mine = [e for e in exps if e.track == t.name]
        ref = rules.reference_for(ledger, t, lock["eval_digest"]) if lock else None
        scored = [e for e in mine if e.status == "judged" and e.type in ("tune", "feature")]
        run = 0
        for e in sorted(scored, key=lambda e: (need(e.verdict).at, e.id)):
            run = 0 if e.decision == "keep" else run + 1
        rows.append(
            [
                t.name,
                t.status,
                ref.id if ref else "none",
                _primary(ledger, ref),
                len(mine),
                sum(rules.kept(e) for e in mine),
                run,
                t.head_commit[:10],
            ]
        )
    head = ["track", "status", "reference", ledger.cfg.primary.name, "experiments", "kept", "in a row without a keep", "head"]
    return table(head, rows)


def _budget_line(ledger: Ledger) -> str:
    b = rules.budget(ledger)
    return (
        f"{b['experiments']} of {b['max_experiments'] if b['max_experiments'] is not None else 'unlimited'} experiments, "
        f"{b['hours']} of {b['max_run_hours'] if b['max_run_hours'] is not None else 'unlimited'} run hours"
    )


def _target_line(ledger: Ledger, lock: dict[str, Any] | None, data: str | None) -> str:
    """Where the target stands: who reached it, whether that was confirmed, and who did not hold."""
    primary = ledger.cfg.primary
    if primary.target is None or lock is None:
        return ""
    goal = f"{primary.name} {'>=' if primary.goal == 'max' else '<='} {fmt(float(primary.target))}"
    hit = rules.target_claim(ledger, lock, data)
    fell = [rules.claim_text(c) for c in rules.claims(ledger, lock) if c["state"] in ("missed", "given up")]
    return f"{goal}: {rules.claim_text(hit) if hit else 'not reached'}." + (f" Not confirmed: {'; '.join(fell)}." if fell else "")


def summary(ledger: Ledger, rec: Record) -> str:
    cfg = ledger.cfg
    lines = [f"{rec.id} {rec.title} [{rec.type} on {rec.track}, {rec.status}]"]
    if rec.metrics is not None:
        comp = need(rec.comparison)
        for m in cfg.metrics:
            if m.name in rec.metrics:  # a metric added to the contract later is not in older records
                lines.append(f"  {m.name:28s} {fmt(rec.metrics[m.name])}" + ("  (primary)" if m.role == "primary" else ""))
        if comp.reference:
            lines.append(
                f"  against {comp.reference}: {comp.metric} {fmt(comp.reference_value)} -> {fmt(comp.value)}, "
                f"gain {comp.gain:+.6g} (at least {fmt(comp.min_delta)} counts)"
            )
        for g in comp.gate_failures:
            lines.append(f"  constraint not met: {g}")
        lines.append(f"  outcome: {comp.outcome}")
    if rec.train_metrics:
        lines.append("  reported by the run itself (not scored): " + ", ".join(f"{k} {fmt(v)}" for k, v in list(rec.train_metrics.items())[:12]))
    if rec.status in ("judged", "judging", "abandoned"):
        v = need(rec.verdict)
        lines.append(f"  verdict: {v.decision}. {v.reason}")
    return "\n".join(lines)


# --- STATUS.md --------------------------------------------------------------------


def status(ledger: Ledger) -> str:
    cfg = ledger.cfg
    lock = contract.load(ledger)
    out = ["# Status", "", "Written by arl after every change. Do not edit.", ""]
    if lock:
        who = "" if lock.get("interactive") else " (frozen without a terminal, not yet confirmed by a person)"
        out.append(f"- Contract v{lock['version']}, frozen {lock['frozen_at'][:10]}{who}.")
        bad = contract.problems(ledger)
        if bad:
            out.append(f"- CONTRACT DOES NOT MATCH THE FILES: {'; '.join(bad)}")
    else:
        out.append("- Contract: not frozen.")
    out.append(f"- Budget: {_budget_line(ledger)}.")
    state = rules.introspection_state(ledger)
    last = f"{state['last']} decided '{state['last_decision']}'" if state["last"] else "none so far"
    due = "DUE: " + "; ".join(state["reasons"]) if state["due"] else f"{state['judged_since']} of {cfg.intro_every} experiments judged since"
    out.append(f"- Introspections: {last}. {due}.")
    data = runner.data_now(ledger)
    goal = _target_line(ledger, lock, data)
    if goal:
        out.append(f"- Target: {goal}")
    action = rules.next_action(ledger, data)
    target = f" {action['target']}" if action.get("target") else ""
    out.append(f"- Next: {action['stage']}{target} ({action['why']}).")

    out += ["", "## Tracks", "", track_table(ledger, lock)]
    exps = ledger.experiments()
    flying = [e for e in exps if e.status in rules.IN_FLIGHT]
    out += ["## In flight", "", table(["id", "track", "type", "status", "title"], [[e.id, e.track, e.type, e.status, e.title] for e in flying])]
    latest = list(reversed(exps))[:20]
    out += [f"## Experiments (latest {len(latest)} of {len(exps)})", "", exp_table(ledger, latest)]

    tested: dict[str, list[str]] = {}
    for e in exps:
        for h in e.hypotheses:
            tested.setdefault(h, []).append(e.id)
    hyps = ledger.hypotheses()
    rows = [[h.id, h.status, h.title, " ".join(tested.get(h.id, []))] for h in hyps]
    out += ["## Hypotheses", "", table(["id", "status", "title", "tested by"], rows)]

    ideas = ledger.ideas()
    counts = ", ".join(f"{sum(i['status'] == s for i in ideas)} {s}" for s in ("open", "promoted", "parked", "rejected"))
    out += ["## Ideas", "", f"{counts}. Full text in IDEAS.md.", ""]
    out += [f"- {i['id']} {i['title']}" for i in ideas if i["status"] == "open"]
    lessons = f"{len(ledger.lessons('project'))} for this project (LESSONS.md), {len(ledger.lessons('general'))} general ({ledger.lessons_file('general')})"
    out += ["", "## Knowledge and lessons", "", f"What the work has shown about the problem: KNOWLEDGE.md. Lessons about the way of working: {lessons}."]
    return "\n".join(out).rstrip("\n") + "\n"


def state(ledger: Ledger) -> dict[str, Any]:
    """The same picture for a program."""
    lock = contract.load(ledger)
    return {
        "home": str(ledger.home),
        "contract": {"version": lock["version"], "problems": contract.problems(ledger)} if lock else None,
        "paused": ledger.paused(),
        "budget": rules.budget(ledger),
        "introspection": rules.introspection_state(ledger),
        "next": rules.next_action(ledger, runner.data_now(ledger)),
        "tracks": [dump(t) for t in ledger.tracks()],
        "experiments": [
            {k: e.get(k) for k in ("id", "title", "type", "track", "status", "hypotheses", "metrics", "comparison", "verdict")}
            for e in map(dump, ledger.experiments())
        ],
        "hypotheses": [dump(h) for h in ledger.hypotheses()],
        "ideas": ledger.ideas(),
        "lessons": {scope: ledger.lessons(scope) for scope in ("general", "project")},
    }


# --- facts written into records ---------------------------------------------------


def _took(rec: Record) -> str:
    attempt = rec.attempts[-1]
    took = f"Run {(attempt.run_seconds or 0):.0f} s"
    parts = len(attempt.segments)
    if parts > 1:
        took += f" in {parts} parts (paused and continued)"
    if attempt.eval_seconds is not None:
        took += f", evaluation {attempt.eval_seconds:.0f} s"
    return took


def _env_line(rec: Record) -> str:
    """The environment a run had, when the contract named one."""
    env = rec.env or {}
    if not env.get("python"):
        return ""
    return f" Environment: {env.get('prefix') or 'the python on the PATH'}, python {env['python']}."


def _reported(rec: Record) -> list[str]:
    """What the run says about itself in train_metrics.json."""
    said = rec.train_metrics or {}
    return ["Reported by the run itself (not scored): " + ", ".join(f"{k} {fmt(v)}" for k, v in list(said.items())[:20]) + "."] if said else []


def _env_facts(ledger: Ledger, rec: Record, ref: Record | None) -> list[str]:
    """Where the environment of a run is not the one it is compared with. A changed
    package can explain a changed score, so the analysis starts from knowing it."""
    out = []
    env = rec.env
    primary = ledger.cfg.primary.name
    try:
        if ref is not None and ref.id != rec.id:
            differs = runner.env_differences(ledger, ref.env, env)
            if differs:
                out.append(
                    f"The environment is not the one the reference {ref.id} ran in ({runner.differences(len(differs))}): {runner.short(differs)}. "
                    f"A difference in the scores may come from there. All of them: `arl exp env {ref.id} {rec.id}`."
                )
        elif rec.outcome == "reference":
            last = ledger.track(rec.track).head_experiment
            if last and last != rec.id:
                before = ledger.record(last)
                differs = runner.env_differences(ledger, before.env, env)
                value = f" ({primary} {fmt(before.metrics[primary])})" if before.metrics and primary in before.metrics else ""
                out.append(
                    f"The track was last measured by {last}{value}, which no longer counts as its reference. "
                    + (f"The environment since then ({runner.differences(len(differs))}): {runner.short(differs)}." if differs else "The environment is the same, as far as it was recorded.")
                )
        parts = rec.attempts[-1].segments
        if len(parts) > 1:
            between = runner.env_differences(ledger, parts[0].env, parts[-1].env)
            if between:
                out.append(f"The environment changed between the parts of this run ({runner.differences(len(between))}): {runner.short(between)}.")
    except (ArlError, KeyError, OSError, TypeError, ValueError) as e:
        out.append(f"The environments could not be compared ({e}).")
    return out


def analysis_facts(ledger: Ledger, rec: Record) -> str:
    cfg = ledger.cfg
    where = f"Code {need(rec.code_commit)[:10]} on `{rec.branch}`, contract v{need(rec.contract).version}.{_env_line(rec)}"
    took = _took(rec)
    if rec.type not in rules.SCORED:
        kept = ", ".join(f"kept/{k}" for k in (rec.kept or [])) or "none"
        extra = "".join(f"\n{line}" for line in _reported(rec) + _env_facts(ledger, rec, None))
        return f"Written by arl. Not scored. {took}. {where}\nSmall result files: {kept}. Everything else is under artifacts/.{extra}"
    comp, metrics = need(rec.comparison), need(rec.metrics)
    ref = ledger.record(comp.reference) if comp.reference else None
    head = ["metric", "role", "value"] + ([f"reference {ref.id}", "gain"] if ref else [])
    rows = []
    for m in cfg.metrics:
        if m.name not in metrics:  # added to the contract after this run
            continue
        row = [m.name, m.role, fmt(metrics[m.name])]
        if ref:
            before = (ref.metrics or {}).get(m.name)
            row += [fmt(before) if before is not None else "", f"{m.gain(metrics[m.name], before):+.6g}" if before is not None else ""]
        rows.append(row)
    lines = [f"Written by arl. Outcome: **{comp.outcome}**.", "", table(head, rows)]
    if comp.outcome == "repeat":
        lines.append("A repeat of the reference: it shows the noise of the measurement and does not replace the reference.")
    elif ref:
        lines.append(f"A change of at least {fmt(comp.min_delta)} on {comp.metric} counts as better or worse.")
    if comp.gate_failures:
        lines.append("Constraints not met: " + "; ".join(comp.gate_failures) + ".")
    lines += _reported(rec) + _env_facts(ledger, rec, ref)
    lines.append(f"{took}. {where}")
    return "\n".join(lines)


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}.*?\n(.*?)(?=^#+ |\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def _activity_facts(ledger: Ledger, events: list[dict[str, Any]], window: list[Record]) -> list[str]:
    """Part 2 and part 3 start from this: what was refused, what failed, what was noted."""
    out = ["How the work went in this window (for part 2):", ""]
    refused = collections.Counter()
    example: dict[str, str] = {}
    for ev in events:
        if ev.get("kind") == "refused":
            first = str(ev.get("text", "")).splitlines()[0][:150] if ev.get("text") else "no message"
            key = re.sub(r"(/[\w.\-]+)+", "<path>", re.sub(r"\b[EHNI]\d{3,4}\b", "<id>", first))
            refused[key] += 1
            example.setdefault(key, str(ev.get("command", ""))[:100])
    if refused:
        out.append(f"Commands arl refused: {sum(refused.values())}.")
        out += [f"- {count} x {key}  (for example: `arl {example[key]}`)" for key, count in refused.most_common(15)]
    else:
        out.append("Commands arl refused: none.")
    failures = []
    for e in window:
        for a in e.attempts:
            if a.failed:
                failures.append(f"- {e.id} attempt {a.n}: {str(a.failed).splitlines()[0][:200]}")
            for seg in a.segments:
                if seg.ended_by not in (None, "finished", "failed"):
                    failures.append(f"- {e.id} attempt {a.n}: a part of the run ended early ({seg.ended_by})")
    out += ["", "Runs that failed or were cut short:" if failures else "Runs that failed or were cut short: none."] + failures
    for about, label in (("work", "Notes about the way of working"), ("problem", "Notes about the problem (for part 3)")):
        notes = [ev for ev in events if ev.get("kind") == "note" and ev.get("about") == about]
        out += ["", f"{label}:" if notes else f"{label}: none."]
        out += [f"- {str(ev.get('at', ''))[:10]}: {ev.get('text', '')}" for ev in notes]
    lessons = f"{len(ledger.lessons('project'))} for this project, {len(ledger.lessons('general'))} general"
    out += ["", f"Lessons on file: {lessons}."]
    return out


def _env_window_facts(ledger: Ledger, window: list[Record]) -> list[str]:
    """Where the environment changed from one run to the next, for the runs of this window."""
    try:
        ran = [e for e in ledger.experiments() if e.env and e.attempts]
        ran.sort(key=lambda e: (e.attempts[0].started, e.id))
        inside, out = {e.id for e in window}, []
        for before, after in zip(ran, ran[1:]):
            if after.id in inside:
                differs = runner.env_differences(ledger, before.env, after.env)
                if differs:
                    out.append(f"- between {before.id} and {after.id} ({runner.differences(len(differs))}): {runner.short(differs, 5)}")
    except (ArlError, KeyError, OSError, TypeError, ValueError) as e:
        return [f"Environment: the runs could not be compared ({e})."]
    if not out:
        return ["Environment: the same for every run of this window, as far as it was recorded."]
    return ["The environment changed between runs (a possible reason for a changed score; `arl exp env <id> <other>` lists everything):", ""] + out


def introspection_facts(ledger: Ledger, trigger: str, state_: dict[str, Any], activity_from: int) -> str:
    lock = contract.load(ledger)
    by_id = {e.id: e for e in ledger.experiments()}
    window = [by_id[i] for i in state_["window"]]
    out = ["## Facts", "", f"Written by arl on {now()[:10]}. Trigger: {trigger}.", ""]
    out.append(f"Window: {len(window)} experiments judged since {state_['last'] or 'the start'}.")
    out += ["", exp_table(ledger, window), "Tracks:", "", track_table(ledger, lock)]
    out.append(f"Budget: {_budget_line(ledger)}.")
    goal = _target_line(ledger, lock, runner.data_now(ledger))
    if goal:
        out.append(f"Target: {goal}")
    hyps = [h for h in ledger.hypotheses() if h.status == "open"]
    out.append("Open hypotheses: " + ("; ".join(f"{h.id} {h.title}" for h in hyps) or "none") + ".")
    ideas = [i for i in ledger.ideas() if i["status"] == "open"]
    out.append("Open ideas: " + ("; ".join(f"{i['id']} {i['title']}" for i in ideas) or "none") + ".")
    first = ledger.cfg.splits[0]
    others = sum(1 for e in by_id.values() for r in e.reruns if r.split != first)
    if len(ledger.cfg.splits) > 1:
        out.append(f"Runs on a confirmation split so far: {others}.")
    out += [""] + _env_window_facts(ledger, window)
    if state_["last"]:
        tripwires = _section(ledger.introspection(state_["last"])[1], "6. Tripwires")
        out += ["", f"Tripwires set by {state_['last']}:", "", tripwires or "none written"]
    out += [""] + _activity_facts(ledger, ledger.activity()[activity_from:], window)
    return "\n".join(out)


# --- what a reload prints ---------------------------------------------------------


def reload_text(ledger: Ledger, handoff: dict[str, Any] | None = None, env: list[str] | None = None) -> str:
    """The knowledge and the lessons as they are now, in full, with the notes left at a pause.

    env: lines about the environment runs use, when the contract names one.
    """
    out = []
    if handoff and (handoff.get("note") or handoff.get("reason")):
        out += [
            "==> Left by whoever paused the loop" + (f" ({str(handoff.get('at', ''))[:16]})" if handoff.get("at") else ""),
            "",
            f"Reason: {handoff.get('reason') or 'not given'}",
            f"Where things were: {handoff.get('note') or 'no note was left'}",
            "",
        ]
    for path, label in (
        (ledger.knowledge_path, "what the work has shown about the problem"),
        (ledger.lessons_path, "how to work in this project"),
        (ledger.lessons_file("general"), "how to work with arl, in any project"),
    ):
        out += [f"==> {path}  ({label})", "", read_text(path).rstrip("\n") if path.exists() else "(not written yet)", ""]
    tracks = ", ".join(f"{ledger.name}/tracks/{t.name}.md" for t in ledger.tracks() if t.status == "active") or "none yet"
    out += env or []
    out += [
        "==> Also part of your instructions. Read them again if this session has not, or has been cut short since:",
        f"  {ROOT / 'PROTOCOL.md'}",
        f"  {ledger.program_path}",
        f"  {ledger.status_path}",
        f"  the active tracks: {tracks}",
        f"  {ledger.ideas_path}",
    ]
    return "\n".join(out) + "\n"


# --- briefs -----------------------------------------------------------------------

_CHANGE = {
    "baseline": "Change nothing: a baseline measures the track head as it is.",
    "tune": "Edit {exp}/params.json. Params only: the code stays the track head.",
    "feature": (
        "Change the code in {worktree} (it is on branch {branch}), then commit with\n"
        "   `{arl} exp commit {target} -m \"...\"`. Leave {research}/ alone in that worktree."
    ),
    "diagnose": (
        "Add the instrumentation in {worktree} (it is on branch {branch}) and commit with\n"
        "   `{arl} exp commit {target} -m \"...\"`. It must observe, not change behaviour."
    ),
}


def next_text(ledger: Ledger, action: dict[str, Any]) -> str:
    stage = action["stage"]
    ctx: dict[str, Any] = {
        "arl": LAUNCHER,
        "root": ROOT,
        "home": ledger.home,
        "research": ledger.name,
        "target": action.get("target", ""),
        "why": action.get("why", ""),
    }
    name = stage
    if stage in ("judge", "design", "crashed", "outdated", "interrupted", "resume", "unfinished", "running"):
        rec = ledger.record(action["target"])
        ctx["track"] = rec.track
        ctx["exp"] = ledger.exp_dir(rec.id)
        ctx["worktree"] = ledger.worktree_path(rec.track)
        ctx["branch"] = rec.branch
        ctx["change_step"] = fill(_CHANGE[rec.type], **ctx)
        ctx["outcome"] = rec.outcome or "not scored"
        ctx["allowed"] = ", ".join(rules.allowed_verdicts(rec))
        flags = ""
        if rec.type in ("tune", "feature"):
            flags = "".join(f" \\\n       --hyp {h}=<supported|refuted|inconclusive|open>" for h in rec.hypotheses)
        ctx["hyp_flags"] = flags
        if stage == "crashed" and rec.attempts:
            ctx["why"] = f"{ctx['why']}. Last failure: {(rec.attempts[-1].failed or 'not recorded')}"
        if stage == "judge" and (rec.type == "diagnose" or ctx["outcome"] == "repeat"):
            name = "judge_diagnose"
    if stage == "confirm":
        split, first = action.get("split", ""), ledger.cfg.splits[0]
        again = split == first  # the contract has one split: the same run, made once more
        ctx.update(
            split=split,
            first=first,
            max_attempts=ledger.cfg.max_attempts,
            where="when it is run once more" if again else f"on '{split}', where the loop never looked",
        )
    head = f"NEXT: {stage}" + (f" {action['target']}" if action.get("target") else "")
    return f"{head}\nWhy: {action.get('why', '')}\n\n" + fill(template(f"briefs/{name}.md"), **ctx)
