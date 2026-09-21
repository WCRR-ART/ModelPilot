"""Create and verify a non-overwriting SQLite snapshot, including committed WAL data.

Usage: python scripts/backup-sqlite.py SOURCE_DATABASE NEW_DESTINATION
The same command can restore a backup to a new path with all app processes stopped.
This copies the existing schema; it does not migrate or downgrade a database.
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> int:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not source.is_file() or source == destination:
        raise ValueError("source must be an existing database; destination must differ")
    if any(Path(str(destination) + suffix).exists() for suffix in ("", "-wal", "-shm")):
        raise FileExistsError("destination or SQLite sidecar already exists")
    # Exclusive creation protects an existing destination even if it appears after the check.
    with destination.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(destination)) as writer,
        ):
            reader.backup(writer)
            if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("backup integrity check failed")
            version = writer.execute("SELECT version FROM schema_version").fetchone()
            if version is None:
                raise sqlite3.DatabaseError("backup has no ModelPilot schema version")
            return int(version[0])
    except Exception:
        # Only remove the incomplete destination exclusively created by this invocation.
        destination.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        version = backup_database(args.source, args.destination)
    except FileExistsError:
        print("Backup refused: destination or SQLite sidecar already exists.", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error):
        print(
            "Backup failed: verify paths, permissions and source database integrity.",
            file=sys.stderr,
        )
        return 1
    print(f"Backup verified: integrity_check=ok; schema={version}. No migration performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
