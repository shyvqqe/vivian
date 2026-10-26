from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Callable, TypeVar


T = TypeVar("T")


def database_path() -> Path:
    directory = Path(os.environ.get("VIVIAN_DATA_DIR") or Path(__file__).resolve().parents[1] / "data")
    return directory.expanduser().resolve() / "vivian.sqlite3"


def _transaction(callback: Callable[[sqlite3.Connection], T]) -> T:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        result = callback(connection)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


async def run(callback: Callable[[sqlite3.Connection], T]) -> T:
    return await asyncio.to_thread(_transaction, callback)
