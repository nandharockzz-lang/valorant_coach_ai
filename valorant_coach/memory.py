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
    labels = normalize_text_list(review.get("labels") or []) + normalize_text_list(death.get("mistake_labels") or [])
    labels = unique_keep_order([label.lower() for label in labels if label and label.lower() != "needs manual review"])
    if not labels:
        labels = ["unlabeled visual review"]

    for label in labels:
        increment(state["label_counts"], label)
    if match.get("map"):
        increment_nested(state["map_label_counts"], str(match["map"]), labels)
    if match.get("agent"):
        increment_nested(state["agent_label_counts"], str(match["agent"]), labels)

    lesson = extract_review_lesson(review)
    if lesson:
        add_unique_lesson(state, lesson)

    recent = {
        "death_id": int(death.get("id") or 0),
        "match_id": int(death.get("match_id") or 0),
        "map": match.get("map") or "unknown",
        "agent": match.get("agent") or "unknown",
        "round_number": death.get("round_number"),
        "timestamp": death.get("timestamp"),
        "labels": labels[:5],
        "summary": str(review.get("summary") or "")[:260],
        "better_play": str(review.get("better_play") or "")[:220],
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
    recent = state.get("recent_reviews") or []
    recent_lines = [
        f"- {item.get('map')}/{item.get('agent')} {format_ts(item.get('timestamp'))}: {', '.join(item.get('labels') or [])}. Better play: {item.get('better_play') or 'not recorded'}"
        for item in recent[:3]
    ]
    rules = [f"- {rule}" for rule in state.get("prompt_rules", [])[:5]]
    lessons = [f"- {lesson}" for lesson in state.get("lessons", [])[:4]]
    context = "\n".join(
        [
            "Personal coach memory, stored locally:",
            f"- Completed learned clip reviews: {int(state.get('review_count') or 0)}",
            f"- Current personal focus: {state.get('current_focus') or 'not enough data'}",
            f"- Repeated issues: {', '.join(f'{label} x{count}' for label, count in top_labels) or 'not enough data'}",
            "Reusable rules:",
            *(rules or ["- Build the read from visible evidence before giving advice."]),
            "Recent personal examples:",
            *(recent_lines or ["- No recent examples yet."]),
            "Learned coach notes:",
            *(lessons or ["- No durable lesson yet."]),
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
        "learned_rules": state.get("prompt_rules", [])[:5],
        "recent_lessons": state.get("lessons", [])[:5],
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
        "lessons": [],
        "prompt_rules": [],
        "recent_reviews": [],
    }


def normalize_memory_state(state: Dict[str, Any]) -> Dict[str, Any]:
    base = empty_memory_state()
    base.update({key: value for key, value in state.items() if key in base})
    base["review_count"] = int(base.get("review_count") or 0)
    for key in ("label_counts", "map_label_counts", "agent_label_counts"):
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


def format_ts(value: Any) -> str:
    if value is None:
        return "unknown time"
    seconds = max(0, int(float(value)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
