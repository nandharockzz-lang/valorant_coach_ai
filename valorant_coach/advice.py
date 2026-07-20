from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import Database


ADVICE_BY_LABEL = {
    "dry peek": {
        "what_happened": "You likely took first contact without forcing safer information, utility pressure, or a teammate ready to trade.",
        "better_play": "Before committing, jiggle for info or pair the swing with utility or a teammate's trade timing.",
        "drill": "In deathmatch, take 20 fights where you jiggle or shoulder-check before the full swing.",
    },
    "crosshair too low/wide": {
        "what_happened": "Your crosshair placement likely made the fight harder before the enemy appeared.",
        "better_play": "Pre-aim the next likely head-height angle before moving into the lane.",
        "drill": "Play one deathmatch where score does not matter and every corner must be pre-aimed at head height.",
    },
    "exposed to multiple angles": {
        "what_happened": "You entered a position where more than one enemy angle could punish you at the same time.",
        "better_play": "Slice the space into one fight at a time and use cover or utility to block the second angle.",
        "drill": "On the map in custom mode, walk your common routes and name each angle before you expose yourself.",
    },
    "poor reposition after contact": {
        "what_happened": "After first contact, you likely stayed in a predictable fight instead of resetting the duel.",
        "better_play": "After contact, break line of sight, change elevation, or move to a new off-angle before fighting again.",
        "drill": "In deathmatch, after every shot burst, strafe back to cover or move before repeeking.",
    },
    "isolated from team": {
        "what_happened": "The death likely happened before a teammate could trade or support the fight.",
        "better_play": "Wait for teammate spacing, utility, or a timing cue before taking the committed duel.",
        "drill": "For five ranked rounds, say who can trade you before you take first contact.",
    },
    "repeated same-angle fight": {
        "what_happened": "You likely challenged a known angle again without changing the fight condition.",
        "better_play": "Change timing, position, or utility before taking the same fight again.",
        "drill": "Review three rounds and write the alternative angle you could have used after first contact.",
    },
    "late rotation / bad timing": {
        "what_happened": "The death likely came from arriving late or moving after the enemy already controlled the timing.",
        "better_play": "Rotate on confirmed pressure and preserve a safe path instead of reacting after site collapse.",
        "drill": "During VOD review, pause after first contact and predict whether you should anchor, shade, or rotate.",
    },
    "utility unused before taking space": {
        "what_happened": "You likely took space while still holding utility that could have made the fight safer.",
        "better_play": "Spend one useful ability before first committed contact if the angle is contested.",
        "drill": "Pick one ability before each round that must be used before your first duel.",
    },
}


def generate_advice(db: Database, death_id: int) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        raise ValueError(f"Unknown death id: {death_id}")
    match = db.get_match(int(death["match_id"]))
    if not match:
        raise ValueError(f"Unknown match id: {death['match_id']}")

    labels = [label for label in death.get("mistake_labels", []) if label != "needs manual review"]
    primary = labels[0] if labels else infer_primary_from_notes(death.get("notes") or "")
    secondary = [label for label in labels[1:] if label != primary]
    template = ADVICE_BY_LABEL.get(primary, fallback_template(primary))
    vision = db.get_latest_clip_analysis(death_id)
    understanding = (death.get("understanding") or {}).get("payload") or {}
    rounds = db.get_rounds(int(death["match_id"]))

    source = advice_source(death)
    context = context_sentence(match, death, source, round_phase(rounds, death.get("timestamp")))
    visual_context = vision_context(vision)
    understanding_context = clip_understanding_context(understanding)
    playbook_context = map_agent_context(match, primary)
    payload = {
        "death_id": death_id,
        "provider": "local-coach",
        "source": source,
        "primary_mistake": primary,
        "secondary_mistakes": secondary,
        "what_happened": f"{context} {template['what_happened']}{visual_context}{understanding_context}{playbook_context}",
        "better_play": round_aware_better_play(template["better_play"], primary, round_phase(rounds, death.get("timestamp"))),
        "drill": map_agent_drill(template["drill"], match, primary),
        "confidence": max(
            float(death.get("confidence") or 0),
            float((vision or {}).get("confidence") or 0),
            0.55 if labels else 0.35,
        ),
    }
    advice_id = db.save_advice(payload)
    payload["id"] = advice_id
    return payload


