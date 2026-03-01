from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Artwork:
    id: int
    seq_no: int
    asset_url: str
    title: str
    creator: str
    year: str
    description: str
    thumbnail_url: str
    created_at: str
    updated_at: str


class ArtworkDb:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path)
        con.row_factory = sqlite3.Row
        # Better concurrency for background workers.
        try:
            con.execute("PRAGMA journal_mode=WAL;")
            con.execute("PRAGMA busy_timeout=5000;")
        except Exception:
            pass
        return con

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS artworks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seq_no INTEGER,
                    asset_url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    creator TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_artworks_updated_at ON artworks(updated_at);"
            )

            cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(artworks);").fetchall()}
            if "seq_no" not in cols:
                con.execute("ALTER TABLE artworks ADD COLUMN seq_no INTEGER;")
            # Best-effort backfill for existing DBs.
            con.execute("UPDATE artworks SET seq_no = id WHERE seq_no IS NULL;")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def upsert_artwork(
        self,
        *,
        asset_url: str,
        title: str,
        creator: str,
        year: str,
        description: str,
        thumbnail_url: str,
    ) -> int:
        now = self._now_iso()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO artworks(
                    seq_no,
                    asset_url,
                    title,
                    creator,
                    year,
                    description,
                    thumbnail_url,
                    created_at,
                    updated_at
                )
                VALUES((SELECT COALESCE(MAX(seq_no), 0) + 1 FROM artworks), ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_url) DO UPDATE SET
                    title=excluded.title,
                    creator=excluded.creator,
                    year=excluded.year,
                    description=excluded.description,
                    thumbnail_url=excluded.thumbnail_url,
                    updated_at=excluded.updated_at;
                """,
                (asset_url, title, creator, year, description, thumbnail_url, now, now),
            )
            row = con.execute("SELECT id FROM artworks WHERE asset_url=?", (asset_url,)).fetchone()
            return int(row["id"]) if row else -1

    def list_artworks(self) -> list[Artwork]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, seq_no, asset_url, title, creator, year, description, thumbnail_url, created_at, updated_at
                FROM artworks
                ORDER BY updated_at DESC, id DESC;
                """
            ).fetchall()
        return [
            Artwork(
                id=int(r["id"]),
                seq_no=int(r["seq_no"] or 0),
                asset_url=str(r["asset_url"]),
                title=str(r["title"]),
                creator=str(r["creator"]),
                year=str(r["year"]),
                description=str(r["description"]),
                thumbnail_url=str(r["thumbnail_url"]),
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
            )
            for r in rows
        ]

    def get_artwork(self, artwork_id: int) -> Artwork | None:
        with self._connect() as con:
            r = con.execute(
                """
                SELECT id, seq_no, asset_url, title, creator, year, description, thumbnail_url, created_at, updated_at
                FROM artworks
                WHERE id=?;
                """,
                (artwork_id,),
            ).fetchone()
        if not r:
            return None
        return Artwork(
            id=int(r["id"]),
            seq_no=int(r["seq_no"] or 0),
            asset_url=str(r["asset_url"]),
            title=str(r["title"]),
            creator=str(r["creator"]),
            year=str(r["year"]),
            description=str(r["description"]),
            thumbnail_url=str(r["thumbnail_url"]),
            created_at=str(r["created_at"]),
            updated_at=str(r["updated_at"]),
        )

    def delete_artworks(self, artwork_ids: Iterable[int]) -> None:
        ids = [int(i) for i in artwork_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as con:
            con.execute(f"DELETE FROM artworks WHERE id IN ({placeholders})", ids)
