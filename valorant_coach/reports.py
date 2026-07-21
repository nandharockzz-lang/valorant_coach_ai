from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import Database


def build_report(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    rounds = db.get_rounds(match_id)
    deaths = db.get_deaths(match_id)
    enrich_deaths_with_display_rounds(deaths, rounds)
    review = db.get_latest_match_review(match_id)
    suggestions = db.get_death_suggestions(match_id)
    match_analyses = {
        analysis_type: db.get_latest_structured_analysis("match", match_id, analysis_type)
        for analysis_type in (
            "hud",
            "minimap",
            "ocr",
            "scoreboard_rounds",
            "death_events_v2",
            "round_timeline",
            "crosshair",
            "review_queue",
            "review_queue_v2",
            "round_story_v2",
            "auto_coach_summary",
            "full_vod_coach_moments",
            "full_vod_coach",
            "evaluation_benchmark",
            "detector_tuning",
            "session_report",
            "guided_coach",
        )
    }
    for death in deaths:
        death["round_phase"] = round_phase(rounds, death.get("timestamp"))
        death["match_context"] = build_death_match_context(db, match, death, rounds, match_analyses)
    match_themes = build_match_themes(match, deaths)
    label_counts = Counter(
        label
        for death in deaths
        for label in death.get("mistake_labels", [])
        if label and label != "needs manual review"
    )

    focus = []
    for label, count in label_counts.most_common(5):
        focus.append(recommendation_for(label, count))
    if not focus:
        focus.append("Create or correct death labels for this match, then rerun the report to get targeted coaching.")

    return {
        "match": match,
        "rounds": rounds,
        "deaths": deaths,
        "label_counts": dict(label_counts),
        "focus": focus,
        "review": review,
        "guided_coach": match_analyses.get("guided_coach"),
        "suggestions": suggestions,
        "match_analyses": match_analyses,
        "match_themes": match_themes,
        "coach_moment_feedback": db.list_subject_analyses("match", match_id, "coach_moment_feedback", limit=200),
}


def build_match_themes(match: Dict[str, Any], deaths: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels: Counter = Counter()
    dimensions: Counter = Counter()
    round_patterns: Counter = Counter()
    context_patterns: Counter = Counter()
    quality: Counter = Counter()
    evidence_examples = []
    for death in deaths:
        for label in death.get("mistake_labels") or []:
            if label and label != "needs manual review":
                labels[label] += 1
        payload = latest_payload(death.get("local_ai_review"))
        for label in payload.get("labels") or []:
            if label:
                labels[label] += 1
        coaching = payload.get("coaching") or {}
        for key, label in (
            ("utility_issue", "utility"),
            ("crosshair_issue", "crosshair"),
            ("positioning_issue", "positioning"),
            ("mechanical_issue", "mechanics"),
        ):
            value = str(coaching.get(key) or payload.get(key) or "").lower()
            if value and not any(token in value for token in ("no", "none", "insufficient")):
                dimensions[label] += 1
        perception = payload.get("perception") or {}
        if perception.get("enemy_seen") and str(perception.get("enemy_seen")).lower() not in {"false", "unknown"}:
            dimensions["enemy contact"] += 1
        context = death.get("match_context") or {}
        fields = context.get("fields") or {}
        for key in ("map", "agent", "weapon"):
            value = (fields.get(key) or {}).get("value")
            if value:
                context_patterns[f"{key}: {value}"] += 1
        round_value = death.get("round_number") or death.get("display_round_number")
        if round_value:
            round_patterns[f"round {round_value}"] += 1
        if death.get("round_phase"):
            round_patterns[str(death.get("round_phase"))] += 1
        q = payload.get("review_quality") or {}
        if q.get("summary"):
            quality[str(q.get("summary"))] += 1
        for item in payload.get("evidence_timeline") or []:
            if item.get("evidence") and len(evidence_examples) < 6:
                evidence_examples.append(
                    {
                        "death_id": death.get("id"),
                        "timestamp": death.get("timestamp"),
                        "event": item.get("event"),
                        "evidence": item.get("evidence"),
                        "confidence": item.get("claim_confidence"),
                    }
                )

    top_labels = labels.most_common(3)
    top_dimensions = dimensions.most_common(3)
    map_name = match.get("map") or "this map"
    agent = match.get("agent") or "your agent"
    if top_labels:
        summary = f"Top match theme: {top_labels[0][0]} appeared {top_labels[0][1]} time(s)."
    else:
        summary = "Not enough reviewed deaths for a strong match theme yet."
    practice = build_theme_practice_plan(top_labels, top_dimensions, map_name, agent)
    return {
        "summary": summary,
        "top_mistakes": [{"label": label, "count": count} for label, count in top_labels],
        "top_dimensions": [{"label": label, "count": count} for label, count in top_dimensions],
        "context_patterns": [{"label": label, "count": count} for label, count in context_patterns.most_common(6)],
        "round_patterns": [{"label": label, "count": count} for label, count in round_patterns.most_common(6)],
        "review_quality": [{"label": label, "count": count} for label, count in quality.most_common(4)],
        "evidence_examples": evidence_examples,
        "practice_plan": practice,
        "confidence": round(min(0.85, 0.25 + sum(labels.values()) * 0.04 + sum(dimensions.values()) * 0.03), 2),
    }


def build_theme_practice_plan(top_labels: List[Any], top_dimensions: List[Any], map_name: str, agent: str) -> List[str]:
    plan = []
    focus = top_labels[0][0] if top_labels else (top_dimensions[0][0] if top_dimensions else "death review discipline")
    lower = str(focus).lower()
    if "crosshair" in lower:
        plan.append(f"On {map_name}, dry-clear common lanes with {agent} and pause before each angle to place crosshair at head height.")
        plan.append("In deathmatch, only take fights where the crosshair is already near the target before the enemy appears.")
    elif "utility" in lower or "dry peek" in lower:
        plan.append(f"Before first contact on {map_name}, require one info/utility/trade condition before committing.")
        plan.append("Review three deaths and write the utility or teammate timing that would have changed the fight.")
    elif "position" in lower or "exposed" in lower:
        plan.append("Pause every death at first contact and name the two sightlines that could see you.")
        plan.append(f"Practice one route on {map_name} where you isolate one angle at a time.")
    else:
        plan.append(f"Review the top three deaths for {focus} and write one avoidable decision before queueing again.")
        plan.append("For the next match, track only this one focus so the coach can measure whether it repeats.")
    plan.append("After the next session, mark Clip Coach reviews as useful/wrong so the personal coach adapts.")
    return plan


def save_death_context_correction(db: Database, death_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found"}
    match = db.get_match(int(death.get("match_id") or 0)) or {}

    context = {
        "kind": "context_correction",
        "source": clean_optional(payload.get("source")) or "manual",
        "map": clean_optional(payload.get("map")),
        "agent": clean_optional(payload.get("agent")),
        "side": clean_optional(payload.get("side")),
        "round_number": optional_int(payload.get("round_number")),
        "weapon": clean_optional(payload.get("weapon")),
        "location": clean_optional(payload.get("location")),
        "spike_state": clean_optional(payload.get("spike_state")),
        "team_counts": clean_optional(payload.get("team_counts")),
        "confidence": clamp_float(payload.get("confidence"), 0.0, 1.0, 1.0),
        "notes": clean_optional(payload.get("notes")),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    context = {key: value for key, value in context.items() if value not in (None, "")}

    if context.get("map") or context.get("agent"):
        updates = {}
        if context.get("map"):
            updates["map"] = context["map"]
        if context.get("agent"):
            updates["agent"] = context["agent"]
        db.update_match(int(match["id"]), **updates)
    if context.get("round_number"):
        db.update_death_round_number(death_id, int(context["round_number"]))

    correction_id = db.save_death_analysis(death_id, "context_correction", context)
    refreshed = db.get_death(death_id) or death
    refreshed_match = db.get_match(int(refreshed.get("match_id") or match.get("id") or 0)) or match
    return {
        "ok": True,
        "correction_id": correction_id,
        "context": build_death_match_context(db, refreshed_match, refreshed, db.get_rounds(int(refreshed_match.get("id") or 0)), {}),
    }


def build_death_match_context(
    db: Database,
    match: Dict[str, Any],
    death: Dict[str, Any],
    rounds: List[Dict[str, Any]],
    match_analyses: Dict[str, Any],
) -> Dict[str, Any]:
    correction = latest_payload(db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_correction"))
    extraction = latest_payload(death.get("context_extraction") or db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_extraction"))
    extracted = extraction.get("resolved") or {}
    local_ai = latest_payload(death.get("local_ai_review"))
    perception = local_ai.get("perception") or {}
    scoreboard = latest_payload(match_analyses.get("scoreboard_rounds"))
    ocr = latest_payload(match_analyses.get("ocr"))

    fields = {
        "map": context_field_from_sources("map", correction, extracted, match.get("map"), "match metadata"),
        "agent": context_field_from_sources("agent", correction, extracted, match.get("agent"), "match metadata"),
        "round": context_round_field(correction, extracted, death, scoreboard),
        "side": context_field_from_sources("side", correction, extracted, infer_side(rounds, death), "round timeline"),
        "weapon": context_field_from_sources("weapon", correction, extracted, perception.get("weapon_seen") or local_ai.get("weapon") or ocr.get("weapon"), "local AI/OCR"),
        "location": context_field_from_sources("location", correction, extracted, perception.get("location") or local_ai.get("location"), "local AI"),
        "spike_state": context_field_from_sources("spike_state", correction, extracted, perception.get("spike_state_seen") or local_ai.get("spike_state"), "local AI/HUD"),
        "team_counts": context_field_from_sources("team_counts", correction, extracted, perception.get("team_counts") or ocr.get("team_counts"), "local AI/OCR"),
        "round_phase": context_field(death.get("round_phase"), "round timeline", known_confidence(death.get("round_phase"))),
    }
    known = sum(1 for item in fields.values() if item["known"])
    required = ["map", "agent", "round"]
    ready_for_kb = all(fields[key]["known"] for key in required)
    return {
        "fields": fields,
        "known_count": known,
        "total_count": len(fields),
        "ready_for_knowledge": ready_for_kb,
        "summary": context_summary(fields, ready_for_kb),
        "manual_correction": correction,
        "context_extraction": extraction,
    }


def context_field_from_sources(key: str, correction: Dict[str, Any], extracted: Dict[str, Any], fallback: Any, fallback_source: str) -> Dict[str, Any]:
    if correction.get(key):
        return context_field(correction.get(key), correction_source(correction), 1.0)
    extracted_text = extracted_value(extracted, key)
    if extracted_text:
        return context_field(extracted_text, "KB-constrained local extraction", extracted_confidence(extracted, key))
    return context_field(fallback, fallback_source, known_confidence(fallback))


def context_round_field(correction: Dict[str, Any], extracted: Dict[str, Any], death: Dict[str, Any], scoreboard: Dict[str, Any]) -> Dict[str, Any]:
    if correction.get("round_number"):
        return context_field(correction.get("round_number"), correction_source(correction), 1.0)
    if death.get("round_number"):
        return context_field(death.get("round_number"), "marker", 1.0)
    extracted_round = extracted_value(extracted, "round_number")
    if extracted_round:
        return context_field(extracted_round, "KB-constrained local extraction", extracted_confidence(extracted, "round_number"))
    if death.get("display_round_number"):
        return context_field(death.get("display_round_number"), death.get("round_source") or "timeline estimate", 0.45)
    return context_field(scoreboard.get("inferred_round"), "scoreboard round analysis", 0.2)


def extracted_value(extracted: Dict[str, Any], key: str) -> Any:
    item = extracted.get(key) if isinstance(extracted, dict) else {}
    return item.get("value") if isinstance(item, dict) else ""


def extracted_confidence(extracted: Dict[str, Any], key: str) -> float:
    item = extracted.get(key) if isinstance(extracted, dict) else {}
    return clamp_float(item.get("confidence"), 0.0, 1.0, 0.0) if isinstance(item, dict) else 0.0


def correction_source(correction: Dict[str, Any]) -> str:
    if correction.get("source") == "mixed":
        return "manual + KB-constrained extraction"
    return "KB-constrained local extraction" if correction.get("source") == "context_extraction" else "manual"


def context_field(value: Any, source: str, confidence: float) -> Dict[str, Any]:
    text = clean_optional(value)
    unknown = not text or text.lower() in {"unknown", "n/a", "none", "null", "unreadable"}
    return {
        "value": "" if unknown else text,
        "known": not unknown,
        "source": source,
        "confidence": round(clamp_float(confidence, 0.0, 1.0, 0.0), 2),
    }


def context_summary(fields: Dict[str, Dict[str, Any]], ready_for_kb: bool) -> str:
    core = []
    for key in ("map", "agent", "round"):
        value = fields[key]["value"] if fields[key]["known"] else "unknown"
        core.append(f"{key}={value}")
    suffix = "KB retrieval can be specific." if ready_for_kb else "KB retrieval will fall back to generic coaching until map/agent/round are corrected."
    return ", ".join(core) + f". {suffix}"


def infer_side(rounds: List[Dict[str, Any]], death: Dict[str, Any]) -> str:
    round_number = death.get("round_number") or death.get("display_round_number")
    if round_number:
        for item in rounds:
            if int(item.get("round_number") or 0) == int(round_number) and item.get("side"):
                return str(item.get("side"))
    timestamp = death.get("timestamp")
    if timestamp is not None:
        phase_round = infer_round_from_timeline(rounds, timestamp)
        for item in rounds:
            if int(item.get("round_number") or 0) == phase_round and item.get("side"):
                return str(item.get("side"))
    return ""


def latest_payload(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    payload = row.get("payload") if isinstance(row, dict) else {}
    return payload if isinstance(payload, dict) else {}


def known_confidence(value: Any) -> float:
    return 0.75 if clean_optional(value) else 0.0


def clean_optional(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def recommendation_for(label: str, count: int) -> str:
    advice = {
        "dry peek": "Before taking first contact, pair the peek with utility, a teammate trade, or a jiggle for info.",
        "crosshair too low/wide": "Run 10 minutes of deathmatch with a rule: crosshair stays head-height at the next likely angle.",
        "exposed to multiple angles": "Pause before entries and name the two angles that can see you; isolate one before committing.",
        "poor reposition after contact": "After first contact, strafe back to cover or change elevation/angle before repeeking.",
        "isolated from team": "Delay solo contact until a teammate can trade or until utility creates a timing advantage.",
        "repeated same-angle fight": "After losing an angle once, change the fight condition with timing, utility, or position.",
        "late rotation / bad timing": "Review minimap timing after first contact and rotate on confirmed pressure, not after site collapse.",
        "utility unused before taking space": "Pick one ability per round that must be spent before your first committed fight.",
    }
    base = advice.get(label, f"Review every death tagged '{label}' and write the avoidable decision before the fight.")
    return f"{label} x{count}: {base}"


def enrich_deaths_with_display_rounds(deaths: List[Dict[str, Any]], rounds: List[Dict[str, Any]]) -> None:
    """Attach a UI-safe round label without pretending an estimate is confirmed data."""
    for death in deaths:
        if death.get("round_number"):
            death["display_round_number"] = int(death["round_number"])
            death["round_source"] = "confirmed"
            continue
        inferred = infer_round_from_timeline(rounds, death.get("timestamp"))
        if inferred:
            death["display_round_number"] = inferred
            death["round_source"] = "timeline"
            continue
        estimated = estimate_round_from_death_spacing(deaths, death)
        if estimated:
            death["display_round_number"] = estimated
            death["round_source"] = "estimated"


def infer_round_from_timeline(rounds: List[Dict[str, Any]], timestamp: Any) -> int:
    if timestamp is None:
        return 0
    ts = float(timestamp)
    for item in rounds:
        start = float(item.get("start_ts") or 0)
        end = item.get("end_ts")
        if ts < start:
            continue
        if end is not None and ts > float(end):
            continue
        return int(item.get("round_number") or 0)
    return 0


def estimate_round_from_death_spacing(deaths: List[Dict[str, Any]], target: Dict[str, Any]) -> int:
    if target.get("timestamp") is None:
        return 0
    timed = sorted(
        (death for death in deaths if death.get("timestamp") is not None),
        key=lambda death: (float(death["timestamp"]), int(death.get("id") or 0)),
    )
    if not timed:
        return 0
    current_round = 1
    previous_ts = float(timed[0]["timestamp"])
    for death in timed:
        ts = float(death["timestamp"])
        if ts - previous_ts > 55:
            current_round += 1
        if int(death.get("id") or 0) == int(target.get("id") or -1):
            return current_round
        previous_ts = ts
    return 0


def render_markdown(report: Dict[str, Any]) -> str:
    match = report["match"]
    lines = [
        f"# VALORANT Match Report #{match['id']}",
        "",
        f"- Video: `{match['video_path']}`",
        f"- Map: {match.get('map') or 'unknown'}",
        f"- Agent: {match.get('agent') or 'unknown'}",
        f"- Status: {match['status']}",
        f"- Deaths reviewed: {len(report['deaths'])}",
        "",
        "## Recurring Mistakes",
    ]
    if report["label_counts"]:
        for label, count in report["label_counts"].items():
            lines.append(f"- {label}: {count}")
    else:
        lines.append("- No labeled mistakes yet.")

    lines.extend(["", "## Next Session Focus"])
    for item in report["focus"]:
        lines.append(f"- {item}")

    if report.get("review"):
        review = report["review"]
        lines.extend(["", "## Coach Review"])
        lines.append(f"- Score: {review['score']}/100")
        lines.append(f"- Summary: {review['summary']}")
        lines.append(f"- Next action: {review['next_action']}")
        lines.append(f"- Drill: {review['drill']}")
        lines.append(f"- Coach note: {review['coach_note']}")

    analyses = report.get("match_analyses") or {}
    if any(analyses.values()):
        lines.extend(["", "## Visual And OCR Analysis"])
        for analysis_type, analysis in analyses.items():
            if analysis:
                payload = analysis.get("payload") or {}
                lines.append(f"- {analysis_type.upper()}: {payload.get('summary') or 'analysis captured'}")

    lines.extend(["", "## Death Review"])
    for death in report["deaths"]:
        labels = ", ".join(death.get("mistake_labels") or ["unlabeled"])
        ts = format_ts(death.get("timestamp"))
        if death.get("round_number"):
            round_label = f"Round {death.get('round_number')}"
        elif death.get("display_round_number"):
            round_label = f"Est. Round {death.get('display_round_number')}"
        else:
            round_label = "Round unknown"
        lines.append(f"- {round_label} @ {ts}: {labels}. {death.get('notes') or ''}".rstrip())
        if death.get("advice"):
            advice = death["advice"]
            lines.append(f"  - Advice: {advice['better_play']}")
            lines.append(f"  - Drill: {advice['drill']}")
        if death.get("vision"):
            vision = death["vision"]
            lines.append(f"  - Visual read: {vision['summary']}")
        if death.get("understanding"):
            understanding = death["understanding"].get("payload") or {}
            lines.append(f"  - Clip understanding: {understanding.get('summary', 'analysis captured')}")
        if death.get("round_phase"):
            lines.append(f"  - Round phase: {death['round_phase']}")

    return "\n".join(lines) + "\n"


def write_markdown_report(db: Database, reports_dir: Path, match_id: int) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(db, match_id)
    path = reports_dir / f"match-{match_id}.md"
    path.write_text(render_markdown(report), encoding="utf-8")
    return path


def format_ts(value: Any) -> str:
    if value is None:
        return "unknown"
    seconds = int(float(value))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def round_phase(rounds: List[Dict[str, Any]], timestamp: Any) -> str:
    if timestamp is None:
        return "unknown"
    ts = float(timestamp)
    for item in rounds:
        start = float(item.get("start_ts") or 0)
        end = item.get("end_ts")
        if end is not None and not (start <= ts <= float(end)):
            continue
        elapsed = ts - start
        if elapsed < 25:
            return "early round"
        if elapsed < 65:
            return "mid round"
        return "late round"
    return "unknown"