def advice_source(death: Dict[str, Any]) -> str:
    clip_path = death.get("clip_path")
    if clip_path and Path(clip_path).exists():
        return "clip"
    if death.get("timestamp") is not None:
        return "vod-timestamp"
    return "manual-context"


def context_sentence(match: Dict[str, Any], death: Dict[str, Any], source: str, phase: str) -> str:
    map_name = match.get("map") or "unknown map"
    agent = match.get("agent") or "unknown agent"
    round_number = death.get("round_number") or "?"
    timestamp = format_ts(death.get("timestamp"))
    basis = {
        "clip": "Using the extracted death clip and marker context,",
        "vod-timestamp": "Using the VOD timestamp and marker context,",
        "manual-context": "Using the manual marker context,",
    }[source]
    return f"{basis} this {phase} R{round_number} death at {timestamp} on {map_name} as {agent} points to:"


def infer_primary_from_notes(notes: str) -> str:
    text = notes.lower()
    if "repeek" in text or "same angle" in text:
        return "poor reposition after contact"
    if "alone" in text or "trade" in text:
        return "isolated from team"
    if "utility" in text or "smoke" in text or "flash" in text:
        return "utility unused before taking space"
    if "mid" in text or "peek" in text or "swing" in text:
        return "dry peek"
    return "review required"


def fallback_template(primary: str) -> Dict[str, str]:
    return {
        "what_happened": f"The marker suggests '{primary}', but there is not enough structured evidence for a sharper diagnosis.",
        "better_play": "Replay the clip, identify the last safe position, and write the decision that made the fight unfavorable.",
        "drill": "Review five similar deaths and group them by timing, angle exposure, teammate spacing, or utility usage.",
    }


def vision_context(vision: Optional[Dict[str, Any]]) -> str:
    if not vision:
        return ""
    observations = vision.get("observations") or []
    if not observations:
        return ""
    return " Visual read: " + " ".join(str(item) for item in observations[:2])


def clip_understanding_context(understanding: Dict[str, Any]) -> str:
    if not understanding:
        return ""
    parts = []
    if understanding.get("minimap_read"):
        parts.append(str(understanding["minimap_read"]))
    if understanding.get("crosshair_read"):
        parts.append(str(understanding["crosshair_read"]))
    return " Clip understanding: " + " ".join(parts[:2]) if parts else ""


def map_agent_context(match: Dict[str, Any], primary: str) -> str:
    map_name = (match.get("map") or "").lower()
    agent = (match.get("agent") or "").lower()
    notes = []
    if agent == "jett" and primary in {"dry peek", "poor reposition after contact"}:
        notes.append(" As Jett, your dash/updraft is only useful if it is planned before first contact, not after the duel is already lost.")
    if map_name == "ascent" and primary in {"dry peek", "exposed to multiple angles"}:
        notes.append(" On Ascent, mid and lane fights punish wide untraded exposure, so isolate one lane before committing.")
    return "".join(notes)


def round_aware_better_play(base: str, primary: str, phase: str) -> str:
    if phase == "early round" and primary in {"dry peek", "utility unused before taking space"}:
        return base + " In early round, value information and survival over a fast committed duel."
    if phase == "late round" and primary in {"late rotation / bad timing", "isolated from team"}:
        return base + " In late round, preserve trade spacing and avoid solo timing fights."
    return base


def map_agent_drill(base: str, match: Dict[str, Any], primary: str) -> str:
    agent = (match.get("agent") or "").lower()
    map_name = match.get("map") or "the map"
    if agent == "jett" and primary in {"dry peek", "poor reposition after contact"}:
        return base + f" Then run {map_name} custom routes and pre-call your escape before each first-contact angle."
    return base


def round_phase(rounds: List[Dict[str, Any]], timestamp: Any) -> str:
    if timestamp is None:
        return "unknown phase"
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
    return "unknown phase"


def format_ts(value: Any) -> str:
    if value is None:
        return "unknown time"
    seconds = int(float(value))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
