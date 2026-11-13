"""The mechanical decisions: how a result compares, which verdicts it allows,
when an introspection is due, when the budget is spent, and what comes next."""

from __future__ import annotations

from typing import Any

from . import contract
from .config import Config
from .ledger import Ledger
from .runner import alive
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


def compare(cfg: Config, metrics: dict[str, float], reference: dict[str, Any] | None, repeat: bool = False) -> dict[str, Any]:
    """Outcome of a scored run against the track's reference experiment.

    No reference: this run is the reference. A baseline on a track that has one
    is a repeat: it shows the noise and never replaces the reference. Otherwise
    improve, neutral, worse or gate_fail.
    """
    primary = cfg.primary
    value = metrics[primary.name]
    ref_metrics = reference["metrics"] if reference else None
    fails = gate_failures(cfg, metrics, ref_metrics)
    out: dict[str, Any] = {
        "metric": primary.name,
        "value": value,
        "gate_failures": fails,
        "target_met": bool(primary.target is not None and not fails and primary.meets(value, primary.target)),
    }
    if reference is None:
        out["outcome"] = "reference"
        return out
    assert ref_metrics is not None
    gain = primary.gain(value, ref_metrics[primary.name])
    out.update(
        reference=reference["id"],
        reference_value=ref_metrics[primary.name],
        gain=gain,
        min_delta=primary.min_delta,
    )
    ref_failed = bool(reference.get("comparison", {}).get("gate_failures"))
    if repeat:
        out["outcome"] = "repeat"
        out["target_met"] = False
    elif fails:
        out["outcome"] = "gate_fail"
    elif ref_failed:
        out["outcome"] = "improve"  # inside the constraints beats outside them
    elif gain > 0 and gain >= primary.min_delta:
        out["outcome"] = "improve"
    elif gain < 0 and -gain >= primary.min_delta:
        out["outcome"] = "worse"
    else:
        out["outcome"] = "neutral"
    return out


def allowed_verdicts(rec: dict[str, Any]) -> list[str]:
    """What the agent may decide. The first entry is the default."""
    if rec["status"] in ("crashed", "paused"):
        return ["crash"]
    if rec["status"] != "evaluated":
        return []
    if rec["type"] == "diagnose":
        return ["finding"]
    outcome = rec.get("comparison", {}).get("outcome")
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


def reference_for(ledger: Ledger, track: dict[str, Any], eval_digest: str, data: str | None = None) -> dict[str, Any] | None:
    """The experiment that measured the track head under the current evaluator, on the current data."""
    eid = track.get("head_experiment")
    if not eid:
        return None
    rec = ledger.record(eid)
    if rec.get("contract", {}).get("eval_digest") != eval_digest:
        return None
    measured_on = rec.get("conditions", {}).get("data_digest")
    if data is not None and measured_on is not None and measured_on != data:
        return None
    return rec


def in_flight(ledger: Ledger, track: str | None = None) -> list[dict[str, Any]]:
    return [
        e
        for e in ledger.experiments()
        if e["status"] in IN_FLIGHT and (track is None or e["track"] == track)
    ]


def kept(rec: dict[str, Any]) -> bool:
    return rec["status"] == "judged" and rec["verdict"]["decision"] == "keep"


def stale(rec: dict[str, Any]) -> bool:
    """Marked as running, but the arl that ran it is gone."""
    return rec["status"] == "running" and not alive(rec.get("pid"), rec.get("pid_start"))


# --- budget -----------------------------------------------------------------------


def budget(ledger: Ledger) -> dict[str, Any]:
    """Experiments that ran, and the hours of every run: failed ones, interrupted ones, reruns."""
    cfg = ledger.cfg
    exps = ledger.experiments()
    ran = [e for e in exps if e.get("attempts")]
    runs = [a for e in exps for a in (*e.get("attempts", []), *e.get("reruns", []))]
    hours = sum(a.get("run_seconds", 0) + a.get("eval_seconds", 0) for a in runs) / 3600
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


