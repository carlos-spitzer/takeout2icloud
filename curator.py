import logging

from rich.console import Console
from rich.table import Table

from models import CurateSummary, AlbumMembership
from state import (
    get_connection,
    get_all_sha256_in_albums,
    update_status,
    add_album_membership,
    mark_phase_complete,
)

logger = logging.getLogger(__name__)
console = Console()


def curate() -> CurateSummary:
    conn = get_connection()
    summary = CurateSummary()

    album_hashes = get_all_sha256_in_albums(conn)

    # Only process files still at "pending" status to make this idempotent
    rows = conn.execute("""
        SELECT sha256, source_path, canonical_path, folder_type, album_name, status
        FROM media WHERE status = 'pending'
        ORDER BY sha256
    """).fetchall()

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["sha256"], []).append(dict(r))

    for sha256, entries in groups.items():
        album_entries = [e for e in entries if e["folder_type"] == "album"]
        bucket_entries = [e for e in entries if e["folder_type"] == "year_bucket"]

        if len(album_entries) > 1:
            for entry in album_entries:
                if entry["album_name"]:
                    add_album_membership(conn, AlbumMembership(
                        sha256=sha256, album_name=entry["album_name"],
                    ))
            for entry in album_entries[1:]:
                update_status(conn, sha256, entry["source_path"], "duplicate")
                summary.duplicates_suppressed += 1

        for entry in bucket_entries:
            if sha256 in album_hashes:
                update_status(conn, sha256, entry["source_path"], "duplicate")
                summary.duplicates_suppressed += 1
            else:
                summary.unalbumized += 1

    # Compute totals from full DB state (includes previously curated files)
    row = conn.execute("""
        SELECT COUNT(DISTINCT sha256) as cnt FROM media
        WHERE folder_type = 'album' AND status != 'duplicate'
    """).fetchone()
    summary.albumized = row["cnt"]

    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM media WHERE status != 'duplicate'
    """).fetchone()
    summary.total_unique = row["cnt"]

    album_rows = conn.execute("""
        SELECT am.album_name, COUNT(DISTINCT am.sha256) as cnt
        FROM album_memberships am
        JOIN media m ON am.sha256 = m.sha256
        WHERE m.status != 'duplicate' OR m.folder_type = 'album'
        GROUP BY am.album_name
        ORDER BY am.album_name
    """).fetchall()
    for r in album_rows:
        summary.albums[r["album_name"]] = r["cnt"]

    conn.commit()
    mark_phase_complete(conn, "curate", summary.total_unique)
    conn.close()

    _print_curate_summary(summary)
    return summary


def _print_curate_summary(summary: CurateSummary) -> None:
    console.print()
    console.print("[bold green]Curation complete[/bold green]")

    table = Table(title="Curation Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white", justify="right")

    table.add_row("Unique files to import", str(summary.total_unique))
    table.add_row("Duplicates suppressed", str(summary.duplicates_suppressed))
    table.add_row("Albumized", str(summary.albumized))
    table.add_row("Unalbumized", str(summary.unalbumized))

    console.print(table)

    if summary.albums:
        album_table = Table(title="Albums")
        album_table.add_column("Album", style="cyan")
        album_table.add_column("Files", style="white", justify="right")
        for name, count in sorted(summary.albums.items()):
            album_table.add_row(name, str(count))
        console.print(album_table)
