"""
Unified Database Access Layer for RoomRadar
Seamlessly supports both PostgreSQL (Neon cloud) and SQLite (local fallback).
Uses native PostgreSQL 64-bit bitwise Hamming distance operators:
  bit_count((phash_int64 # %s)::bit(64))
"""

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

DATABASE_URL = os.environ.get("DATABASE_URL")
_default_sqlite = "roomradar.db" if os.path.exists("roomradar.db") else "exeter_hmo_tracker.db"
DB_PATH = os.environ.get("DB_PATH", _default_sqlite)


def is_postgres() -> bool:
    return bool(DATABASE_URL and HAS_POSTGRES)


def get_connection():
    """Returns an active, configured connection to either Postgres or SQLite."""
    if is_postgres():
        clean_url = DATABASE_URL.split("&channel_binding=")[0]
        conn = psycopg2.connect(clean_url, cursor_factory=RealDictCursor)
        conn.autocommit = True
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        from roomradar_matcher import hamming_distance_int64
        conn.create_function("HAMMING_DIST", 2, hamming_distance_int64)
        return conn


def execute_query(conn: Any, query: str, params: Union[Tuple, List] = ()) -> Any:
    """Executes a query converting SQLite ? syntax to Postgres %s if using Postgres."""
    if is_postgres():
        pg_query = query.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(pg_query, params)
        return cur
    else:
        return conn.execute(query, params)


def fetch_one(cur_or_res: Any) -> Optional[Dict[str, Any]]:
    """Safely fetches one dictionary row from either cursor."""
    row = cur_or_res.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def fetch_all(cur_or_res: Any) -> List[Dict[str, Any]]:
    """Safely fetches all rows as dictionaries."""
    rows = cur_or_res.fetchall()
    return [dict(r) for r in rows]
