"""Local SQLite storage for the focus list and dismissed suggestions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS focus (
    title    TEXT PRIMARY KEY,
    note     TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dismissed (
    title        TEXT NOT NULL,
    key          TEXT NOT NULL,
    message      TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (title, key)
);
DROP TABLE IF EXISTS unwatched;
"""


@dataclass
class FocusItem:
    title: str
    note: str
    added_at: datetime


@dataclass
class DismissedItem:
    key: str
    message: str
    dismissed_at: datetime


class Store:
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- focus list --------------------------------------------------------

    def focus_items(self) -> list[FocusItem]:
        rows = self._db.execute("SELECT title, note, added_at FROM focus ORDER BY added_at DESC")
        return [FocusItem(t, n, datetime.fromisoformat(a)) for t, n, a in rows]

    def focus_titles(self) -> set[str]:
        return {t for (t,) in self._db.execute("SELECT title FROM focus")}

    def add_focus(self, title: str, note: str = "") -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO focus (title, note, added_at) VALUES (?, ?, ?) "
                "ON CONFLICT(title) DO UPDATE SET note = excluded.note",
                (title, note, _now()),
            )

    def remove_focus(self, title: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM focus WHERE title = ?", (title,))

    # -- dismissed suggestions ---------------------------------------------

    def dismissed(self, title: str) -> dict[str, DismissedItem]:
        rows = self._db.execute(
            "SELECT key, message, dismissed_at FROM dismissed WHERE title = ?", (title,))
        return {k: DismissedItem(k, m, datetime.fromisoformat(a)) for k, m, a in rows}

    def dismiss(self, title: str, key: str, message: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO dismissed (title, key, message, dismissed_at) "
                "VALUES (?, ?, ?, ?)", (title, key, message, _now()))

    def restore(self, title: str, key: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM dismissed WHERE title = ? AND key = ?", (title, key))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
