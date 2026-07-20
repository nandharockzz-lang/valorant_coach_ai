from typing import Any, Dict, List, Optional

from .advice import ADVICE_BY_LABEL, generate_advice
from .db import Database
from .memory import load_coach_memory_state, memory_dashboard_overlay


LABEL_PRESETS = [
    "dry peek",
    "crosshair too low/wide",
    "exposed to multiple angles",
    "poor reposition after contact",
    "isolated from team",
    "repeated same-angle fight",
    "late rotation / bad timing",
    "utility unused before taking space",
]


def build_coach_dashboard(db: Database) -> Dict[str, Any]:
    profile = db.get_profile()
    active_goal = db.get_active_goal()
    trends = db.build_trends()
    feedback = db.get_feedback_summary()
    sessions = db.get_session_summary()
    suggestion_learning = db.suggestion_learning_summary()
    memory = build_personal_memory(db, trends, feedback, suggestion_learning)
    outcomes = build_session_outcomes(db, trends, memory)
    plan = build_session_plan(profile, active_goal, trends, feedback)
    return {
        "profile": profile,
        "active_goal": active_goal,
        "feedback": feedback,
        "sessions": sessions,
        "suggestion_learning": suggestion_learning,
        "memory": memory,
        "outcomes": outcomes,
        "label_presets": LABEL_PRESETS,
        "plan": plan,
    }


