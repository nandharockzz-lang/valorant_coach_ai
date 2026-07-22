import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_path TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    duration REAL,
    map TEXT,
    agent TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    start_ts REAL,
    end_ts REAL,
    outcome TEXT,
    side TEXT,
    FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS deaths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    round_number INTEGER,
    timestamp REAL,
    clip_path TEXT,
    mistake_labels TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    death_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    primary_mistake TEXT NOT NULL,
    secondary_mistakes TEXT NOT NULL,
    what_happened TEXT NOT NULL,
    better_play TEXT NOT NULL,
    drill TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(death_id) REFERENCES deaths(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS coach_profile (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    rank TEXT NOT NULL DEFAULT '',
    main_agents TEXT NOT NULL DEFAULT '[]',
    target_style TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    focus_label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    target_matches INTEGER NOT NULL DEFAULT 2,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS advice_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    advice_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(advice_id) REFERENCES advice(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS match_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    focus_result TEXT NOT NULL,
    top_mistake TEXT NOT NULL,
    label_counts TEXT NOT NULL,
    next_action TEXT NOT NULL,
    drill TEXT NOT NULL,
    coach_note TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS death_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    frame_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS clip_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    death_id INTEGER NOT NULL,
    frame_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    observations TEXT NOT NULL,
    suggested_labels TEXT NOT NULL,
    metrics TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(death_id) REFERENCES deaths(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS play_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    focus_label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS session_matches (
    session_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    PRIMARY KEY(session_id, match_id),
    FOREIGN KEY(session_id) REFERENCES play_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS structured_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    analysis_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calibration_regions (
    region_name TEXT PRIMARY KEY,
    x REAL NOT NULL,
    y REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS detector_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER,
    match_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    result TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS local_playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playbook_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                """
                INSERT INTO schema_meta(key, value, updated_at)
                VALUES('schema_version', '2', CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """
            )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def schema_info(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM schema_meta ORDER BY key").fetchall()
        return {
            "items": [dict(row) for row in rows],
            "migrations": [
                {"id": "0001_initial", "status": "applied", "description": "Base coach schema."},
                {"id": "0002_jobs_logs_playbooks", "status": "applied", "description": "Persistent jobs, logs, schema metadata, local playbooks."},
            ],
            "rollback_supported": False,
        }

    def create_job(self, name: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(name, status, progress, message) VALUES(?, 'queued', 0, 'Queued.')",
                (name,),
            )
            return int(cursor.lastrowid)

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "progress", "message", "result", "error", "cancel_requested"}
        values = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "result" and value is not None:
                value = json.dumps(value)
            values[key] = value
        if not values:
            return
        columns = ", ".join(f"{key} = ?" for key in values)
        params = list(values.values()) + [job_id]
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {columns}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", params)

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_job(row) if row else None

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode_job(row) for row in rows]

    def request_cancel_job(self, job_id: int) -> None:
        self.update_job(job_id, cancel_requested=1, message="Cancel requested.")

    def _decode_job(self, row: Any) -> Dict[str, Any]:
        item = dict(row)
        if item.get("result"):
            try:
                item["result"] = json.loads(item["result"])
            except json.JSONDecodeError:
                item["result"] = None
        else:
            item["result"] = None
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        return item

    def log(self, level: str, source: str, message: str, context: Optional[Dict[str, Any]] = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO app_logs(level, source, message, context) VALUES(?, ?, ?, ?)",
                (level, source, message, json.dumps(context or {})),
            )
            return int(cursor.lastrowid)

    def list_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM app_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["context"] = json.loads(item["context"])
            except json.JSONDecodeError:
                item["context"] = {}
            items.append(item)
        return items

    def save_playbook(self, playbook_key: str, payload: Dict[str, Any]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO local_playbooks(playbook_key, payload, updated_at)
                VALUES(?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(playbook_key) DO UPDATE SET payload = excluded.payload, updated_at = CURRENT_TIMESTAMP
                """,
                (playbook_key, json.dumps(payload)),
            )
            return int(cursor.lastrowid or 0)

    def delete_playbook(self, playbook_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM local_playbooks WHERE playbook_key = ?", (playbook_key,))

    def list_playbooks(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM local_playbooks ORDER BY playbook_key").fetchall()
        items = {}
        for row in rows:
            try:
                items[row["playbook_key"]] = json.loads(row["payload"])
            except json.JSONDecodeError:
                items[row["playbook_key"]] = {}
        return items

    def get_calibration(self) -> Dict[str, Dict[str, float]]:
        defaults = {
            "hud_top": {"x": 0.37, "y": 0.00, "w": 0.26, "h": 0.11},
            "hud_bottom": {"x": 0.30, "y": 0.78, "w": 0.40, "h": 0.22},
            "killfeed": {"x": 0.72, "y": 0.02, "w": 0.27, "h": 0.22},
            "minimap": {"x": 0.015, "y": 0.02, "w": 0.22, "h": 0.30},
            "crosshair": {"x": 0.45, "y": 0.45, "w": 0.10, "h": 0.10},
            "combat_report": {"x": 0.72, "y": 0.19, "w": 0.27, "h": 0.48},
        }
        with self.connect() as conn:
            rows = conn.execute("SELECT region_name, x, y, w, h FROM calibration_regions").fetchall()
        for row in rows:
            defaults[row["region_name"]] = {
                "x": float(row["x"]),
                "y": float(row["y"]),
                "w": float(row["w"]),
                "h": float(row["h"]),
            }
        return defaults

    def save_calibration(self, regions: Dict[str, Dict[str, Any]]) -> None:
        with self.connect() as conn:
            for name, region in regions.items():
                values = (
                    name,
                    clamp01(float(region.get("x", 0))),
                    clamp01(float(region.get("y", 0))),
                    clamp01(float(region.get("w", 0))),
                    clamp01(float(region.get("h", 0))),
                )
                conn.execute(
                    """
                    INSERT INTO calibration_regions(region_name, x, y, w, h, updated_at)
                    VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(region_name) DO UPDATE SET
                        x = excluded.x,
                        y = excluded.y,
                        w = excluded.w,
                        h = excluded.h,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    values,
                )

    def reset_calibration(self, region_names: Optional[List[str]] = None) -> None:
        names = region_names or ["hud_top", "hud_bottom", "killfeed", "minimap", "crosshair", "combat_report"]
        with self.connect() as conn:
            conn.executemany("DELETE FROM calibration_regions WHERE region_name = ?", [(name,) for name in names])

    def upsert_match(self, video_path: str, started_at: str, status: str = "queued") -> int:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO matches(video_path, started_at, status) VALUES(?, ?, ?)",
                (video_path, started_at, status),
            )
            row = conn.execute("SELECT id FROM matches WHERE video_path = ?", (video_path,)).fetchone()
        match_id = int(row["id"])
        active = self.get_active_play_session()
        if active:
            self.attach_match_to_session(int(active["id"]), match_id)
        return match_id

    def update_match(self, match_id: int, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [match_id]
        with self.connect() as conn:
            conn.execute(f"UPDATE matches SET {columns} WHERE id = ?", values)

    def replace_rounds(self, match_id: int, rounds: Iterable[Dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM rounds WHERE match_id = ?", (match_id,))
            conn.executemany(
                """
                INSERT INTO rounds(match_id, round_number, start_ts, end_ts, outcome, side)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        match_id,
                        int(item.get("round_number") or 0),
                        item.get("start_ts"),
                        item.get("end_ts"),
                        item.get("outcome"),
                        item.get("side"),
                    )
                    for item in rounds
                ],
            )

    def replace_deaths(self, match_id: int, deaths: Iterable[Dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM deaths WHERE match_id = ?", (match_id,))
            conn.executemany(
                """
                INSERT INTO deaths(match_id, round_number, timestamp, clip_path, mistake_labels, confidence, notes)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        match_id,
                        item.get("round_number"),
                        item.get("timestamp"),
                        item.get("clip_path"),
                        json.dumps(item.get("labels") or item.get("mistake_labels") or []),
                        float(item.get("confidence") or 0),
                        item.get("notes") or "",
                    )
                    for item in deaths
                ],
            )

    def list_matches(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*,
                       COUNT(DISTINCT r.id) AS round_count,
                       COUNT(DISTINCT d.id) AS death_count
                FROM matches m
                LEFT JOIN rounds r ON r.match_id = m.id
                LEFT JOIN deaths d ON d.match_id = m.id
                GROUP BY m.id
                ORDER BY m.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_match(self, match_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        return dict(row) if row else None

    def get_rounds(self, match_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rounds WHERE match_id = ? ORDER BY round_number",
                (match_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_deaths(self, match_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deaths WHERE match_id = ? ORDER BY COALESCE(timestamp, 999999), id",
                (match_id,),
            ).fetchall()
        deaths = []
        for row in rows:
            item = dict(row)
            try:
                item["mistake_labels"] = json.loads(item["mistake_labels"])
            except json.JSONDecodeError:
                item["mistake_labels"] = []
            item["advice"] = self.get_latest_advice(int(item["id"]))
            item["vision"] = self.get_latest_clip_analysis(int(item["id"]))
            death_id = int(item["id"])
            item["understanding"] = self.get_latest_structured_analysis("death", death_id, "clip_understanding")
            item["keyframes"] = self.get_latest_structured_analysis("death", death_id, "keyframes")
            item["local_ai_review"] = self.get_latest_structured_analysis("death", death_id, "local_ai_review")
            item["clip_visual_signals"] = self.get_latest_structured_analysis("death", death_id, "clip_visual_signals")
            item["clip_ocr_regions"] = self.get_latest_structured_analysis("death", death_id, "clip_ocr_regions")
            item["clip_review_feedback"] = self.get_latest_structured_analysis("death", death_id, "clip_review_feedback")
            item["clip_training_label"] = self.get_latest_structured_analysis("death", death_id, "clip_training_label")
            item["annotations"] = self.list_subject_analyses("death", death_id, "clip_annotation", limit=20)
            deaths.append(item)
        return deaths

    def get_death(self, death_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deaths WHERE id = ?", (death_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["mistake_labels"] = json.loads(item["mistake_labels"])
        except json.JSONDecodeError:
            item["mistake_labels"] = []
        item["vision"] = self.get_latest_clip_analysis(death_id)
        item["gameplay"] = self.get_latest_structured_analysis("death", death_id, "gameplay")
        item["understanding"] = self.get_latest_structured_analysis("death", death_id, "clip_understanding")
        item["keyframes"] = self.get_latest_structured_analysis("death", death_id, "keyframes")
        item["local_ai_review"] = self.get_latest_structured_analysis("death", death_id, "local_ai_review")
        item["context_extraction"] = self.get_latest_structured_analysis("death", death_id, "context_extraction")
        item["clip_visual_signals"] = self.get_latest_structured_analysis("death", death_id, "clip_visual_signals")
        item["clip_ocr_regions"] = self.get_latest_structured_analysis("death", death_id, "clip_ocr_regions")
        item["clip_review_feedback"] = self.get_latest_structured_analysis("death", death_id, "clip_review_feedback")
        item["clip_training_label"] = self.get_latest_structured_analysis("death", death_id, "clip_training_label")
        item["annotations"] = self.list_subject_analyses("death", death_id, "clip_annotation", limit=20)
        return item

    def get_latest_advice(self, death_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM advice WHERE death_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (death_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["secondary_mistakes"] = json.loads(item["secondary_mistakes"])
        except json.JSONDecodeError:
            item["secondary_mistakes"] = []
        item["feedback"] = self.get_latest_advice_feedback(int(item["id"]))
        return item

    def save_advice(self, advice: Dict[str, Any]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO advice(
                    death_id, provider, source, primary_mistake, secondary_mistakes,
                    what_happened, better_play, drill, confidence
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    advice["death_id"],
                    advice["provider"],
                    advice["source"],
                    advice["primary_mistake"],
                    json.dumps(advice.get("secondary_mistakes") or []),
                    advice["what_happened"],
                    advice["better_play"],
                    advice["drill"],
                    float(advice.get("confidence") or 0),
                ),
            )
            return int(cursor.lastrowid)

    def get_latest_advice_feedback(self, advice_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM advice_feedback WHERE advice_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (advice_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_advice_feedback(self, advice_id: int, verdict: str, note: str = "") -> int:
        if verdict not in {"accepted", "rejected"}:
            raise ValueError("verdict must be accepted or rejected")
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO advice_feedback(advice_id, verdict, note) VALUES(?, ?, ?)",
                (advice_id, verdict, note),
            )
            return int(cursor.lastrowid)

    def get_feedback_summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT verdict, COUNT(*) AS count
                FROM advice_feedback
                GROUP BY verdict
                """
            ).fetchall()
        summary = {"accepted": 0, "rejected": 0}
        for row in rows:
            summary[row["verdict"]] = int(row["count"])
        return summary

    def get_profile(self) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM coach_profile WHERE id = 1").fetchone()
        if not row:
            return {"rank": "", "main_agents": [], "target_style": "", "notes": ""}
        item = dict(row)
        try:
            item["main_agents"] = json.loads(item["main_agents"])
        except json.JSONDecodeError:
            item["main_agents"] = []
        return item

    def save_profile(
        self,
        rank: str,
        main_agents: List[str],
        target_style: str,
        notes: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO coach_profile(id, rank, main_agents, target_style, notes, updated_at)
                VALUES(1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    rank = excluded.rank,
                    main_agents = excluded.main_agents,
                    target_style = excluded.target_style,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (rank, json.dumps(main_agents), target_style, notes),
            )

    def create_goal(self, focus_label: str, description: str, target_matches: int = 2) -> int:
        with self.connect() as conn:
            conn.execute("UPDATE session_goals SET status = 'replaced' WHERE status = 'active'")
            cursor = conn.execute(
                """
                INSERT INTO session_goals(focus_label, description, target_matches)
                VALUES(?, ?, ?)
                """,
                (focus_label, description, target_matches),
            )
            return int(cursor.lastrowid)

    def get_active_goal(self) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_goals WHERE status = 'active' ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def complete_goal(self, goal_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE session_goals SET status = 'complete', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (goal_id,),
            )

    def save_match_review(self, review: Dict[str, Any]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO match_reviews(
                    match_id, summary, focus_result, top_mistake, label_counts,
                    next_action, drill, coach_note, score
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review["match_id"],
                    review["summary"],
                    review["focus_result"],
                    review["top_mistake"],
                    json.dumps(review.get("label_counts") or {}),
                    review["next_action"],
                    review["drill"],
                    review["coach_note"],
                    int(review.get("score") or 0),
                ),
            )
            return int(cursor.lastrowid)

    def get_latest_match_review(self, match_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM match_reviews WHERE match_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (match_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["label_counts"] = json.loads(item["label_counts"])
        except json.JSONDecodeError:
            item["label_counts"] = {}
        return item

    def update_death(self, death_id: int, labels: List[str], notes: str, confidence: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE deaths SET mistake_labels = ?, notes = ?, confidence = ? WHERE id = ?",
                (json.dumps(labels), notes, confidence, death_id),
            )

    def create_death(
        self,
        match_id: int,
        round_number: Optional[int],
        timestamp: Optional[float],
        labels: List[str],
        notes: str,
        confidence: float,
    ) -> int:
        with self.connect() as conn:
            if timestamp is not None:
                existing = conn.execute(
                    """
                    SELECT id FROM deaths
                    WHERE match_id = ?
                      AND timestamp IS NOT NULL
                      AND ABS(timestamp - ?) <= 3.0
                    ORDER BY ABS(timestamp - ?), id
                    LIMIT 1
                    """,
                    (match_id, float(timestamp), float(timestamp)),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO deaths(match_id, round_number, timestamp, clip_path, mistake_labels, confidence, notes)
                VALUES(?, ?, ?, NULL, ?, ?, ?)
                """,
                (match_id, round_number, timestamp, json.dumps(labels), confidence, notes),
            )
            return int(cursor.lastrowid)

    def create_death_suggestion(
        self,
        match_id: int,
        timestamp: float,
        reason: str,
        confidence: float,
        frame_path: Optional[str],
    ) -> Optional[int]:
        with self.connect() as conn:
            existing_death = conn.execute(
                """
                SELECT id FROM deaths
                WHERE match_id = ?
                  AND timestamp IS NOT NULL
                  AND ABS(timestamp - ?) <= 5.0
                ORDER BY ABS(timestamp - ?), id
                LIMIT 1
                """,
                (match_id, float(timestamp), float(timestamp)),
            ).fetchone()
            if existing_death:
                return None

            existing_suggestion = conn.execute(
                """
                SELECT * FROM death_suggestions
                WHERE match_id = ?
                  AND ABS(timestamp - ?) <= 5.0
                ORDER BY
                  CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'accepted' THEN 1
                    WHEN 'rejected' THEN 2
                    ELSE 3
                  END,
                  ABS(timestamp - ?),
                  id
                LIMIT 1
                """,
                (match_id, float(timestamp), float(timestamp)),
            ).fetchone()
            if existing_suggestion:
                if existing_suggestion["status"] == "pending":
                    existing_id = int(existing_suggestion["id"])
                    if float(confidence) > float(existing_suggestion["confidence"] or 0):
                        conn.execute(
                            """
                            UPDATE death_suggestions
                            SET timestamp = ?, reason = ?, confidence = ?, frame_path = ?
                            WHERE id = ?
                            """,
                            (timestamp, reason, confidence, frame_path, existing_id),
                        )
                    return existing_id
                return None

            cursor = conn.execute(
                """
                INSERT INTO death_suggestions(match_id, timestamp, reason, confidence, frame_path)
                VALUES(?, ?, ?, ?, ?)
                """,
                (match_id, timestamp, reason, confidence, frame_path),
            )
            return int(cursor.lastrowid)

    def save_clip_analysis(self, analysis: Dict[str, Any]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO clip_analyses(
                    death_id, frame_count, summary, observations,
                    suggested_labels, metrics, confidence
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis["death_id"],
                    int(analysis.get("frame_count") or 0),
                    analysis["summary"],
                    json.dumps(analysis.get("observations") or []),
                    json.dumps(analysis.get("suggested_labels") or []),
                    json.dumps(analysis.get("metrics") or {}),
                    float(analysis.get("confidence") or 0),
                ),
            )
            return int(cursor.lastrowid)

    def get_latest_clip_analysis(self, death_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM clip_analyses WHERE death_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (death_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        for key, fallback in (("observations", []), ("suggested_labels", []), ("metrics", {})):
            try:
                item[key] = json.loads(item[key])
            except json.JSONDecodeError:
                item[key] = fallback
        return item

    def get_death_suggestions(self, match_id: int) -> List[Dict[str, Any]]:
        return self.list_death_suggestions(match_id, "pending")

    def list_death_suggestions(self, match_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        where = "WHERE match_id = ?"
        params: List[Any] = [match_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM death_suggestions
                {where}
                ORDER BY timestamp, id
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_pending_death_suggestions(self, match_id: int, window_seconds: float = 5.0) -> int:
        with self.connect() as conn:
            pending = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM death_suggestions
                    WHERE match_id = ? AND status = 'pending'
                    ORDER BY confidence DESC, timestamp, id
                    """,
                    (match_id,),
                ).fetchall()
            ]
            blockers = [
                float(row["timestamp"])
                for row in conn.execute(
                    """
                    SELECT timestamp FROM deaths
                    WHERE match_id = ? AND timestamp IS NOT NULL
                    UNION ALL
                    SELECT timestamp FROM death_suggestions
                    WHERE match_id = ? AND status IN ('accepted', 'rejected')
                    """,
                    (match_id, match_id),
                ).fetchall()
            ]
            kept: List[float] = []
            delete_ids: List[int] = []
            for item in pending:
                ts = float(item.get("timestamp") or 0)
                duplicate = any(abs(ts - other) <= window_seconds for other in blockers + kept)
                if duplicate:
                    delete_ids.append(int(item["id"]))
                else:
                    kept.append(ts)
            if delete_ids:
                conn.executemany("DELETE FROM death_suggestions WHERE id = ?", [(item_id,) for item_id in delete_ids])
            return len(delete_ids)

    def clear_pending_death_suggestions(self, match_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM death_suggestions WHERE match_id = ? AND status = 'pending'",
                (match_id,),
            ).fetchone()
            conn.execute(
                "DELETE FROM death_suggestions WHERE match_id = ? AND status = 'pending'",
                (match_id,),
            )
        return int(row["count"] if row else 0)

    def update_death_suggestion_status(self, suggestion_id: int, status: str) -> None:
        if status not in {"pending", "accepted", "rejected"}:
            raise ValueError("invalid suggestion status")
        with self.connect() as conn:
            conn.execute("UPDATE death_suggestions SET status = ? WHERE id = ?", (status, suggestion_id))

    def get_death_suggestion(self, suggestion_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM death_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        return dict(row) if row else None

    def save_detector_feedback(
        self,
        suggestion: Dict[str, Any],
        verdict: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> int:
        if verdict not in {"accepted", "rejected"}:
            raise ValueError("verdict must be accepted or rejected")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO detector_feedback(suggestion_id, match_id, verdict, confidence, reason, metrics)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    suggestion.get("id"),
                    int(suggestion["match_id"]),
                    verdict,
                    float(suggestion.get("confidence") or 0),
                    str(suggestion.get("reason") or ""),
                    json.dumps(metrics or {}),
                ),
            )
            return int(cursor.lastrowid)

    def detector_feedback_summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT verdict, COUNT(*) AS count, AVG(confidence) AS avg_confidence
                FROM detector_feedback
                GROUP BY verdict
                """
            ).fetchall()
        summary = {"accepted": 0, "rejected": 0, "avg_confidence": {}, "threshold_adjustment": 0.0}
        for row in rows:
            verdict = row["verdict"]
            summary[verdict] = int(row["count"])
            summary["avg_confidence"][verdict] = round(float(row["avg_confidence"] or 0), 3)
        total = summary["accepted"] + summary["rejected"]
        if total:
            rejection_rate = summary["rejected"] / total
            acceptance_rate = summary["accepted"] / total
            summary["threshold_adjustment"] = round((rejection_rate - acceptance_rate) * 0.06, 3)
        return summary

    def suggestion_learning_summary(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count, AVG(confidence) AS avg_confidence FROM death_suggestions GROUP BY status"
            ).fetchall()
        summary = {"pending": 0, "accepted": 0, "rejected": 0, "avg_confidence": {}}
        for row in rows:
            status = row["status"]
            summary[status] = int(row["count"])
            summary["avg_confidence"][status] = round(float(row["avg_confidence"] or 0), 2)
        total_labeled = summary["accepted"] + summary["rejected"]
        summary["acceptance_rate"] = round(summary["accepted"] / total_labeled, 2) if total_labeled else 0
        summary["detector_feedback"] = self.detector_feedback_summary()
        return summary

    def start_play_session(self, name: str, focus_label: str, notes: str = "") -> int:
        with self.connect() as conn:
            conn.execute("UPDATE play_sessions SET status = 'ended', ended_at = CURRENT_TIMESTAMP WHERE status = 'active'")
            cursor = conn.execute(
                "INSERT INTO play_sessions(name, focus_label, notes) VALUES(?, ?, ?)",
                (name, focus_label, notes),
            )
            return int(cursor.lastrowid)

    def end_play_session(self, session_id: int, notes: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE play_sessions
                SET status = 'ended', ended_at = CURRENT_TIMESTAMP, notes = CASE WHEN ? = '' THEN notes ELSE ? END
                WHERE id = ?
                """,
                (notes, notes, session_id),
            )

    def get_active_play_session(self) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM play_sessions WHERE status = 'active' ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def attach_match_to_session(self, session_id: int, match_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_matches(session_id, match_id) VALUES(?, ?)",
                (session_id, match_id),
            )

    def get_session_summary(self) -> Dict[str, Any]:
        active = self.get_active_play_session()
        with self.connect() as conn:
            recent = conn.execute(
                """
                SELECT s.*, COUNT(sm.match_id) AS match_count
                FROM play_sessions s
                LEFT JOIN session_matches sm ON sm.session_id = s.id
                GROUP BY s.id
                ORDER BY s.started_at DESC
                LIMIT 5
                """
            ).fetchall()
        return {"active": active, "recent": [dict(row) for row in recent]}

    def save_structured_analysis(self, subject_id: int, analysis_type: str, payload: Dict[str, Any]) -> int:
        return self._save_structured_analysis("match", subject_id, analysis_type, payload)

    def save_death_analysis(self, death_id: int, analysis_type: str, payload: Dict[str, Any]) -> int:
        return self._save_structured_analysis("death", death_id, analysis_type, payload)

    def _save_structured_analysis(
        self,
        subject_type: str,
        subject_id: int,
        analysis_type: str,
        payload: Dict[str, Any],
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO structured_analyses(subject_type, subject_id, analysis_type, payload)
                VALUES(?, ?, ?, ?)
                """,
                (subject_type, subject_id, analysis_type, json.dumps(payload)),
            )
            return int(cursor.lastrowid)

    def get_latest_structured_analysis(
        self,
        subject_type: str,
        subject_id: int,
        analysis_type: str,
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM structured_analyses
                WHERE subject_type = ? AND subject_id = ? AND analysis_type = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (subject_type, subject_id, analysis_type),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"])
        except json.JSONDecodeError:
            item["payload"] = {}
        return item

    def list_structured_analyses(self, subject_type: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = ""
        if subject_type:
            where = "WHERE subject_type = ?"
            params.append(subject_type)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM structured_analyses
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                item["payload"] = {}
            items.append(item)
        return items

    def list_subject_analyses(
        self,
        subject_type: str,
        subject_id: int,
        analysis_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [subject_type, subject_id]
        where = "WHERE subject_type = ? AND subject_id = ?"
        if analysis_type:
            where += " AND analysis_type = ?"
            params.append(analysis_type)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM structured_analyses
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                item["payload"] = {}
            items.append(item)
        return items

    def update_death_full(
        self,
        death_id: int,
        round_number: Optional[int],
        timestamp: Optional[float],
        labels: List[str],
        notes: str,
        confidence: float,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE deaths
                SET round_number = ?, timestamp = ?, mistake_labels = ?, notes = ?, confidence = ?
                WHERE id = ?
                """,
                (round_number, timestamp, json.dumps(labels), notes, confidence, death_id),
            )

    def update_death_round_number(self, death_id: int, round_number: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE deaths SET round_number = ? WHERE id = ?", (round_number, death_id))

    def update_death_clip(self, death_id: int, clip_path: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE deaths SET clip_path = ? WHERE id = ?", (clip_path, death_id))

    def delete_death(self, death_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM deaths WHERE id = ?", (death_id,))

    def build_trends(self) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id AS match_id, m.created_at, m.map, m.agent, d.mistake_labels
                FROM matches m
                JOIN deaths d ON d.match_id = m.id
                ORDER BY m.created_at DESC, d.id DESC
                """
            ).fetchall()

        labels: Dict[str, int] = {}
        by_match: Dict[int, Dict[str, Any]] = {}
        by_map: Dict[str, int] = {}
        by_agent: Dict[str, int] = {}
        for row in rows:
            match_id = int(row["match_id"])
            by_match.setdefault(
                match_id,
                {
                    "match_id": match_id,
                    "created_at": row["created_at"],
                    "map": row["map"] or "unknown",
                    "agent": row["agent"] or "unknown",
                    "death_count": 0,
                    "labels": {},
                },
            )
            by_match[match_id]["death_count"] += 1
            by_map[row["map"] or "unknown"] = by_map.get(row["map"] or "unknown", 0) + 1
            by_agent[row["agent"] or "unknown"] = by_agent.get(row["agent"] or "unknown", 0) + 1
            try:
                parsed = json.loads(row["mistake_labels"])
            except json.JSONDecodeError:
                parsed = []
            for label in parsed:
                if label == "needs manual review":
                    continue
                labels[label] = labels.get(label, 0) + 1
                by_match[match_id]["labels"][label] = by_match[match_id]["labels"].get(label, 0) + 1

        return {
            "labels": dict(sorted(labels.items(), key=lambda item: item[1], reverse=True)),
            "matches": list(by_match.values()),
            "by_map": dict(sorted(by_map.items(), key=lambda item: item[1], reverse=True)),
            "by_agent": dict(sorted(by_agent.items(), key=lambda item: item[1], reverse=True)),
        }


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
