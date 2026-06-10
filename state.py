import sqlite3
from pathlib import Path
from typing import Optional

from models import MediaItem, AlbumMembership

DB_DIR = Path.home() / ".takeout2icloud"
DB_PATH = DB_DIR / "state.db"


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media (
            sha256 TEXT NOT NULL,
            source_path TEXT NOT NULL,
            canonical_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            folder_type TEXT NOT NULL,
            album_name TEXT,
            capture_ts INTEGER,
            latitude REAL,
            longitude REAL,
            altitude REAL,
            title TEXT,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            photos_uuid TEXT,
            error_message TEXT,
            PRIMARY KEY (sha256, source_path)
        );

        CREATE TABLE IF NOT EXISTS album_memberships (
            sha256 TEXT NOT NULL,
            album_name TEXT NOT NULL,
            PRIMARY KEY (sha256, album_name)
        );

        CREATE TABLE IF NOT EXISTS phase_log (
            phase TEXT PRIMARY KEY,
            completed_at TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
        CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media(sha256);
        CREATE INDEX IF NOT EXISTS idx_media_folder_type ON media(folder_type);
    """)
    conn.commit()


def upsert_media(conn: sqlite3.Connection, item: MediaItem) -> None:
    conn.execute("""
        INSERT INTO media (
            sha256, source_path, canonical_path, filename, folder_type, album_name,
            capture_ts, latitude, longitude, altitude, title, description,
            status, photos_uuid, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sha256, source_path) DO UPDATE SET
            filename=excluded.filename,
            folder_type=excluded.folder_type,
            album_name=excluded.album_name,
            capture_ts=excluded.capture_ts,
            latitude=excluded.latitude,
            longitude=excluded.longitude,
            altitude=excluded.altitude,
            title=excluded.title,
            description=excluded.description
    """, (
        item.sha256, item.source_path, item.canonical_path, item.filename,
        item.folder_type, item.album_name, item.capture_ts, item.latitude,
        item.longitude, item.altitude, item.title, item.description,
        item.status, item.photos_uuid, item.error_message,
    ))


def add_album_membership(conn: sqlite3.Connection, membership: AlbumMembership) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO album_memberships (sha256, album_name)
        VALUES (?, ?)
    """, (membership.sha256, membership.album_name))


def get_album_memberships(conn: sqlite3.Connection, sha256: str) -> list[str]:
    rows = conn.execute(
        "SELECT album_name FROM album_memberships WHERE sha256 = ?",
        (sha256,),
    ).fetchall()
    return [r["album_name"] for r in rows]


def get_media_by_status(conn: sqlite3.Connection, status: str, limit: Optional[int] = None,
                        album: Optional[str] = None) -> list[dict]:
    query = "SELECT * FROM media WHERE status = ?"
    params: list = [status]
    if album:
        query += " AND sha256 IN (SELECT sha256 FROM album_memberships WHERE album_name = ?)"
        params.append(album)
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def update_status(conn: sqlite3.Connection, sha256: str, source_path: str,
                  status: str, error_message: Optional[str] = None,
                  photos_uuid: Optional[str] = None) -> None:
    conn.execute("""
        UPDATE media SET status = ?, error_message = ?, photos_uuid = ?
        WHERE sha256 = ? AND source_path = ?
    """, (status, error_message, photos_uuid, sha256, source_path))


def update_status_by_sha(conn: sqlite3.Connection, sha256: str, status: str,
                         error_message: Optional[str] = None) -> None:
    conn.execute("""
        UPDATE media SET status = ?, error_message = ?
        WHERE sha256 = ?
    """, (status, error_message, sha256))


def update_canonical_path(conn: sqlite3.Connection, sha256: str, source_path: str,
                          new_path: str) -> None:
    conn.execute("""
        UPDATE media SET canonical_path = ?
        WHERE sha256 = ? AND source_path = ?
    """, (new_path, sha256, source_path))


def get_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM media GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


def get_album_progress(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT am.album_name,
               COUNT(*) as total,
               SUM(CASE WHEN m.status = 'imported' THEN 1 ELSE 0 END) as imported
        FROM album_memberships am
        JOIN media m ON am.sha256 = m.sha256
        GROUP BY am.album_name
        ORDER BY am.album_name
    """).fetchall()
    return [dict(r) for r in rows]


def get_all_sha256_in_albums(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT sha256 FROM media WHERE folder_type = 'album'"
    ).fetchall()
    return {r["sha256"] for r in rows}


def get_sha256_counts(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Group media entries by sha256, returning those with multiple entries."""
    rows = conn.execute("""
        SELECT sha256, source_path, canonical_path, folder_type, album_name, status
        FROM media ORDER BY sha256
    """).fetchall()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["sha256"], []).append(dict(r))
    return groups


def mark_phase_complete(conn: sqlite3.Connection, phase: str, file_count: int = 0) -> None:
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO phase_log (phase, completed_at, file_count)
        VALUES (?, ?, ?)
        ON CONFLICT(phase) DO UPDATE SET completed_at=excluded.completed_at, file_count=excluded.file_count
    """, (phase, datetime.now(timezone.utc).isoformat(), file_count))
    conn.commit()


def is_phase_complete(conn: sqlite3.Connection, phase: str) -> bool:
    row = conn.execute("SELECT 1 FROM phase_log WHERE phase = ?", (phase,)).fetchone()
    return row is not None


def clear_phase(conn: sqlite3.Connection, phase: str) -> None:
    conn.execute("DELETE FROM phase_log WHERE phase = ?", (phase,))
    conn.commit()


def get_known_paths(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT source_path FROM media").fetchall()
    return {r["source_path"] for r in rows}


def reset_failed(conn: sqlite3.Connection, phase: Optional[str] = None) -> int:
    """Reset failed files back to the appropriate prior status for retry.

    Phase-specific reset uses error_message patterns to identify which phase
    failed. Without a phase, resets all failed files to 'pending'.
    """
    if phase == "import":
        # Import failures: files that made it to staging/exif_fixed, reset back to exif_fixed
        # Match error messages from the importer
        cur = conn.execute("""
            UPDATE media SET status = 'exif_fixed', error_message = NULL
            WHERE status = 'failed'
              AND (error_message LIKE '%Photos.app%'
                   OR error_message LIKE '%import%'
                   OR error_message LIKE '%Rejected%'
                   OR error_message LIKE '%Not returned%')
        """)
    elif phase == "fix-exif":
        cur = conn.execute("""
            UPDATE media SET status = 'staged', error_message = NULL
            WHERE status = 'failed'
              AND (error_message LIKE 'exiftool%'
                   OR error_message LIKE 'Staged file not found%')
        """)
    elif phase == "stage":
        cur = conn.execute("""
            UPDATE media SET status = 'pending', error_message = NULL
            WHERE status = 'failed'
              AND error_message LIKE 'Source file not found%'
        """)
    elif phase:
        return 0
    else:
        cur = conn.execute(
            "UPDATE media SET status = 'pending', error_message = NULL WHERE status = 'failed'",
        )
    count = cur.rowcount
    conn.commit()
    return count


def reset_scan(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM media")
    conn.execute("DELETE FROM album_memberships")
    conn.execute("DELETE FROM phase_log")
    conn.commit()
