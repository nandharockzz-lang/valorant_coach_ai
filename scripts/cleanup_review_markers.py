import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "coach.sqlite3"
BACKUP_DIR = ROOT / "data" / "backups"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_database(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"coach-before-marker-cleanup-{stamp}.sqlite3"
    shutil.copy2(path, backup)
    return backup


def load_labels(value: str) -> List[str]:
    try:
        labels = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item).strip().lower() for item in labels if str(item).strip()]


def marker_score(conn: sqlite3.Connection, marker: Dict[str, Any]) -> float:
    death_id = int(marker["id"])
    labels = load_labels(str(marker.get("mistake_labels") or "[]"))
    score = float(marker.get("confidence") or 0)
    if marker.get("clip_path"):
        score += 4
    if str(marker.get("notes") or "").strip():
        score += 3
    if labels and labels != ["needs manual review"]:
        score += 6
    score += len(labels)
    for table in ("advice", "clip_analyses"):
        count = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE death_id = ?", (death_id,)).fetchone()["count"]
        score += int(count) * 5
    structured = conn.execute(
        """
        SELECT COUNT(*) AS count FROM structured_analyses
        WHERE subject_type = 'death' AND subject_id = ?
        """,
        (death_id,),
    ).fetchone()["count"]
    return score + int(structured) * 3


def cluster_deaths(rows: List[Dict[str, Any]], window: float) -> List[List[Dict[str, Any]]]:
    timestamped = [row for row in rows if row.get("timestamp") is not None]
    timestamped.sort(key=lambda item: (float(item["timestamp"]), int(item["id"])))
    clusters: List[List[Dict[str, Any]]] = []
    for row in timestamped:
        ts = float(row["timestamp"])
        if not clusters or ts - float(clusters[-1][-1]["timestamp"]) > window:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    return [cluster for cluster in clusters if len(cluster) > 1]


def cleanup_pending_suggestions(conn: sqlite3.Connection, match_id: int, window: float, apply: bool) -> int:
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
        if any(abs(ts - other) <= window for other in blockers + kept):
            delete_ids.append(int(item["id"]))
        else:
            kept.append(ts)
    if apply and delete_ids:
        conn.executemany("DELETE FROM death_suggestions WHERE id = ?", [(item_id,) for item_id in delete_ids])
    return len(delete_ids)


def merge_duplicate_deaths(conn: sqlite3.Connection, match_id: int, window: float, apply: bool) -> int:
    deaths = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM deaths WHERE match_id = ? ORDER BY COALESCE(timestamp, 999999), id",
            (match_id,),
        ).fetchall()
    ]
    merged = 0
    for cluster in cluster_deaths(deaths, window):
        ranked = sorted(cluster, key=lambda item: (marker_score(conn, item), -int(item["id"])), reverse=True)
        keeper = ranked[0]
        duplicate_ids = [int(item["id"]) for item in ranked[1:]]
        if not apply:
            merged += len(duplicate_ids)
            continue
        keeper_id = int(keeper["id"])
        for duplicate_id in duplicate_ids:
            conn.execute("UPDATE advice SET death_id = ? WHERE death_id = ?", (keeper_id, duplicate_id))
            conn.execute("UPDATE clip_analyses SET death_id = ? WHERE death_id = ?", (keeper_id, duplicate_id))
            conn.execute(
                """
                UPDATE structured_analyses
                SET subject_id = ?
                WHERE subject_type = 'death' AND subject_id = ?
                """,
                (keeper_id, duplicate_id),
            )
            conn.execute("DELETE FROM deaths WHERE id = ?", (duplicate_id,))
            merged += 1
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean duplicate VALORANT coach death suggestions and markers.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to coach.sqlite3")
    parser.add_argument("--window", type=float, default=5.0, help="Duplicate timestamp window in seconds")
    parser.add_argument("--apply", action="store_true", help="Actually modify the database. Without this, only previews.")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    backup = None
    if args.apply:
        backup = backup_database(db_path)

    with connect(db_path) as conn:
        matches = [dict(row) for row in conn.execute("SELECT id, video_path FROM matches ORDER BY id").fetchall()]
        total_suggestions = 0
        total_deaths = 0
        for match in matches:
            match_id = int(match["id"])
            suggestions = cleanup_pending_suggestions(conn, match_id, args.window, args.apply)
            deaths = merge_duplicate_deaths(conn, match_id, args.window, args.apply)
            total_suggestions += suggestions
            total_deaths += deaths
            if suggestions or deaths:
                print(
                    f"match {match_id}: "
                    f"{suggestions} duplicate pending suggestion(s), "
                    f"{deaths} duplicate confirmed marker(s)"
                )

    mode = "Applied" if args.apply else "Preview"
    print(f"{mode}: {total_suggestions} pending suggestion(s), {total_deaths} confirmed marker duplicate(s).")
    if backup:
        print(f"Backup created: {backup}")
    if not args.apply:
        print("Run again with --apply to make these changes.")


if __name__ == "__main__":
    main()
