"""The mechanical decisions: how a result compares, which verdicts it allows,
when an introspection is due, when the budget is spent, and what comes next."""

from __future__ import annotations

import math
from typing import Any

from . import contract
from .config import Config
from .ledger import Ledger
from .runner import alive
from .record import Comparison, Record, Rerun, Track, need
from .util import now, parse_time

SCORED = ("baseline", "tune", "feature")
TYPES = (*SCORED, "diagnose")
# draft: written, not run. running: a job is going. paused: a run was interrupted
# and can be continued. evaluated: has a result. judging: a verdict is being
# applied. crashed: a run failed.
IN_FLIGHT = ("draft", "running", "paused", "evaluated", "crashed", "judging")


# --- comparing a result -------------------------------------------------------------


def gate_failures(cfg: Config, metrics: dict[str, float], reference: dict[str, float] | None) -> list[str]:
    fails = []
    for m in cfg.metrics:
        if m.role != "constraint":
            continue
        value = metrics[m.name]
        if m.limit is not None and not m.meets(value, m.limit):
            sign = ">=" if m.goal == "max" else "<="
            fails.append(f"{m.name} = {value:.6g}, must be {sign} {m.limit:.6g}")
        if m.max_regression is not None and reference is not None and m.name in reference:
            loss = -m.gain(value, reference[m.name])
            if loss > m.max_regression:
                fails.append(
                    f"{m.name} = {value:.6g} is {loss:.6g} worse than the reference "
                    f"({reference[m.name]:.6g}), {m.max_regression:.6g} allowed"
                )
    return fails


def compare(cfg: Config, metrics: dict[str, float], reference: Record | None, repeat: bool = False) -> Comparison:
    """Outcome of a scored run against the track's reference experiment.

    No reference: this run is the reference. A baseline on a track that has one
    is a repeat: it shows the noise and never replaces the reference. Otherwise
    improve, neutral, worse or gate_fail. Secondary metrics break exact ties:
    a nonzero change below min_delta is neutral, not a tie.
    """
    primary = cfg.primary
    value = metrics[primary.name]
    ref_metrics = reference.metrics if reference else None
    fails = gate_failures(cfg, metrics, ref_metrics)
    met = bool(primary.target is not None and not fails and primary.meets(value, primary.target))
    ranking = [m.name for m in cfg.ranking]
    if reference is None:
        return Comparison(metric=primary.name, value=value, gate_failures=fails, target_met=met, outcome="reference", ranking=ranking)
    assert ref_metrics is not None
    compared = primary
    gain = 0.0
    for metric in cfg.ranking:
        change = metric.gain(metrics[metric.name], ref_metrics[metric.name])
        if change != 0:
            compared, gain = metric, change
            break
    ref_failed = bool(reference.comparison and reference.comparison.gate_failures)
    # Subtracting two decimal scores can put an exact threshold a few bits below it.
    enough = abs(gain) >= compared.min_delta or math.isclose(abs(gain), compared.min_delta, rel_tol=1e-12, abs_tol=0.0)
    if repeat:
        outcome, met = "repeat", False
    elif fails:
        outcome = "gate_fail"
    elif ref_failed:
        outcome = "improve"  # inside the constraints beats outside them
    elif gain > 0 and enough:
        outcome = "improve"
    elif gain < 0 and enough:
        outcome = "worse"
    else:
        outcome = "neutral"
    return Comparison(
        metric=compared.name,
        value=metrics[compared.name],
        gate_failures=fails,
        target_met=met,
        reference=reference.id,
        reference_value=ref_metrics[compared.name],
        gain=gain,
        min_delta=compared.min_delta,
        outcome=outcome,
        ranking=ranking,
    )


def allowed_verdicts(rec: Record) -> list[str]:
    """What the agent may decide. The first entry is the default."""
    if rec.status in ("crashed", "paused"):
        return ["crash"]
    if rec.status != "evaluated":
        return []
    if rec.type == "diagnose":
        return ["finding"]
    outcome = rec.outcome
    if outcome == "repeat":
        return ["finding"]  # a measurement of the noise, not a new reference
    if outcome == "reference":
        return ["keep", "discard"]
    if outcome == "improve":
        return ["keep", "discard"]
    if outcome == "neutral":
        return ["discard", "keep"]  # keep only as a simplification, with a reason
    return ["discard"]


# --- tracks -----------------------------------------------------------------------


