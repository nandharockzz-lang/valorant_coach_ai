import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import request as urlrequest

from .db import Database


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KNOWLEDGE_ROOT = ROOT / "knowledge"
INDEX_VERSION = 1
VALORANT_API_BASE = "https://valorant-api.com/v1"
REMOTE_ENDPOINTS = {
    "agents": "/agents?isPlayableCharacter=true",
    "maps": "/maps",
    "weapons": "/weapons",
    "gamemodes": "/gamemodes",
    "competitivetiers": "/competitivetiers",
}
DEFAULT_MAPS = ["Abyss", "Ascent", "Bind", "Breeze", "Corrode", "Fracture", "Haven", "Icebox", "Lotus", "Pearl", "Split", "Sunset"]
DEFAULT_AGENTS = [
    "Astra", "Breach", "Brimstone", "Chamber", "Clove", "Cypher", "Deadlock", "Fade", "Gekko", "Harbor",
    "Iso", "Jett", "KAY/O", "Killjoy", "Neon", "Omen", "Phoenix", "Raze", "Reyna", "Sage", "Skye",
    "Sova", "Tejo", "Viper", "Vyse", "Waylay", "Yoru",
]
DEFAULT_WEAPONS = [
    "Classic", "Shorty", "Frenzy", "Ghost", "Sheriff", "Stinger", "Spectre", "Bucky", "Judge", "Bulldog",
    "Guardian", "Phantom", "Vandal", "Marshal", "Outlaw", "Operator", "Ares", "Odin", "Knife",
]


def knowledge_status(root: Path = DEFAULT_KNOWLEDGE_ROOT) -> Dict[str, Any]:
    index = load_index(root)
    if not index:
        return {
            "ok": True,
            "ready": False,
            "root": str(root),
            "summary": "Knowledge base has not been built yet.",
            "counts": {},
            "last_built_at": "",
            "sources": [],
        }
    return {
        "ok": True,
        "ready": True,
        "root": str(root),
        "summary": index.get("summary") or "",
        "counts": index.get("counts") or {},
        "last_built_at": index.get("built_at") or "",
        "sources": index.get("sources") or [],
        "snippet_count": len(index.get("snippets") or []),
    }


def rebuild_knowledge_base(root: Path = DEFAULT_KNOWLEDGE_ROOT, fetch_remote: bool = True) -> Dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "curated").mkdir(parents=True, exist_ok=True)
    ensure_default_curated_files(root)

    raw_payloads: Dict[str, Any] = {}
    errors = []
    if fetch_remote:
        for name, endpoint in REMOTE_ENDPOINTS.items():
            try:
                raw_payloads[name] = fetch_valorant_api(endpoint)
                write_json(root / "raw" / f"{name}.json", raw_payloads[name])
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                cached = read_json(root / "raw" / f"{name}.json")
                if cached:
                    raw_payloads[name] = cached
    else:
        for name in REMOTE_ENDPOINTS:
            cached = read_json(root / "raw" / f"{name}.json")
            if cached:
                raw_payloads[name] = cached

    curated = load_curated_snippets(root)
    snippets = curated + build_structured_snippets(raw_payloads)
    for item in snippets:
        item["tokens"] = tokenize(" ".join(str(item.get(key) or "") for key in ("title", "topic", "text", "tags")))
    counts = count_by(snippets, "type")
    sources = sorted({str(item.get("source") or "local") for item in snippets})
    index = {
        "version": INDEX_VERSION,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "summary": f"{len(snippets)} VALORANT knowledge snippet(s): " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items())),
        "counts": counts,
        "sources": sources,
        "errors": errors,
        "snippets": snippets,
    }
    write_json(root / "index.json", index)
    return {"ok": True, "index": compact_index(index), "errors": errors}


