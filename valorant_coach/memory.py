import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .db import Database


MEMORY_SETTING = "coach_memory_state"
MAX_RECENT_REVIEWS = 20
MAX_LESSONS = 12


def load_coach_memory_state(db: Database) -> Dict[str, Any]:
    raw = db.get_setting(MEMORY_SETTING, "")
    if raw:
        try:
            state = json.loads(raw)
            if isinstance(state, dict):
                return normalize_memory_state(state)
        except json.JSONDecodeError:
            pass
    return empty_memory_state()


def save_coach_memory_state(db: Database, state: Dict[str, Any]) -> None:
    db.set_setting(MEMORY_SETTING, json.dumps(normalize_memory_state(state)))


def update_coach_memory_from_review(db: Database, death: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    if str(review.get("status") or "") != "completed":
        return load_coach_memory_state(db)

    state = load_coach_memory_state(db)
    match = db.get_match(int(death.get("match_id") or 0)) or {}
    context = latest_payload(db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_correction")) if death.get("id") else {}
    training = latest_payload(db.get_latest_structured_analysis("death", int(death.get("id") or 0), "clip_training_label")) if death.get("id") else {}
    perception = review.get("perception") or {}
    coaching = review.get("coaching") or {}
    weapon = context.get("weapon") or perception.get("weapon_seen") or review.get("weapon") or "unknown"
    map_name = context.get("map") or match.get("map") or "unknown"
    agent_name = context.get("agent") or match.get("agent") or "unknown"
    labels = normalize_text_list(review.get("labels") or []) + normalize_text_list(death.get("mistake_labels") or [])
    labels = unique_keep_order([label.lower() for label in labels if label and label.lower() != "needs manual review"])
    if not labels:
        labels = ["unlabeled visual review"]

    for label in labels:
        increment(state["label_counts"], label)
    if map_name and map_name != "unknown":
        increment_nested(state["map_label_counts"], str(map_name), labels)
    if agent_name and agent_name != "unknown":
        increment_nested(state["agent_label_counts"], str(agent_name), labels)
    if weapon and weapon != "unknown":
        increment_nested(state["weapon_label_counts"], str(weapon), labels)
    increment_coaching_dimensions(state, coaching, perception)
    increment_training_memory(state, training, review)

    lesson = extract_review_lesson(review)
    if lesson:
        add_unique_lesson(state, lesson)

    recent = {
        "death_id": int(death.get("id") or 0),
        "match_id": int(death.get("match_id") or 0),
        "map": map_name,
        "agent": agent_name,
        "weapon": weapon,
        "round_number": death.get("round_number"),
        "timestamp": death.get("timestamp"),
        "labels": labels[:5],
        "summary": str(review.get("summary") or "")[:260],
        "better_play": str(review.get("better_play") or "")[:220],
        "first_mistake": str(review.get("first_mistake") or coaching.get("first_mistake") or "")[:180],
        "review_quality": review.get("review_quality") or {},
        "training_label": {
            "correct_mistake_label": training.get("correct_mistake_label") or "",
            "crosshair_issue": training.get("crosshair_issue"),
            "has_frame_labels": bool(training.get("enemy_visible_frame") or training.get("first_contact_frame") or training.get("death_frame")),
        },
        "confidence": float(review.get("confidence") or 0),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    state["recent_reviews"] = [recent] + [
        item for item in state["recent_reviews"] if int(item.get("death_id") or 0) != recent["death_id"]
    ]
    state["recent_reviews"] = state["recent_reviews"][:MAX_RECENT_REVIEWS]
    state["review_count"] = int(state.get("review_count") or 0) + 1
    state["current_focus"] = top_key(state["label_counts"]) or labels[0]
    state["prompt_rules"] = build_prompt_rules(state)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_coach_memory_state(db, state)
    return state


def build_memory_prompt_context(db: Database, max_chars: int = 1400) -> str:
    state = load_coach_memory_state(db)
    if int(state.get("review_count") or 0) <= 0:
        return (
            "Personal coach memory: no completed local AI clip reviews have been learned yet. "
            "After this review, extract reusable coaching patterns for future clips."
        )

    top_labels = top_items(state.get("label_counts") or {}, 5)
    top_dimensions = top_items(state.get("dimension_counts") or {}, 5)
    recent = state.get("recent_reviews") or []
    recent_lines = [
        f"- {item.get('map')}/{item.get('agent')}/{item.get('weapon', 'unknown')} {format_ts(item.get('timestamp'))}: {', '.join(item.get('labels') or [])}. Better play: {item.get('better_play') or 'not recorded'}"
        for item in recent[:3]
    ]
    rules = [f"- {rule}" for rule in state.get("prompt_rules", [])[:5]]
    lessons = [f"- {lesson}" for lesson in state.get("lessons", [])[:4]]
    feedback = review_feedback_prompt_lines(db)
    context = "\n".join(
        [
            "Personal coach memory, stored locally:",
            f"- Completed learned clip reviews: {int(state.get('review_count') or 0)}",
            f"- Current personal focus: {state.get('current_focus') or 'not enough data'}",
            f"- Repeated issues: {', '.join(f'{label} x{count}' for label, count in top_labels) or 'not enough data'}",
            f"- Repeated dimensions: {', '.join(f'{label} x{count}' for label, count in top_dimensions) or 'not enough data'}",
            *memory_specific_pattern_lines(state),
            "Reusable rules:",
            *(rules or ["- Build the read from visible evidence before giving advice."]),
            "Recent personal examples:",
            *(recent_lines or ["- No recent examples yet."]),
            "Learned coach notes:",
            *(lessons or ["- No durable lesson yet."]),
            "User feedback on Clip Coach reviews:",
            *(feedback or ["- No Clip Coach feedback yet."]),
        ]
    )
    return context[:max_chars]


def memory_dashboard_overlay(state: Dict[str, Any]) -> Dict[str, Any]:
    state = normalize_memory_state(state)
    top_labels = top_items(state.get("label_counts") or {}, 4)
    return {
        "persistent_review_count": int(state.get("review_count") or 0),
        "persistent_updated_at": state.get("updated_at") or "",
        "current_focus": state.get("current_focus") or "",
        "top_patterns": [{"label": label, "count": count} for label, count in top_labels],
        "map_patterns": top_nested_patterns(state.get("map_label_counts") or {}, 5),
        "agent_patterns": top_nested_patterns(state.get("agent_label_counts") or {}, 5),
        "weapon_patterns": top_nested_patterns(state.get("weapon_label_counts") or {}, 5),
        "dimension_patterns": [{"label": label, "count": count} for label, count in top_items(state.get("dimension_counts") or {}, 6)],
        "correction_patterns": [{"label": label, "count": count} for label, count in top_items(state.get("correction_counts") or {}, 6)],
        "perception_patterns": [{"label": label, "count": count} for label, count in top_items(state.get("perception_counts") or {}, 6)],
        "learned_rules": state.get("prompt_rules", [])[:5],
        "recent_lessons": state.get("lessons", [])[:5],
        "recent_reviews": state.get("recent_reviews", [])[:8],
    }


def empty_memory_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "review_count": 0,
        "current_focus": "",
        "label_counts": {},
        "map_label_counts": {},
        "agent_label_counts": {},
        "weapon_label_counts": {},
        "dimension_counts": {},
        "correction_counts": {},
        "perception_counts": {},
        "lessons": [],
        "prompt_rules": [],
        "recent_reviews": [],
    }


def normalize_memory_state(state: Dict[str, Any]) -> Dict[str, Any]:
    base = empty_memory_state()
    base.update({key: value for key, value in state.items() if key in base})
    base["review_count"] = int(base.get("review_count") or 0)
    for key in ("label_counts", "map_label_counts", "agent_label_counts", "weapon_label_counts", "dimension_counts", "correction_counts", "perception_counts"):
        base[key] = base[key] if isinstance(base.get(key), dict) else {}
    for key in ("lessons", "prompt_rules", "recent_reviews"):
        base[key] = base[key] if isinstance(base.get(key), list) else []
    base["lessons"] = [str(item) for item in base["lessons"] if str(item).strip()][:MAX_LESSONS]
    base["prompt_rules"] = [str(item) for item in base["prompt_rules"] if str(item).strip()][:8]
    base["recent_reviews"] = [item for item in base["recent_reviews"] if isinstance(item, dict)][:MAX_RECENT_REVIEWS]
    return base


def build_prompt_rules(state: Dict[str, Any]) -> List[str]:
    rules = []
    for label, _count in top_items(state.get("label_counts") or {}, 5):
        rules.append(rule_for_label(label))
    for lesson in state.get("lessons", [])[:4]:
        if lesson and lesson not in rules:
            rules.append(lesson)
    return unique_keep_order(rules)[:8]


def memory_specific_pattern_lines(state: Dict[str, Any]) -> List[str]:
    lines = []
    for title, key in (("Map patterns", "map_label_counts"), ("Agent patterns", "agent_label_counts"), ("Weapon patterns", "weapon_label_counts")):
        patterns = top_nested_patterns(state.get(key) or {}, 3)
        if patterns:
            lines.append(f"- {title}: " + ", ".join(f"{item['bucket']} -> {item['label']} x{item['count']}" for item in patterns))
    corrections = top_items(state.get("correction_counts") or {}, 3)
    if corrections:
        lines.append("- Corrected coach labels: " + ", ".join(f"{label} x{count}" for label, count in corrections))
    perceptions = top_items(state.get("perception_counts") or {}, 3)
    if perceptions:
        lines.append("- Perception issues: " + ", ".join(f"{label} x{count}" for label, count in perceptions))
    return lines[:5]


def increment_coaching_dimensions(state: Dict[str, Any], coaching: Dict[str, Any], perception: Dict[str, Any]) -> None:
    checks = {
        "utility": coaching.get("utility_issue"),
        "crosshair": coaching.get("crosshair_issue") or perception.get("crosshair_alignment"),
        "positioning": coaching.get("positioning_issue") or perception.get("peek_type"),
        "mechanics": coaching.get("mechanical_issue") or perception.get("movement_state"),
        "enemy_contact": perception.get("enemy_seen"),
    }
    for key, value in checks.items():
        text = str(value or "").strip().lower()
        if text and text not in {"unknown", "none", "no", "false"}:
            increment(state["dimension_counts"], key)


def increment_training_memory(state: Dict[str, Any], training: Dict[str, Any], review: Dict[str, Any]) -> None:
    if not training:
        return
    label = str(training.get("correct_mistake_label") or "").strip().lower()
    if label:
        increment(state["correction_counts"], label)
    if training.get("crosshair_issue") is True:
        increment(state["perception_counts"], "confirmed crosshair issue")
    if training.get("enemy_visible_frame") is not None:
        increment(state["perception_counts"], "enemy frame manually labeled")
    if training.get("first_contact_frame") is not None:
        increment(state["perception_counts"], "contact frame manually labeled")
    if training.get("death_frame") is not None:
        increment(state["perception_counts"], "death frame manually labeled")
    quality = (review.get("review_quality") or {}).get("summary")
    if quality:
        increment(state["perception_counts"], f"review quality: {quality}")


def rule_for_label(label: str) -> str:
    lower = label.lower()
    if "dry peek" in lower or "utility" in lower:
        return "Before first contact, check whether the peek had info, utility, trade timing, or an escape."
    if "crosshair" in lower:
        return "Score whether the crosshair was already at likely head height before the enemy appeared."
    if "reposition" in lower or "same-angle" in lower or "repeek" in lower:
        return "After contact, look for a reset, angle change, or cover break before the next fight."
    if "multiple angles" in lower or "exposed" in lower:
        return "Check whether the player isolated one angle or exposed themselves to two sightlines at once."
    if "team" in lower or "isolated" in lower:
        return "Check teammate spacing and whether the fight was tradeable from the minimap/HUD evidence."
    if "rotation" in lower or "timing" in lower:
        return "Review minimap/timer context before judging a rotate or late-round timing choice."
    return f"Compare the clip against the recurring personal pattern: {label}."


def extract_review_lesson(review: Dict[str, Any]) -> str:
    better = str(review.get("better_play") or "").strip()
    if better:
        return better[:260]
    drill = str(review.get("drill") or "").strip()
    if drill:
        return drill[:260]
    summary = str(review.get("summary") or "").strip()
    return summary[:220]


def add_unique_lesson(state: Dict[str, Any], lesson: str) -> None:
    normalized = lesson.strip()
    if not normalized:
        return
    existing = [item for item in state.get("lessons", []) if item.strip().lower() != normalized.lower()]
    state["lessons"] = [normalized] + existing
    state["lessons"] = state["lessons"][:MAX_LESSONS]


def normalize_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def unique_keep_order(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def increment(counts: Dict[str, int], key: str, amount: int = 1) -> None:
    counts[key] = int(counts.get(key) or 0) + amount


def increment_nested(counts: Dict[str, Dict[str, int]], key: str, labels: List[str]) -> None:
    bucket = counts.setdefault(key, {})
    for label in labels:
        increment(bucket, label)


def top_key(counts: Dict[str, int]) -> Optional[str]:
    items = top_items(counts, 1)
    return items[0][0] if items else None


def top_items(counts: Dict[str, int], limit: int) -> List[tuple]:
    return sorted(
        [(str(key), int(value or 0)) for key, value in counts.items() if int(value or 0) > 0],
        key=lambda item: (-item[1], item[0]),
    )[:limit]


def top_nested_patterns(counts: Dict[str, Dict[str, int]], limit: int) -> List[Dict[str, Any]]:
    rows = []
    for bucket, labels in counts.items():
        if not isinstance(labels, dict):
            continue
        for label, count in labels.items():
            if int(count or 0) > 0:
                rows.append({"bucket": str(bucket), "label": str(label), "count": int(count or 0)})
    rows.sort(key=lambda item: (-item["count"], item["bucket"], item["label"]))
    return rows[:limit]


def review_feedback_prompt_lines(db: Database) -> List[str]:
    rows = [
        item for item in db.list_structured_analyses("death", limit=500)
        if item.get("analysis_type") == "clip_review_feedback"
    ]
    if not rows:
        return []
    counts: Dict[str, int] = {}
    notes = []
    for row in rows:
        payload = row.get("payload") or {}
        verdict = str(payload.get("verdict") or "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
        if payload.get("note"):
            notes.append(str(payload.get("note"))[:180])
    lines = ["- Verdicts: " + ", ".join(f"{key} x{value}" for key, value in sorted(counts.items()))]
    if int(counts.get("wrong") or 0) + int(counts.get("not_useful") or 0):
        lines.append("- The user has rejected some reviews; be conservative, cite exact visible evidence, and lower confidence when unsure.")
    if notes:
        lines.append("- Recent corrections: " + " | ".join(notes[:3]))
    return lines[:4]


def latest_payload(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    payload = row.get("payload") if isinstance(row, dict) else {}
    return payload if isinstance(payload, dict) else {}


def format_ts(value: Any) -> str:
    if value is None:
        return "unknown time"
    seconds = max(0, int(float(value)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