def reference_for(ledger: Ledger, track: Track, eval_digest: str, data: str | None = None) -> Record | None:
    """The experiment that measured the track head under the current evaluator, on the current data."""
    eid = track.head_experiment
    if not eid:
        return None
    rec = ledger.record(eid)
    if rec.eval_digest != eval_digest:
        return None
    measured_on = rec.data_digest
    if data is not None and measured_on is not None and measured_on != data:
        return None
    return rec


def in_flight(ledger: Ledger, track: str | None = None) -> list[Record]:
    return [
        e
        for e in ledger.experiments()
        if e.status in IN_FLIGHT and (track is None or e.track == track)
    ]


def kept(rec: Record) -> bool:
    return rec.status == "judged" and rec.decision == "keep"


def stale(rec: Record) -> bool:
    """Marked as running, but the arl that ran it is gone."""
    return rec.status == "running" and not alive(rec.pid, rec.pid_start)


# --- budget -----------------------------------------------------------------------


def budget(ledger: Ledger) -> dict[str, Any]:
    """Experiments that ran, and the hours of every run: failed ones, interrupted ones, reruns."""
    cfg = ledger.cfg
    exps = ledger.experiments()
    ran = [e for e in exps if e.attempts]
    runs = [a for e in exps for a in (*e.attempts, *e.reruns)]
    hours = sum((a.run_seconds or 0) + (a.eval_seconds or 0) for a in runs) / 3600
    count_spent = cfg.max_experiments is not None and len(ran) >= cfg.max_experiments
    hours_spent = cfg.max_run_hours is not None and hours >= cfg.max_run_hours
    reasons = []
    if count_spent:
        reasons.append(f"{len(ran)} experiments run, the budget is {cfg.max_experiments}")
    if hours_spent:
        reasons.append(f"{hours:.2f} run hours used, the budget is {cfg.max_run_hours}")
    return {
        "experiments": len(ran),
        "max_experiments": cfg.max_experiments,
        "hours": round(hours, 2),
        "max_run_hours": cfg.max_run_hours,
        "count_spent": count_spent,
        "hours_spent": hours_spent,
        "spent": reasons,
    }


# --- the target -------------------------------------------------------------------
# Reaching the target ends the program, so one result on the split the loop tunes on
# is not enough. It is a claim, and the claim has to hold where the loop never looked.


def meets_target(cfg: Config, metrics: dict[str, float]) -> bool:
    """A result on its own against the target: the primary metric reaches it and every
    absolute limit holds. Limits relative to a reference say nothing on another split."""
    primary = cfg.primary
    try:
        return primary.target is not None and not gate_failures(cfg, metrics, None) and primary.meets(metrics[primary.name], primary.target)
    except KeyError:
        return False  # a metric the contract asks for is not in this result


def rerun_estimate(cfg: Config, rec: Record, split: str) -> dict[str, Any] | None:
    """How long a rerun of an experiment on a split would take, from [eval] split_size: the
    recorded time of the run that gave its result (every part of it, not the evaluation),
    scaled by the number of items. None without sizes, or without a recorded time.

    seconds, limit ([run] timeout_s) and fits; ran and split: what it was scaled from.
    """
    sizes, given = cfg.split_size, rec.conditions
    if not sizes or not rec.attempts or given is None or given.split not in sizes or split not in sizes:
        return None
    attempt = rec.attempts[-1]
    ran = attempt.run_seconds if attempt.run_seconds is not None else attempt.ran()
    if not ran:
        return None
    seconds = ran * sizes[split] / sizes[given.split]
    return {"seconds": seconds, "limit": cfg.run_timeout, "fits": seconds <= cfg.run_timeout, "ran": ran, "split": given.split}


def confirming_splits(cfg: Config) -> list[str]:
    """Where a result that met the target has to hold as well: every split the loop does
    not score on, or the first one again when the contract has no other."""
    return cfg.splits[1:] or cfg.splits[:1]