def build_match_review(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")

    profile = db.get_profile()
    active_goal = db.get_active_goal()
    deaths = db.get_deaths(match_id)
    label_counts = count_labels(deaths)
    total_deaths = len(deaths)
    focus = active_goal["focus_label"] if active_goal else first_key(label_counts) or "death review discipline"
    focus_deaths = int(label_counts.get(focus, 0))
    advice_count = sum(1 for death in deaths if death.get("advice"))
    accepted_count = sum(
        1
        for death in deaths
        if death.get("advice") and (death["advice"].get("feedback") or {}).get("verdict") == "accepted"
    )
    top_label = first_key(label_counts) or "unlabeled deaths"
    template = ADVICE_BY_LABEL.get(focus) or ADVICE_BY_LABEL.get(top_label)

    if active_goal:
        if focus_deaths == 0:
            focus_read = f"You avoided the active focus mistake '{focus}' in the marked deaths."
        else:
            focus_read = f"The active focus '{focus}' appeared in {focus_deaths} of {total_deaths} marked death(s)."
    else:
        focus_read = f"No active focus was set. The match points toward '{top_label}' as the next focus."

    if template:
        next_action = template["better_play"]
        drill = template["drill"]
    else:
        next_action = "Keep labeling every death, then choose one repeated mistake as the next session focus."
        drill = "Review the first five deaths and write the avoidable decision for each one."

    review = {
        "match_id": match_id,
        "summary": build_review_summary(match, total_deaths, top_label, focus_read),
        "focus_result": focus_read,
        "top_mistake": top_label,
        "label_counts": label_counts,
        "next_action": next_action,
        "drill": drill,
        "coach_note": build_coach_note(profile, advice_count, accepted_count),
        "score": review_score(total_deaths, focus_deaths, advice_count),
    }
    review_id = db.save_match_review(review)
    review["id"] = review_id
    return review


def build_guided_match_coach(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")

    deaths = db.get_deaths(match_id)
    suggestions = db.get_death_suggestions(match_id)
    profile = db.get_profile()
    trends = db.build_trends()
    active_goal = db.get_active_goal()
    label_counts = count_labels(deaths)
    generated_advice = []

    for death in deaths[:6]:
        if not death.get("advice"):
            generated_advice.append(generate_advice(db, int(death["id"])))

    deaths = db.get_deaths(match_id)
    review = build_match_review(db, match_id)
    focus = choose_focus(active_goal, label_counts, trends)
    review_order = build_review_order(deaths, suggestions, focus)
    homework = build_homework(focus, match, profile, review_order)
    coach = {
        "kind": "guided_match_coach",
        "match_id": match_id,
        "summary": guided_summary(match, deaths, suggestions, focus, generated_advice),
        "focus": focus,
        "coach_read": coach_read(match, deaths, suggestions, label_counts, focus, review),
        "review_order": review_order,
        "between_round_rule": between_round_rule(focus),
        "homework": homework,
        "generated_advice": len(generated_advice),
        "confidence": guided_confidence(deaths, suggestions, generated_advice),
    }
    coach["id"] = db.save_structured_analysis(match_id, "guided_coach", coach)
    return coach


def choose_focus(
    active_goal: Optional[Dict[str, Any]],
    label_counts: Dict[str, int],
    trends: Dict[str, Any],
) -> str:
    if active_goal and active_goal.get("focus_label"):
        return str(active_goal["focus_label"])
    return first_key(label_counts) or first_key(trends.get("labels") or {}) or "death review discipline"


def build_review_order(
    deaths: List[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    focus: str,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    scored_deaths = sorted(deaths, key=lambda death: death_priority(death, focus), reverse=True)
    for index, death in enumerate(scored_deaths[:5], start=1):
        labels = death.get("mistake_labels") or []
        primary = labels[0] if labels else "unlabeled death"
        items.append(
            {
                "rank": index,
                "kind": "marked_death",
                "death_id": death["id"],
                "timestamp": death.get("timestamp"),
                "title": f"Review death #{death['id']} at {format_ts(death.get('timestamp'))}",
                "reason": review_reason(death, focus),
                "pause_question": pause_question(primary),
                "coach_action": coach_action(primary),
                "has_advice": bool(death.get("advice")),
            }
        )
    next_rank = len(items) + 1
    for suggestion in suggestions[: max(0, 5 - len(items))]:
        items.append(
            {
                "rank": next_rank,
                "kind": "death_candidate",
                "suggestion_id": suggestion["id"],
                "timestamp": suggestion.get("timestamp"),
                "title": f"Check candidate at {format_ts(suggestion.get('timestamp'))}",
                "reason": suggestion.get("reason") or "The detector found a likely death/combat transition.",
                "pause_question": "Is this a real death? If yes, what decision made the fight unfavorable?",
                "coach_action": "Accept it if it is a real death, reject it if it is noise. This teaches the detector.",
                "has_advice": False,
            }
        )
        next_rank += 1
    if not items:
        items.append(
            {
                "rank": 1,
                "kind": "setup",
                "title": "Create the first review point",
                "reason": "No deaths or detector candidates are available for this match yet.",
                "pause_question": "Watch the VOD until your first death, then mark the timestamp and write what happened.",
                "coach_action": "Use Find Deaths if ffmpeg is available, or manually add the first death marker.",
                "has_advice": False,
            }
        )
    return items


def death_priority(death: Dict[str, Any], focus: str) -> float:
    labels = death.get("mistake_labels") or []
    score = float(death.get("confidence") or 0)
    if focus in labels:
        score += 2.0
    if death.get("advice"):
        score += 0.4
    if death.get("vision") or death.get("understanding"):
        score += 0.3
    if not labels or labels == ["needs manual review"]:
        score -= 0.5
    return score


def review_reason(death: Dict[str, Any], focus: str) -> str:
    labels = death.get("mistake_labels") or []
    if focus in labels:
        return f"This matches your current focus: {focus}."
    if labels:
        return f"This is tagged as {', '.join(labels[:2])}."
    return "This death needs a sharper label before the coach can learn from it."


def pause_question(primary: str) -> str:
    questions = {
        "dry peek": "Pause two seconds before contact: what info, utility, or trade made this peek safe?",
        "crosshair too low/wide": "Pause before the swing: where should the crosshair already be?",
        "exposed to multiple angles": "Pause before entering space: which second angle can punish you?",
        "poor reposition after contact": "Pause after first contact: where is the reset or off-angle?",
        "isolated from team": "Pause before the duel: who can trade you?",
        "late rotation / bad timing": "Pause before rotating: what confirmed info says move now?",
        "utility unused before taking space": "Pause before taking space: which ability should be spent first?",
    }
    return questions.get(primary, "Pause before the death: what was the last safer decision available?")


def coach_action(primary: str) -> str:
    template = ADVICE_BY_LABEL.get(primary)
    if template:
        return template["better_play"]
    return "Write one better decision, then tag the death so future reviews become more personalized."


def guided_summary(
    match: Dict[str, Any],
    deaths: List[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    focus: str,
    generated_advice: List[Dict[str, Any]],
) -> str:
    map_name = match.get("map") or "unknown map"
    agent = match.get("agent") or "unknown agent"
    advice_text = f" Generated {len(generated_advice)} new advice item(s)." if generated_advice else ""
    return (
        f"Coach mode for {map_name} as {agent}: review {len(deaths)} marked death(s), "
        f"check {len(suggestions)} pending candidate(s), and keep the session focus on {focus}."
        f"{advice_text}"
    )


def coach_read(
    match: Dict[str, Any],
    deaths: List[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    label_counts: Dict[str, int],
    focus: str,
    review: Dict[str, Any],
) -> str:
    if deaths:
        top = first_key(label_counts) or "unlabeled deaths"
        return (
            f"The coach read is not to review every tool output. Start with the highest-signal deaths, "
            f"look for the repeated decision pattern, then compare against the focus. Current top pattern: {top}. "
            f"Match review says: {review.get('next_action')}"
        )
    if suggestions:
        return "The first coaching task is verification: accept real death candidates and reject noise so the agent can learn your recording style."
    return "There is not enough evidence yet. The coach needs at least one marked death or accepted detector candidate before it can give personal feedback."


def between_round_rule(focus: str) -> str:
    template = ADVICE_BY_LABEL.get(focus)
    if template:
        return template["better_play"]
    return "Before each committed fight, name your advantage: info, utility, trade, timing, or position."


def build_homework(
    focus: str,
    match: Dict[str, Any],
    profile: Dict[str, Any],
    review_order: List[Dict[str, Any]],
) -> List[str]:
    agent = match.get("agent") or (profile.get("main_agents") or ["your agent"])[0]
    base = [
        f"Review the first {min(3, len(review_order))} coach-selected item(s), not the whole VOD.",
        f"For each item, answer the pause question before reading the coach action.",
        f"Next match as {agent}, use one rule only: {between_round_rule(focus)}",
    ]
    return base


def guided_confidence(
    deaths: List[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    generated_advice: List[Dict[str, Any]],
) -> float:
    if deaths and generated_advice:
        return 0.78
    if deaths:
        return 0.68
    if suggestions:
        return 0.45
    return 0.25


def count_labels(deaths: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for death in deaths:
        for label in death.get("mistake_labels") or []:
            if label == "needs manual review":
                continue
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))


def build_review_summary(match: Dict[str, Any], total_deaths: int, top_label: str, focus_read: str) -> str:
    map_name = match.get("map") or "unknown map"
    agent = match.get("agent") or "unknown agent"
    return f"On {map_name} as {agent}, {total_deaths} death(s) were marked. {focus_read} The clearest pattern is {top_label}."


def build_coach_note(profile: Dict[str, Any], advice_count: int, accepted_count: int) -> str:
    style = profile.get("target_style") or "more disciplined fights"
    if advice_count:
        return f"You accepted {accepted_count}/{advice_count} generated advice item(s). Keep the next session narrow: {style}."
    return f"Generate advice on the marked deaths before the next session so the coach can learn which reads are useful. Target style: {style}."


def review_score(total_deaths: int, focus_deaths: int, advice_count: int) -> int:
    if total_deaths == 0:
        return 0
    focus_penalty = min(50, int((focus_deaths / total_deaths) * 50))
    advice_bonus = min(25, advice_count * 5)
    return max(0, min(100, 70 - focus_penalty + advice_bonus))


def build_session_plan(
    profile: Dict[str, Any],
    active_goal: Optional[Dict[str, Any]],
    trends: Dict[str, Any],
    feedback: Dict[str, Any],
) -> Dict[str, Any]:
    labels = trends.get("labels") or {}
    focus = active_goal["focus_label"] if active_goal else first_key(labels) or "death review discipline"
    count = int(labels.get(focus, 0))
    main_agents = profile.get("main_agents") or []
    agent = main_agents[0] if main_agents else first_key(trends.get("by_agent") or {}) or "your main agent"
    rank = profile.get("rank") or "unranked"
    template = ADVICE_BY_LABEL.get(focus)

    if template:
        in_game_rule = template["better_play"]
        drill = template["drill"]
    else:
        in_game_rule = "Before each committed fight, name your trade, escape, or utility advantage."
        drill = "After your session, review the first five deaths and write the decision that made each fight unfavorable."

    recent = trends.get("matches") or []
    recent_count = len(recent[:5])
    recent_focus_deaths = sum((match.get("labels") or {}).get(focus, 0) for match in recent[:5])
    accepted = int(feedback.get("accepted") or 0)
    rejected = int(feedback.get("rejected") or 0)

    if count:
        why = f"'{focus}' is your most useful current focus with {count} tagged occurrence(s)."
    else:
        why = "There is not enough labeled history yet, so the focus is to build clean review data."

    if active_goal:
        summary = f"Stay on the active focus: {focus}."
    else:
        summary = f"Next session focus: reduce {focus}."

    return {
        "focus_label": focus,
        "summary": summary,
        "why": why,
        "profile_context": f"Rank: {rank}. Primary agent context: {agent}.",
        "in_game_rule": in_game_rule,
        "review_rule": "After each match, mark every death that matches the focus before adding any other labels.",
        "drill": drill,
        "target": "Play 2 matches with this single focus, then compare focus deaths per match.",
        "progress": {
            "recent_matches": recent_count,
            "recent_focus_deaths": recent_focus_deaths,
            "accepted_advice": accepted,
            "rejected_advice": rejected,
        },
    }


def build_personal_memory(
    db: Database,
    trends: Dict[str, Any],
    feedback: Dict[str, Any],
    suggestion_learning: Dict[str, Any],
) -> Dict[str, Any]:
    analyses = db.list_structured_analyses(limit=40)
    labels = trends.get("labels") or {}
    top_label = first_key(labels) or "not enough labeled deaths"
    accepted = int(feedback.get("accepted") or 0)
    rejected = int(feedback.get("rejected") or 0)
    detector_rate = suggestion_learning.get("acceptance_rate", 0)

    clip_reads = [
        item.get("payload") or {}
        for item in analyses
        if item.get("analysis_type") == "clip_understanding"
    ]
    match_reads = {
        item.get("analysis_type"): item.get("payload") or {}
        for item in analyses
        if item.get("subject_type") == "match"
    }
    crosshair = match_reads.get("crosshair") or {}
    minimap = match_reads.get("minimap") or {}
    event_v2 = match_reads.get("death_events_v2") or {}
    persistent = memory_dashboard_overlay(load_coach_memory_state(db))

    learned = []
    learned.append(f"Most repeated labeled issue: {top_label}.")
    if accepted or rejected:
        learned.append(f"Advice feedback: {accepted} accepted, {rejected} rejected.")
    if detector_rate:
        learned.append(f"Death-suggestion acceptance rate: {detector_rate}.")
    if crosshair:
        learned.append(crosshair.get("summary") or "Crosshair scoring has recent samples.")
    if minimap:
        learned.append(minimap.get("summary") or "Minimap analysis has recent samples.")
    if event_v2:
        learned.append(event_v2.get("summary") or "Death detector v2 has recent samples.")
    if clip_reads:
        learned.append(f"{len(clip_reads)} recent clip-understanding read(s) are available.")
    if persistent["persistent_review_count"]:
        learned.append(
            f"Personal coach memory has learned from {persistent['persistent_review_count']} completed Local AI clip review(s)."
        )

    priorities = []
    if "crosshair" in top_label or float((crosshair.get("metrics") or {}).get("average_crosshair_activity") or 0) > 0.10:
        priorities.append("Reduce crosshair correction before first contact.")
    if "late rotation" in top_label or "minimap" in (minimap.get("summary") or "").lower():
        priorities.append("Review minimap timing before rotations and retakes.")
    if "dry peek" in top_label:
        priorities.append("Require utility, info, or trade timing before committed first contact.")
    for rule in persistent["learned_rules"]:
        if rule not in priorities:
            priorities.append(rule)
    if not priorities:
        priorities.append("Keep collecting labeled deaths and accept/reject coach advice to sharpen personalization.")

    return {
        "summary": " ".join(learned),
        "top_label": persistent["current_focus"] or top_label,
        "priorities": priorities[:4],
        "recent_clip_reads": len(clip_reads),
        "analysis_count": len(analyses),
        "persistent": persistent,
    }


def build_session_outcomes(db: Database, trends: Dict[str, Any], memory: Dict[str, Any]) -> Dict[str, Any]:
    recent = trends.get("matches") or []
    focus = memory.get("top_label") or first_key(trends.get("labels") or {}) or ""
    focus_by_match = [
        {
            "match_id": item["match_id"],
            "focus_deaths": int((item.get("labels") or {}).get(focus, 0)),
            "death_count": int(item.get("death_count") or 0),
        }
        for item in recent[:6]
    ]
    analyses = db.list_structured_analyses("match", limit=20)
    crosshair_scores = [
        int((item.get("payload") or {}).get("score") or 0)
        for item in analyses
        if item.get("analysis_type") == "crosshair" and (item.get("payload") or {}).get("score") is not None
    ]
    minimap_reads = [
        (item.get("payload") or {}).get("spacing_read") or {}
        for item in analyses
        if item.get("analysis_type") == "minimap"
    ]
    detector = db.detector_feedback_summary()
    return {
        "focus_label": focus,
        "focus_by_match": focus_by_match,
        "crosshair_average": round(sum(crosshair_scores) / len(crosshair_scores), 1) if crosshair_scores else None,
        "minimap_risks": [read.get("risk") for read in minimap_reads[:5] if read],
        "detector_feedback": detector,
        "summary": outcome_summary(focus, focus_by_match, crosshair_scores, detector),
    }


def outcome_summary(
    focus: str,
    focus_by_match: List[Dict[str, Any]],
    crosshair_scores: List[int],
    detector: Dict[str, Any],
) -> str:
    parts = []
    if focus_by_match:
        latest = focus_by_match[0]
        parts.append(f"Latest match has {latest['focus_deaths']} '{focus}' death(s).")
    if crosshair_scores:
        parts.append(f"Average crosshair score from recent analyses is {round(sum(crosshair_scores) / len(crosshair_scores), 1)}.")
    total_feedback = int(detector.get("accepted") or 0) + int(detector.get("rejected") or 0)
    if total_feedback:
        parts.append(f"Detector feedback has {detector.get('accepted', 0)} accepted and {detector.get('rejected', 0)} rejected candidate(s).")
    return " ".join(parts) if parts else "No measured outcomes yet. Run match analyses and accept/reject suggestions to build progress metrics."


def first_key(items: Dict[str, Any]) -> Optional[str]:
    for key in items:
        return key
    return None


def format_ts(value: Any) -> str:
    if value is None:
        return "unknown"
    seconds = int(float(value))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
