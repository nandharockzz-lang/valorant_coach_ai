from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from .db import Database


def build_report(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    rounds = db.get_rounds(match_id)
    deaths = db.get_deaths(match_id)
    review = db.get_latest_match_review(match_id)
    suggestions = db.get_death_suggestions(match_id)
    match_analyses = {
        analysis_type: db.get_latest_structured_analysis("match", match_id, analysis_type)
        for analysis_type in (
            "hud",
            "minimap",
            "ocr",
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
        "coach_moment_feedback": db.list_subject_analyses("match", match_id, "coach_moment_feedback", limit=200),
    }


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
        round_number = death.get("round_number") or "?"
        lines.append(f"- R{round_number} @ {ts}: {labels}. {death.get('notes') or ''}".rstrip())
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