def claim(ledger: Ledger, rec: Record, lock: dict[str, Any]) -> dict[str, Any] | None:
    """Where a kept result that met the target on the first split stands, or None when
    the experiment makes no such claim under the evaluator as it is.

    state "open": a run on "split" is still to do ('arl exp reproduce --split').
    "confirmed": a run on every confirming split met the target as well.
    "missed": a run on "split" did not. That settles it: a later run that does
    better there does not turn the answer round, or the split would be tuned on.
    "given up": the run on "split" failed as often as a run may, so nothing is
    known. A run that succeeds later still decides.
    "too long": a run on "split" would pass the run limit by [eval] split_size, so
    it is not started, and the claim is set aside as it is for "given up".
    Only reruns under the same evaluator and on the data the experiment read count.
    """
    cfg = ledger.cfg
    if cfg.primary.target is None or not kept(rec) or not (rec.comparison and rec.comparison.target_met):
        return None
    if rec.eval_digest != lock["eval_digest"]:
        return None
    data = rec.data_digest
    out: dict[str, Any] = {"id": rec.id, "first": cfg.splits[0], "splits": confirming_splits(cfg), "state": "confirmed"}
    todo, given_up, too_long = [], [], []
    for split in out["splits"]:
        mine = [
            r
            for r in rec.reruns
            if r.split == split and r.eval_digest == lock["eval_digest"] and (data is None or r.data_digest == data)
        ]
        scored = [r for r in mine if r.metrics is not None and not r.failed]
        missed = next((r for r in scored if not meets_target(cfg, r.metrics or {})), None)
        if missed:
            return {**out, "state": "missed", "split": split, "rerun": missed.n, "value": (missed.metrics or {}).get(cfg.primary.name)}
        if not scored:
            failures = sum(1 for r in mine if r.failed and not r.interrupted)
            estimate = rerun_estimate(cfg, rec, split)
            if estimate is not None and not estimate["fits"]:
                too_long.append((split, estimate))  # first: running the other splits would not change the answer
            else:
                (given_up if failures >= cfg.max_attempts else todo).append((split, failures))
    if too_long:
        split, estimate = too_long[0]
        return {**out, "state": "too long", "split": split, "seconds": estimate["seconds"], "limit": estimate["limit"]}
    if todo:
        return {**out, "state": "open", "split": todo[0][0]}
    if given_up:
        return {**out, "state": "given up", "split": given_up[0][0], "failures": given_up[0][1]}
    return out


NOT_CONFIRMED = ("missed", "given up", "too long")  # a claim set aside: the target counts as not reached by it


def claim_text(c: dict[str, Any]) -> str:
    """A claim in one line, for a reason, a brief or a table."""
    again = c.get("split") == c["first"]  # one split only: the same run, made once more
    where = "when it was run again" if again else f"on {c.get('split')}"
    if c["state"] == "missed":
        value = f" ({c['value']:.6g})" if isinstance(c.get("value"), (int, float)) else ""
        return f"{c['id']} reached the target on {c['first']} and not {where}{value}"
    if c["state"] == "given up":
        return f"{c['id']} reached the target on {c['first']} and could not be confirmed {where} (the run there failed {c['failures']} times)"
    if c["state"] == "too long":
        how = "by running it again" if again else f"on {c.get('split')}"
        return f"{c['id']} reached the target on {c['first']} and cannot be confirmed {how}: that run would take about {c['seconds']:.0f} s, the limit is {c['limit']:.0f} s"
    if c["state"] == "open":
        return f"{c['id']} reached the target on {c['first']} and is not confirmed yet"
    done = "running it again" if c["splits"] == [c["first"]] else f"the run{'s' if len(c['splits']) > 1 else ''} on {' and '.join(c['splits'])}"
    return f"{c['id']} reached the target on {c['first']}, and {done} confirmed it"