def search_knowledge(
    root: Path = DEFAULT_KNOWLEDGE_ROOT,
    query: str = "",
    context: Optional[Dict[str, Any]] = None,
    limit: int = 8,
    max_chars: int = 2200,
) -> Dict[str, Any]:
    index = load_or_build_index(root)
    context = normalize_context(context or {})
    scored = []
    for item in index.get("snippets") or []:
        score = score_snippet(item, query, context)
        if score <= 0:
            continue
        visible = dict(item)
        visible.pop("tokens", None)
        visible["score"] = score
        scored.append(visible)
    scored.sort(key=lambda row: (-float(row.get("score") or 0), int(row.get("priority") or 0), row.get("title") or ""))
    selected = fit_char_budget(scored[: max(limit * 2, limit)], max_chars=max_chars)[:limit]
    return {
        "ok": True,
        "query": query,
        "context": context,
        "items": selected,
        "count": len(selected),
        "index": compact_index(index),
        "prompt_context": render_prompt_context(selected),
    }


def build_knowledge_prompt_context(db: Database, death: Dict[str, Any], max_chars: int = 1600) -> str:
    match = db.get_match(int(death.get("match_id") or 0)) if death.get("match_id") else None
    latest_local = db.get_latest_structured_analysis("death", int(death.get("id") or 0), "local_ai_review") if death.get("id") else None
    context_correction = db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_correction") if death.get("id") else None
    payload = (latest_local or {}).get("payload") or {}
    correction = (context_correction or {}).get("payload") or {}
    map_name = correction.get("map") or (match or {}).get("map")
    agent = correction.get("agent") or (match or {}).get("agent")
    weapon = correction.get("weapon") or payload.get("weapon") or ((payload.get("perception") or {}).get("weapon_seen"))
    labels = list(death.get("mistake_labels") or []) + list(payload.get("labels") or [])
    query = " ".join(
        [
            str(map_name or ""),
            str(agent or ""),
            str(weapon or ""),
            " ".join(str(label) for label in labels),
            str(death.get("notes") or ""),
        ]
    )
    result = search_knowledge(
        query=query,
        context={
            "map": map_name,
            "agent": agent,
            "weapon": weapon,
            "labels": labels,
            "notes": death.get("notes") or "",
        },
        limit=6,
        max_chars=max_chars,
    )
    if not result["items"]:
        return "VALORANT knowledge context: no relevant local knowledge snippets matched this clip."
    return result["prompt_context"][:max_chars]


def build_vocabulary_pack(root: Path = DEFAULT_KNOWLEDGE_ROOT, max_items: int = 240) -> Dict[str, Any]:
    """Build a compact VALORANT vocabulary for OCR/vision disambiguation."""
    index = load_or_build_index(root)
    raw_payloads = {name: read_json(root / "raw" / f"{name}.json") for name in REMOTE_ENDPOINTS}

    maps: Dict[str, Dict[str, Any]] = {name: {"name": name, "aliases": [name], "callouts": []} for name in DEFAULT_MAPS}
    for item in data_rows(raw_payloads.get("maps")):
        name = clean_text(item.get("displayName"))
        if not name:
            continue
        callouts = sorted({clean_text(row.get("regionName")) for row in item.get("callouts") or [] if clean_text(row.get("regionName"))})
        maps[name] = {"name": name, "aliases": [name], "callouts": callouts[:48]}

    agents: Dict[str, Dict[str, Any]] = {name: {"name": name, "aliases": [name], "role": "", "abilities": []} for name in DEFAULT_AGENTS}
    for item in data_rows(raw_payloads.get("agents")):
        name = clean_text(item.get("displayName"))
        if not name:
            continue
        role = clean_text((item.get("role") or {}).get("displayName"))
        abilities = [clean_text(ability.get("displayName")) for ability in item.get("abilities") or [] if clean_text(ability.get("displayName"))]
        agents[name] = {"name": name, "aliases": [name], "role": role, "abilities": abilities[:6]}

    weapons: Dict[str, Dict[str, Any]] = {name: {"name": name, "aliases": [name], "category": ""} for name in DEFAULT_WEAPONS}
    for item in data_rows(raw_payloads.get("weapons")):
        name = clean_text(item.get("displayName"))
        if not name:
            continue
        shop = item.get("shopData") or {}
        weapons[name] = {"name": name, "aliases": [name], "category": clean_text(shop.get("categoryText"))}

    for snippet in index.get("snippets") or []:
        for name in normalize_list(snippet.get("maps")):
            maps.setdefault(name, {"name": name, "aliases": [name], "callouts": []})
        for name in normalize_list(snippet.get("agents")):
            agents.setdefault(name, {"name": name, "aliases": [name], "role": "", "abilities": []})
        for name in normalize_list(snippet.get("weapons")):
            weapons.setdefault(name, {"name": name, "aliases": [name], "category": ""})

    roles = sorted({clean_text(item.get("role")) for item in agents.values() if clean_text(item.get("role"))} | {"Controller", "Duelist", "Initiator", "Sentinel"})
    callouts = sorted({callout for item in maps.values() for callout in item.get("callouts") or [] if callout})[:max_items]
    abilities = sorted({ability for item in agents.values() for ability in item.get("abilities") or [] if ability})[:max_items]
    hud_terms = [
        "round timer", "score", "ally score", "enemy score", "round number", "ultimate", "credits", "weapon",
        "shield", "health", "spike", "spike planted", "spike dropped", "ally alive", "enemy alive",
    ]

    return {
        "maps": sorted(maps.values(), key=lambda row: row["name"]),
        "agents": sorted(agents.values(), key=lambda row: row["name"]),
        "weapons": sorted(weapons.values(), key=lambda row: row["name"]),
        "roles": roles,
        "callouts": callouts,
        "abilities": abilities,
        "hud_terms": hud_terms,
        "aliases": build_alias_lookup(maps, agents, weapons),
        "summary": f"{len(maps)} maps, {len(agents)} agents, {len(weapons)} weapons, {len(callouts)} callouts",
    }