def target_met(ledger: Ledger) -> dict[str, Any] | None:
    lock = contract.load(ledger)
    if lock is None or ledger.cfg.primary.target is None:
        return None
    for e in ledger.experiments():
        same = e.get("contract", {}).get("eval_digest") == lock["eval_digest"]
        if kept(e) and same and e.get("comparison", {}).get("target_met"):
            return e
    return None


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
    waiting = next((n for n in all_ if n["status"] == "waiting"), None)
    opened = next((n for n in all_ if n["status"] == "open"), None)
    closed = [n for n in all_ if n["status"] == "closed"]
    last = closed[-1] if closed else None
    covered = {eid for n in closed for eid in n.get("window", [])}

    exps = ledger.experiments()
    recent = [e for e in exps if e["status"] == "judged" and e["id"] not in covered]
    recent.sort(key=lambda e: (e["verdict"]["at"], e["id"]))

    reasons = []
    if waiting:
        reasons.append(f"{waiting['id']} waits for a person to decide")
    if opened:
        reasons.append(f"{opened['id']} is open")
    if len(recent) >= cfg.intro_every:
        start = last["id"] if last else "the start"
        reasons.append(f"{len(recent)} experiments judged since {start}, an introspection is due every {cfg.intro_every}")
    stagnant = {}
    for track in ledger.tracks():  # parked ones too: parking a stalled track does not skip the introspection
        run = 0
        for e in recent:
            if e["track"] != track["name"] or e["type"] not in ("tune", "feature"):
                continue
            run = 0 if e["verdict"]["decision"] == "keep" else run + 1
        stagnant[track["name"]] = run
        if run >= cfg.intro_stagnation:
            reasons.append(f"track {track['name']}: {run} scored experiments in a row without a keep")
    if cfg.intro_max_days is not None and recent:
        anchor = parse_time(last["closed"]) if last else parse_time(min(e["created"] for e in exps))
        days = (parse_time(now()) - anchor).total_seconds() / 86400
        if days > cfg.intro_max_days:
            since = f"{last['id']}" if last else "the first experiment"
            reasons.append(f"{days:.1f} days since {since}, the limit is {cfg.intro_max_days}")
    return {
        "due": bool(reasons),
        "reasons": reasons,
        "open": opened["id"] if opened else None,
        "waiting": waiting["id"] if waiting else None,
        "last": last["id"] if last else None,
        "last_decision": last.get("decision") if last else None,
        "reload": last["id"] if last and not last.get("reloaded") else None,
        "judged_since": len(recent),
        "window": [e["id"] for e in recent],
        "stagnant": stagnant,
    }


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

    tracks = {t["name"]: t for t in ledger.tracks()}
    env_problem = (env or {}).get("problem")

    def no_reference(e: dict[str, Any]) -> bool:
        if e["type"] not in ("tune", "feature") or e["track"] not in tracks:
            return False
        return reference_for(ledger, tracks[e["track"]], lock["eval_digest"], data) is None

    for e in exps:
        if e["status"] == "judging":
            return {"stage": "unfinished", "target": e["id"], "why": f"the verdict of {e['id']} was cut off before it was complete"}
    for e in exps:
        if e["status"] == "running":
            if not stale(e):
                return {"stage": "running", "target": e["id"], "why": f"{e['id']} is running (pid {e.get('pid')})"}
            if not env_problem:  # with no environment nothing can be continued: that is said below
                return {"stage": "interrupted", "target": e["id"], "why": f"the run of {e['id']} ended without a result"}
    for e in exps:
        if e["status"] == "evaluated":
            return {"stage": "judge", "target": e["id"], "why": f"{e['id']} has a result and no verdict"}
    if env_problem:
        return {"stage": "environment", "why": env_problem, "stop": True}
    for e in exps:
        if e["status"] == "crashed":
            tries = len(e.get("attempts", []))
            why = f"{e['id']} failed {tries} time(s)"
            if tries >= cfg.max_attempts:
                why += ", the limit: judge it as a crash"
            elif no_reference(e):
                why += f" and track {e['track']} has no reference any more: judge it as a crash"
            return {"stage": "crashed", "target": e["id"], "why": why}
    if state["open"]:
        return {"stage": "introspect", "target": state["open"], "why": f"{state['open']} is open"}
    if state["last_decision"] == "stop":
        return {"stage": "stopped", "target": state["last"], "why": f"{state['last']} decided to stop", "stop": True}
    if state["reload"]:
        return {"stage": "reload", "target": state["reload"], "why": f"{state['reload']} is closed and what it changed has not been read again"}
    changed = ledger.changed_since_reload()
    if changed:
        return {"stage": "reload", "why": f"{', '.join(changed)} changed since the last reload"}
    hit = target_met(ledger)
    if hit:
        return {"stage": "target", "target": hit["id"], "why": f"{hit['id']} reached the target", "stop": True}
    spent = budget(ledger)
    for e in exps:
        if e["status"] == "paused":
            if spent["hours_spent"]:
                break
            how = (e.get("attempts") or [{}])[-1].get("paused", "it was interrupted")
            return {"stage": "resume", "target": e["id"], "why": how}
    if spent["spent"]:
        return {"stage": "budget", "why": "; ".join(spent["spent"]), "stop": True}
    for e in exps:
        if e["status"] == "draft":
            if no_reference(e):
                why = f"{e['id']} was drafted against a reference that no longer counts"
                return {"stage": "outdated", "target": e["id"], "why": why}
            return {"stage": "design", "target": e["id"], "why": f"{e['id']} is drafted and has not run"}
    if state["due"]:
        return {"stage": "introspect", "why": "; ".join(state["reasons"])}

    active = [t for t in tracks.values() if t["status"] == "active"]
    if not active:
        return {"stage": "track", "why": "no active track"}
    for t in active:
        if reference_for(ledger, t, lock["eval_digest"], data) is None:
            return {
                "stage": "baseline",
                "target": t["name"],
                "why": f"track {t['name']} has no reference under contract v{lock['version']} and the data as it is",
            }
    return {"stage": "hypothesize", "why": "nothing in flight, no gate closed"}