def claims(ledger: Ledger, lock: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every claim on the target under the evaluator as it is, oldest first."""
    if lock is None or ledger.cfg.primary.target is None:
        return []
    return [c for c in (claim(ledger, e, lock) for e in ledger.experiments()) if c]


def target_claim(ledger: Ledger, lock: dict[str, Any] | None, data: str | None = None) -> dict[str, Any] | None:
    """The claim that counts now: reached under the evaluator as it is and on the data as
    it is, and confirmed or still to be confirmed. A confirmed one comes first.

    data: the digest of the data files, when the caller has it at hand. A result
    reached on other data says nothing about the data as they are.
    """
    by_id = {e.id: e for e in ledger.experiments()} if lock else {}
    standing = []
    for c in claims(ledger, lock):
        measured_on = by_id[c["id"]].data_digest
        if data is not None and measured_on is not None and measured_on != data:
            continue
        if c["state"] in ("confirmed", "open"):
            standing.append(c)
    return next((c for c in standing if c["state"] == "confirmed"), standing[0] if standing else None)


# --- introspections ---------------------------------------------------------------


def introspection_state(ledger: Ledger) -> dict[str, Any]:
    """Whether the periodic introspection is due, and why.

    An experiment belongs to the introspection that listed it when it was
    opened. The ones no closed introspection has listed are "since the last
    one", whatever the clock says: two things in the same second cannot fall
    between two introspections.
    """
    cfg = ledger.cfg
    all_ = ledger.introspections()
    waiting = next((n for n in all_ if n.status == "waiting"), None)
    opened = next((n for n in all_ if n.status == "open"), None)
    closed = [n for n in all_ if n.status == "closed"]
    last = closed[-1] if closed else None
    covered = {eid for n in closed for eid in n.window}

    exps = ledger.experiments()
    recent = [e for e in exps if e.status == "judged" and e.id not in covered]
    recent.sort(key=lambda e: (need(e.verdict).at, e.id))
    # A result that met the target where the loop tunes and not where it never looked is
    # the clearest sign that the loop fits its first split: how to go on is decided here.
    seen = {eid for n in closed for eid in n.unconfirmed}
    unconfirmed = [c for c in claims(ledger, contract.load(ledger)) if c["state"] in NOT_CONFIRMED and c["id"] not in seen]

    reasons = []
    if waiting:
        reasons.append(f"{waiting.id} waits for a person to decide")
    if opened:
        reasons.append(f"{opened.id} is open")
    reasons += [claim_text(c) for c in unconfirmed]
    if len(recent) >= cfg.intro_every:
        start = last.id if last else "the start"
        reasons.append(f"{len(recent)} experiments judged since {start}, an introspection is due every {cfg.intro_every}")
    stagnant = {}
    for track in ledger.tracks():  # parked ones too: parking a stalled track does not skip the introspection
        run = 0
        for e in recent:
            if e.track != track.name or e.type not in ("tune", "feature"):
                continue
            run = 0 if e.decision == "keep" else run + 1
        stagnant[track.name] = run
        if run >= cfg.intro_stagnation:
            reasons.append(f"track {track.name}: {run} scored experiments in a row without a keep")
    if cfg.intro_max_days is not None and recent:
        anchor = parse_time(last.closed) if last and last.closed else parse_time(min(e.created for e in exps))
        days = (parse_time(now()) - anchor).total_seconds() / 86400
        if days > cfg.intro_max_days:
            since = f"{last.id}" if last else "the first experiment"
            reasons.append(f"{days:.1f} days since {since}, the limit is {cfg.intro_max_days}")
    return {
        "due": bool(reasons),
        "reasons": reasons,
        "open": opened.id if opened else None,
        "waiting": waiting.id if waiting else None,
        "last": last.id if last else None,
        "last_decision": last.decision if last else None,
        "reload": last.id if last and not last.reloaded else None,
        "judged_since": len(recent),
        "window": [e.id for e in recent],
        "stagnant": stagnant,
        "unconfirmed": [c["id"] for c in unconfirmed],
    }


def unlisted_reruns(ledger: Ledger) -> list[tuple[str, Rerun]]:
    """Reruns that failed or were cut short and that no introspection has listed yet.

    A rerun belongs to no window: it can come long after its experiment was judged.
    So each introspection keeps the ones its facts named. One written by an older arl
    kept nothing: the reruns that started before it opened (in that second too) are left
    to it, so the first introspection after an upgrade does not list the whole history.
    """
    earlier = ledger.introspections()
    listed = {key for n in earlier for key in n.reruns or []}
    try:
        cutoff = max((parse_time(n.opened) for n in earlier if n.reruns is None and n.opened), default=None)
    except ValueError:
        cutoff = None
    out = []
    for e in ledger.experiments():
        for r in e.reruns:
            if not r.failed or f"{e.id}/{r.n}" in listed:
                continue
            try:
                if cutoff is not None and parse_time(r.at) <= cutoff:
                    continue
            except ValueError:
                pass  # a time that cannot be read: say it rather than lose it
            out.append((e.id, r))
    return out


# --- what comes next --------------------------------------------------------------


def next_action(ledger: Ledger, data: str | None = None, env: dict[str, Any] | None = None) -> dict[str, Any]:
    """The one thing to do now.

    A person's word first (the contract, a pause, a decision that waits for
    them), then work that is unfinished, then the gates, then new work.
    data: the digest of the data files, when the caller has it at hand.
    env: what the python a job would get answered, or the "problem" with it,
    when the caller has asked.
    """
    if not ledger.exists():
        return {"stage": "setup", "why": f"no {ledger.name}/arl.toml in {ledger.home}"}
    lock = contract.load(ledger)
    exps = ledger.experiments()
    if lock is None:
        if exps:  # experiments but no lock: not a new project, something removed the lock
            return {"stage": "contract", "why": f"missing: {ledger.name}/frozen.lock, although experiments exist", "stop": True}
        return {"stage": "freeze", "why": "the contract is not frozen yet"}
    bad = contract.problems(ledger)
    if bad:
        return {"stage": "contract", "why": "; ".join(bad), "stop": True}
    cfg = ledger.cfg
    paused = ledger.paused()
    if paused is not None:
        why = f"paused {str(paused.get('at', ''))[:16]} by {paused.get('by', 'someone')}: {paused.get('reason') or 'no reason given'}"
        return {"stage": "paused", "why": why, "stop": True}
    state = introspection_state(ledger)
    if state["waiting"]:
        return {"stage": "wait", "target": state["waiting"], "why": state["reasons"][0], "stop": True}

    tracks = {t.name: t for t in ledger.tracks()}
    env_problem = (env or {}).get("problem")

    def no_reference(e: Record) -> bool:
        if e.type not in ("tune", "feature") or e.track not in tracks:
            return False
        return reference_for(ledger, tracks[e.track], lock["eval_digest"], data) is None

    for e in exps:
        if e.status == "judging":
            return {"stage": "unfinished", "target": e.id, "why": f"the verdict of {e.id} was cut off before it was complete"}
    for e in exps:
        if e.status == "running":
            if not stale(e):
                return {"stage": "running", "target": e.id, "why": f"{e.id} is running (pid {e.pid})"}
            if not env_problem:  # with no environment nothing can be continued: that is said below
                return {"stage": "interrupted", "target": e.id, "why": f"the run of {e.id} ended without a result"}
    for e in exps:
        if e.status == "evaluated":
            return {"stage": "judge", "target": e.id, "why": f"{e.id} has a result and no verdict"}
    if env_problem:
        return {"stage": "environment", "why": env_problem, "stop": True}
    for e in exps:
        if e.status == "crashed":
            tries = len(e.attempts)
            why = f"{e.id} failed {tries} time(s)"
            if tries >= cfg.max_attempts:
                why += ", the limit: judge it as a crash"
            elif no_reference(e):
                why += f" and track {e.track} has no reference any more: judge it as a crash"
            return {"stage": "crashed", "target": e.id, "why": why}
    if state["open"]:
        return {"stage": "introspect", "target": state["open"], "why": f"{state['open']} is open"}
    if state["last_decision"] == "stop":
        return {"stage": "stopped", "target": state["last"], "why": f"{state['last']} decided to stop", "stop": True}
    if state["reload"]:
        return {"stage": "reload", "target": state["reload"], "why": f"{state['reload']} is closed and what it changed has not been read again"}
    changed = ledger.changed_since_reload()
    if changed:
        return {"stage": "reload", "why": f"{', '.join(changed)} changed since the last reload"}
    hit = target_claim(ledger, lock, data)
    if hit and hit["state"] == "confirmed":
        return {"stage": "target", "target": hit["id"], "why": claim_text(hit), "stop": True}
    spent = budget(ledger)
    if hit and not spent["hours_spent"]:  # the run that confirms it takes hours like any other
        return {"stage": "confirm", "target": hit["id"], "split": hit["split"], "why": claim_text(hit)}
    for e in exps:
        if e.status == "paused":
            if spent["hours_spent"]:
                break
            how = (e.attempts[-1].paused if e.attempts else None) or "it was interrupted"
            return {"stage": "resume", "target": e.id, "why": how}
    if spent["spent"]:
        return {"stage": "budget", "why": "; ".join(spent["spent"] + ([claim_text(hit)] if hit else [])), "stop": True}
    for e in exps:
        if e.status == "draft":
            if no_reference(e):
                why = f"{e.id} was drafted against a reference that no longer counts"
                return {"stage": "outdated", "target": e.id, "why": why}
            return {"stage": "design", "target": e.id, "why": f"{e.id} is drafted and has not run"}
    if state["due"]:
        return {"stage": "introspect", "why": "; ".join(state["reasons"])}

    active = [t for t in tracks.values() if t.status == "active"]
    if not active:
        return {"stage": "track", "why": "no active track"}
    for t in active:
        if reference_for(ledger, t, lock["eval_digest"], data) is None:
            return {
                "stage": "baseline",
                "target": t.name,
                "why": f"track {t.name} has no reference under contract v{lock['version']} and the data as it is",
            }
    return {"stage": "hypothesize", "why": "nothing in flight, no gate closed"}