def build_alias_lookup(*groups: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for group in groups:
        for canonical, item in group.items():
            values = normalize_list(item.get("aliases")) + [canonical]
            for value in values:
                key = vocabulary_key(value)
                if key:
                    aliases[key] = canonical
    return aliases


def vocabulary_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def prompt_preview(db: Database, death_id: int, root: Path = DEFAULT_KNOWLEDGE_ROOT) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found"}
    match = db.get_match(int(death.get("match_id") or 0)) or {}
    result = search_knowledge(
        root=root,
        query=" ".join([str(match.get("map") or ""), str(match.get("agent") or ""), " ".join(death.get("mistake_labels") or [])]),
        context={"map": match.get("map"), "agent": match.get("agent"), "labels": death.get("mistake_labels") or []},
        limit=8,
        max_chars=2200,
    )
    return {"ok": True, "death": death, "match": match, **result}


def compact_index(index: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": index.get("version"),
        "built_at": index.get("built_at") or "",
        "summary": index.get("summary") or "",
        "counts": index.get("counts") or {},
        "sources": index.get("sources") or [],
        "errors": index.get("errors") or [],
        "snippet_count": len(index.get("snippets") or []),
    }


def fetch_valorant_api(endpoint: str) -> Dict[str, Any]:
    req = urlrequest.Request(
        VALORANT_API_BASE + endpoint,
        headers={"User-Agent": "valorant-coach-agent/knowledge-builder"},
    )
    with urlrequest.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def load_or_build_index(root: Path) -> Dict[str, Any]:
    index = load_index(root)
    if index:
        return index
    return rebuild_knowledge_base(root, fetch_remote=False)["index"] | {"snippets": load_curated_snippets(root)}


def load_index(root: Path) -> Dict[str, Any]:
    index = read_json(root / "index.json")
    if isinstance(index, dict) and index.get("version") == INDEX_VERSION:
        return index
    return {}


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def ensure_default_curated_files(root: Path) -> None:
    target = root / "curated" / "coaching_rules.json"
    if target.exists():
        return
    write_json(target, {"snippets": DEFAULT_CURATED_SNIPPETS})


def load_curated_snippets(root: Path) -> List[Dict[str, Any]]:
    snippets = []
    for path in sorted((root / "curated").glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        for item in payload.get("snippets") or []:
            if not isinstance(item, dict):
                continue
            normalized = normalize_snippet(item, source=f"curated:{path.name}")
            if normalized:
                snippets.append(normalized)
    for path in sorted((root / "curated").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for index, block in enumerate(split_markdown_blocks(text), start=1):
            snippets.append(
                normalize_snippet(
                    {
                        "id": f"{path.stem}-{index}",
                        "title": block["title"],
                        "type": "curated",
                        "topic": path.stem.replace("-", " "),
                        "text": block["text"],
                        "tags": [path.stem],
                        "priority": 20,
                    },
                    source=f"curated:{path.name}",
                )
            )
    return snippets


def build_structured_snippets(raw_payloads: Dict[str, Any]) -> List[Dict[str, Any]]:
    snippets: List[Dict[str, Any]] = []
    for agent in data_rows(raw_payloads.get("agents")):
        role = ((agent.get("role") or {}).get("displayName") or "").strip()
        abilities = [
            f"{ability.get('displayName')}: {clean_text(ability.get('description') or '')}"
            for ability in agent.get("abilities") or []
            if ability.get("displayName") and ability.get("description")
        ]
        snippets.append(
            normalize_snippet(
                {
                    "id": f"agent:{agent.get('uuid')}",
                    "title": f"{agent.get('displayName')} agent kit",
                    "type": "agent",
                    "topic": "agent utility",
                    "agents": [agent.get("displayName")],
                    "roles": [role],
                    "tags": [role, "ability", "utility"],
                    "text": f"{agent.get('displayName')} is a {role or 'VALORANT'} agent. " + " ".join(abilities[:5]),
                    "priority": 50,
                    "source": "valorant-api",
                }
            )
        )
    for item in data_rows(raw_payloads.get("maps")):
        callouts = [callout.get("regionName") for callout in item.get("callouts") or [] if callout.get("regionName")]
        snippets.append(
            normalize_snippet(
                {
                    "id": f"map:{item.get('uuid')}",
                    "title": f"{item.get('displayName')} map structure",
                    "type": "map",
                    "topic": "map control",
                    "maps": [item.get("displayName")],
                    "tags": ["map", "callouts", "rotation"],
                    "text": f"{item.get('displayName')} has callouts including {', '.join(callouts[:24])}. Use this for angle isolation, rotate timing, and site path review.",
                    "priority": 45,
                    "source": "valorant-api",
                }
            )
        )
    for weapon in data_rows(raw_payloads.get("weapons")):
        stats = weapon.get("weaponStats") or {}
        shop = weapon.get("shopData") or {}
        snippets.append(
            normalize_snippet(
                {
                    "id": f"weapon:{weapon.get('uuid')}",
                    "title": f"{weapon.get('displayName')} weapon handling",
                    "type": "weapon",
                    "topic": "mechanics",
                    "weapons": [weapon.get("displayName")],
                    "tags": ["weapon", "crosshair", "movement", shop.get("categoryText")],
                    "text": (
                        f"{weapon.get('displayName')} costs {shop.get('cost') or 'unknown'} and is in {shop.get('categoryText') or 'unknown category'}. "
                        f"Fire rate {stats.get('fireRate') or 'unknown'}, magazine {stats.get('magazineSize') or 'unknown'}, equip time {stats.get('equipTimeSeconds') or 'unknown'}. "
                        "Coach deaths with this weapon around first-bullet readiness, burst discipline, movement error, and range selection."
                    ),
                    "priority": 35,
                    "source": "valorant-api",
                }
            )
        )
    return [item for item in snippets if item]


def data_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    return []


def normalize_snippet(item: Dict[str, Any], source: str = "local") -> Dict[str, Any]:
    text = clean_text(item.get("text") or "")
    title = clean_text(item.get("title") or item.get("id") or "")
    if not text or not title:
        return {}
    return {
        "id": str(item.get("id") or stable_id(title)),
        "title": title[:120],
        "type": str(item.get("type") or "general"),
        "topic": str(item.get("topic") or "fundamentals"),
        "maps": normalize_list(item.get("maps")),
        "agents": normalize_list(item.get("agents")),
        "roles": normalize_list(item.get("roles")),
        "weapons": normalize_list(item.get("weapons")),
        "tags": normalize_list(item.get("tags")),
        "text": text[:900],
        "priority": int(item.get("priority") or 10),
        "source": str(item.get("source") or source),
    }


def score_snippet(item: Dict[str, Any], query: str, context: Dict[str, Any]) -> float:
    score = float(item.get("priority") or 0) / 20.0
    haystack = set(item.get("tokens") or tokenize(" ".join([item.get("title", ""), item.get("text", ""), " ".join(item.get("tags") or [])])))
    query_tokens = set(tokenize(query))
    score += len(haystack.intersection(query_tokens)) * 1.8
    score += list_overlap(item.get("maps") or [], [context.get("map")]) * 12
    score += list_overlap(item.get("agents") or [], [context.get("agent")]) * 10
    score += list_overlap(item.get("weapons") or [], [context.get("weapon")]) * 6
    score += list_overlap(item.get("tags") or [], context.get("labels") or []) * 5
    score += list_overlap(item.get("roles") or [], [context.get("role")]) * 5
    if not any([item.get("maps"), item.get("agents"), item.get("roles"), item.get("weapons")]):
        score += 2
    return round(score, 3)


def fit_char_budget(items: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    result = []
    used = 0
    for item in items:
        size = len(item.get("title") or "") + len(item.get("text") or "") + 8
        if result and used + size > max_chars:
            continue
        used += size
        result.append(item)
    return result


def render_prompt_context(items: List[Dict[str, Any]]) -> str:
    lines = ["VALORANT knowledge context, retrieved locally. Use these as game-specific coaching constraints, not as visual evidence:"]
    for item in items:
        scope = ", ".join(
            part
            for part in [
                f"map={','.join(item.get('maps') or [])}" if item.get("maps") else "",
                f"agent={','.join(item.get('agents') or [])}" if item.get("agents") else "",
                f"topic={item.get('topic') or ''}",
            ]
            if part
        )
        lines.append(f"- {item.get('title')} ({scope}): {item.get('text')}")
    return "\n".join(lines)


def count_by(items: Iterable[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def normalize_context(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "map": clean_text(context.get("map") or ""),
        "agent": clean_text(context.get("agent") or ""),
        "weapon": clean_text(context.get("weapon") or ""),
        "role": clean_text(context.get("role") or ""),
        "labels": normalize_list(context.get("labels")),
        "notes": clean_text(context.get("notes") or ""),
    }


def normalize_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = re.split(r"[,;\n]", value)
    elif isinstance(value, list):
        raw = value
    else:
        raw = [value]
    return [clean_text(item) for item in raw if clean_text(item)]


def list_overlap(left: List[str], right: List[Any]) -> int:
    left_set = {item.lower() for item in normalize_list(left)}
    right_set = {item.lower() for item in normalize_list(right)}
    return len(left_set.intersection(right_set))


def tokenize(text: str) -> List[str]:
    return [item for item in re.findall(r"[a-z0-9]+", str(text).lower()) if len(item) > 2]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def stable_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def split_markdown_blocks(text: str) -> List[Dict[str, str]]:
    blocks = []
    current_title = ""
    current_lines: List[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if current_title and current_lines:
                blocks.append({"title": current_title, "text": clean_text(" ".join(current_lines))})
            current_title = line.lstrip("#").strip()
            current_lines = []
        elif line.strip():
            current_lines.append(line.strip())
    if current_title and current_lines:
        blocks.append({"title": current_title, "text": clean_text(" ".join(current_lines))})
    return blocks


DEFAULT_CURATED_SNIPPETS = [
    {
        "id": "fundamental-angle-isolation",
        "title": "Angle isolation",
        "type": "fundamental",
        "topic": "positioning",
        "tags": ["exposed to multiple angles", "dry peek", "positioning"],
        "text": "When reviewing a death, first ask whether the player exposed themselves to one fight or two. A good VALORANT peek slices one angle, keeps cover close, and avoids giving crossfires free contact.",
        "priority": 90,
    },
    {
        "id": "fundamental-crosshair-preaim",
        "title": "Crosshair pre-aim",
        "type": "fundamental",
        "topic": "mechanics",
        "tags": ["crosshair too low/wide", "mechanics", "enemy visible"],
        "text": "Judge crosshair placement before the enemy appears. Good placement is already near likely head height and close to the next cleared angle, so the player corrects minimally when contact starts.",
        "priority": 88,
    },
    {
        "id": "fundamental-movement-error",
        "title": "Movement before shooting",
        "type": "fundamental",
        "topic": "mechanics",
        "tags": ["movement", "first bullet", "wide swing"],
        "text": "For rifle fights, check whether the player was still moving when the first bullet should have been accurate. Advice should distinguish bad aim from bad readiness or movement discipline.",
        "priority": 76,
    },
    {
        "id": "fundamental-trade-spacing",
        "title": "Trade spacing",
        "type": "fundamental",
        "topic": "teamplay",
        "tags": ["isolated from team", "trade", "entry"],
        "text": "A fight is healthier when a teammate can trade within roughly one second. If the player dies alone with teammates unable to swing, label the problem as spacing or timing before blaming mechanics.",
        "priority": 84,
    },
    {
        "id": "fundamental-utility-before-contact",
        "title": "Utility before contact",
        "type": "fundamental",
        "topic": "utility",
        "tags": ["utility unused before taking space", "dry peek"],
        "text": "If a player takes first contact into a common defender angle without info, flash, drone, smoke, stun, molly, or teammate pressure, the better play is to change the fight condition before swinging.",
        "priority": 87,
    },
    {
        "id": "fundamental-reset-after-contact",
        "title": "Reset after contact",
        "type": "fundamental",
        "topic": "fight hygiene",
        "tags": ["poor reposition after contact", "repeated same-angle fight"],
        "text": "After being seen, damaged, revealed, or after firing, the player should usually reposition, wait for support, or use utility before re-peeking. Repeating the same angle makes the next duel predictable.",
        "priority": 85,
    },
    {
        "id": "fundamental-minimap-check",
        "title": "Minimap timing check",
        "type": "fundamental",
        "topic": "awareness",
        "tags": ["late rotation / bad timing", "minimap", "rotation"],
        "text": "Use minimap/HUD evidence to judge rotations. A bad rotate usually happens after lost map control, missing teammate contact, spike pressure, or moving through an exposed lane without info.",
        "priority": 78,
    },
    {
        "id": "fundamental-post-plant",
        "title": "Post-plant discipline",
        "type": "fundamental",
        "topic": "post plant",
        "tags": ["post plant", "timing", "positioning"],
        "text": "After spike plant, the player should value time, crossfire, and utility delay over unnecessary duels. Advice should ask whether the death gave defenders a free early fight.",
        "priority": 72,
    },
    {
        "id": "fundamental-retake",
        "title": "Retake pacing",
        "type": "fundamental",
        "topic": "retake",
        "tags": ["retake", "isolated from team", "utility"],
        "text": "On retake, evaluate whether the player waited for teammates and utility before crossing a choke. Solo retake contact is usually a timing/spacing mistake unless it denies an urgent spike tap.",
        "priority": 74,
    },
    {
        "id": "duelist-entry-rule",
        "title": "Duelist entry rule",
        "type": "role",
        "topic": "duelist",
        "roles": ["Duelist"],
        "tags": ["entry", "trade", "space"],
        "text": "A duelist entry should either create traded space, force defenders off an angle, or escape after first contact. A death is poor if it consumes entry utility without creating a trade path or map space.",
        "priority": 82,
    },
    {
        "id": "controller-smoke-rule",
        "title": "Controller smoke rule",
        "type": "role",
        "topic": "controller",
        "roles": ["Controller"],
        "tags": ["smoke", "rotation", "space"],
        "text": "Controllers are judged by whether smoke timing reduces enemy sightlines before teammates cross or fight. A controller death after late or missing smoke is usually a timing and responsibility issue.",
        "priority": 80,
    },
    {
        "id": "sentinel-anchor-rule",
        "title": "Sentinel anchor rule",
        "type": "role",
        "topic": "sentinel",
        "roles": ["Sentinel"],
        "tags": ["anchor", "utility", "reposition"],
        "text": "Sentinels should play around info and delay utility. If the player dry re-peeks after their setup is triggered, coach patience, repositioning, and playing for teammate rotation.",
        "priority": 80,
    },
    {
        "id": "initiator-info-rule",
        "title": "Initiator information rule",
        "type": "role",
        "topic": "initiator",
        "roles": ["Initiator"],
        "tags": ["info", "utility", "flash"],
        "text": "Initiators should convert utility into safer team contact. If a clip shows dry contact while recon, flash, dog, drone, or stun was available, coach sequencing before mechanics.",
        "priority": 80,
    },
    {
        "id": "ascent-mid-discipline",
        "title": "Ascent mid discipline",
        "type": "map",
        "topic": "map control",
        "maps": ["Ascent"],
        "tags": ["mid", "dry peek", "crossfire"],
        "text": "Ascent mid creates layered threats from cat, tiles, top mid, market, and link timings. Death reviews should ask whether the player isolated mid contact or walked into a second angle.",
        "priority": 74,
    },
    {
        "id": "bind-teleporter-space",
        "title": "Bind teleporter space",
        "type": "map",
        "topic": "rotation",
        "maps": ["Bind"],
        "tags": ["rotation", "utility", "timing"],
        "text": "Bind rotations can change quickly through teleporters. Coach deaths around whether the player reacted to sound/info, cleared tight corners, and used utility before entering Hookah, Lamps, or Showers.",
        "priority": 72,
    },
    {
        "id": "haven-three-site-rotations",
        "title": "Haven three-site rotations",
        "type": "map",
        "topic": "rotation",
        "maps": ["Haven"],
        "tags": ["rotation", "timing", "minimap"],
        "text": "Haven punishes unsupported rotations because pressure can come through three sites and multiple links. Check minimap timing, teammate contact, and whether the player crossed known danger alone.",
        "priority": 72,
    },
    {
        "id": "split-vertical-crosshair",
        "title": "Split vertical crosshair",
        "type": "map",
        "topic": "mechanics",
        "maps": ["Split"],
        "tags": ["crosshair too low/wide", "verticality", "angles"],
        "text": "Split has frequent vertical and rope/elevated fights. Review whether crosshair height was adjusted before entering heaven, ropes, screens, ramps, or site elevation changes.",
        "priority": 68,
    },
    {
        "id": "lotus-multi-lane-pressure",
        "title": "Lotus multi-lane pressure",
        "type": "map",
        "topic": "map control",
        "maps": ["Lotus"],
        "tags": ["rotation", "multiple angles", "utility"],
        "text": "Lotus creates fast multi-lane pressure through doors and rotating links. A good death review checks whether the player cleared the second lane or rotated after control was already broken.",
        "priority": 70,
    },
    {
        "id": "sunset-mid-control",
        "title": "Sunset mid control",
        "type": "map",
        "topic": "map control",
        "maps": ["Sunset"],
        "tags": ["mid", "timing", "crossfire"],
        "text": "Sunset mid control changes site pressure quickly. Coach deaths around whether the player respected market/link timings and avoided entering a mid crossfire without utility or trade spacing.",
        "priority": 70,
    },
    {
        "id": "icebox-vertical-readiness",
        "title": "Icebox vertical readiness",
        "type": "map",
        "topic": "mechanics",
        "maps": ["Icebox"],
        "tags": ["verticality", "crosshair too low/wide", "positioning"],
        "text": "Icebox fights often shift vertically around nests, rafters, screens, tube, and site boxes. Judge whether the player pre-aimed vertical elevation before contact instead of flicking late.",
        "priority": 68,
    },
    {
        "id": "breeze-range-discipline",
        "title": "Breeze range discipline",
        "type": "map",
        "topic": "mechanics",
        "maps": ["Breeze"],
        "tags": ["range", "crosshair", "positioning"],
        "text": "Breeze creates long-range rifle fights. Coach around burst discipline, cover distance, and whether the player took a weapon-appropriate fight instead of wide-swinging into long sightlines.",
        "priority": 68,
    },
    {
        "id": "pearl-link-control",
        "title": "Pearl link control",
        "type": "map",
        "topic": "map control",
        "maps": ["Pearl"],
        "tags": ["rotation", "link", "multiple angles"],
        "text": "Pearl punishes late link and connector movement. Review whether the player had info before crossing mid/link space and whether they isolated site or main pressure.",
        "priority": 66,
    },
]
