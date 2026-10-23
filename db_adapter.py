"""
Neon PostgreSQL Database Adapter for RoomRadar
Supports both PostgreSQL (Neon) and SQLite fallback.
Provides native PostgreSQL 64-bit bitwise Hamming distance queries:
  bit_count((phash_int64 # query_hash)::bit(64))
"""

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

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


class DBConnection:
    """Unified wrapper around SQLite and PostgreSQL."""

    def __init__(self, db_url: Optional[str] = None):
        self.url = db_url or DATABASE_URL
        self.is_pg = bool(self.url and HAS_POSTGRES)

        if self.is_pg:
            # Clean channel_binding parameter if needed
            clean_url = self.url.split("&channel_binding=")[0]
            self.conn = psycopg2.connect(clean_url, cursor_factory=RealDictCursor)
            self.conn.autocommit = True
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            from roomradar_matcher import hamming_distance_int64
            self.conn.create_function("HAMMING_DIST", 2, hamming_distance_int64)

        self.init_schema()

    def init_schema(self):
        if self.is_pg:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS physical_rooms (
                        room_id SERIAL PRIMARY KEY,
                        outcode VARCHAR(10) NOT NULL,
                        street_hint VARCHAR(255),
                        full_address VARCHAR(255),
                        postcode VARCHAR(12),
                        latitude DOUBLE PRECISION,
                        longitude DOUBLE PRECISION,
                        canonical_label VARCHAR(255) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS listing_snapshots (
                        snapshot_id SERIAL PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES physical_rooms(room_id) ON DELETE CASCADE,
                        portal VARCHAR(50) NOT NULL,
                        external_ad_id VARCHAR(100) NOT NULL,
                        url TEXT NOT NULL,
                        rent_pcm INTEGER,
                        bills_included INTEGER DEFAULT 1,
                        advertiser_uid VARCHAR(100),
                        scraped_at TIMESTAMPTZ NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS image_fingerprints (
                        fingerprint_id SERIAL PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES physical_rooms(room_id) ON DELETE CASCADE,
                        snapshot_id INTEGER NOT NULL REFERENCES listing_snapshots(snapshot_id) ON DELETE CASCADE,
                        image_url TEXT NOT NULL,
                        caption TEXT,
                        is_communal INTEGER NOT NULL DEFAULT 0,
                        phash_hex VARCHAR(32) NOT NULL,
                        phash_int64 BIGINT NOT NULL,
                        dhash_hex VARCHAR(32) NOT NULL,
                        dhash_int64 BIGINT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS room_reviews (
                        review_id SERIAL PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES physical_rooms(room_id) ON DELETE CASCADE,
                        tenancy_period VARCHAR(100) NOT NULL,
                        landlord_or_agency VARCHAR(255) DEFAULT '',
                        noise_isolation INTEGER NOT NULL,
                        winter_warmth_damp INTEGER NOT NULL,
                        water_pressure INTEGER NOT NULL,
                        deposit_fairness INTEGER NOT NULL,
                        agency_communication INTEGER NOT NULL DEFAULT 3,
                        rent_paid_pcm INTEGER NOT NULL,
                        comment TEXT NOT NULL,
                        verified INTEGER NOT NULL DEFAULT 1,
                        embargo_until DATE NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_pg_outcode ON physical_rooms(outcode);
                    CREATE INDEX IF NOT EXISTS idx_pg_ad_id ON listing_snapshots(portal, external_ad_id);
                    """
                )
        else:
            with self.conn:
                self.conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS physical_rooms (
                        room_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        outcode TEXT NOT NULL,
                        street_hint TEXT,
                        full_address TEXT,
                        postcode TEXT,
                        latitude REAL,
                        longitude REAL,
                        canonical_label TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS listing_snapshots (
                        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id INTEGER NOT NULL,
                        portal TEXT NOT NULL,
                        external_ad_id TEXT NOT NULL,
                        url TEXT NOT NULL,
                        rent_pcm INTEGER,
                        bills_included INTEGER,
                        advertiser_uid TEXT,
                        scraped_at TEXT NOT NULL,
                        FOREIGN KEY (room_id) REFERENCES physical_rooms(room_id)
                    );

                    CREATE TABLE IF NOT EXISTS image_fingerprints (
                        fingerprint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id INTEGER NOT NULL,
                        snapshot_id INTEGER NOT NULL,
                        image_url TEXT NOT NULL,
                        caption TEXT,
                        is_communal INTEGER NOT NULL DEFAULT 0,
                        phash_hex TEXT NOT NULL,
                        phash_int64 INTEGER NOT NULL,
                        dhash_hex TEXT NOT NULL,
                        dhash_int64 INTEGER NOT NULL,
                        FOREIGN KEY (room_id) REFERENCES physical_rooms(room_id),
                        FOREIGN KEY (snapshot_id) REFERENCES listing_snapshots(snapshot_id)
                    );

                    CREATE TABLE IF NOT EXISTS room_reviews (
                        review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id INTEGER NOT NULL,
                        tenancy_period TEXT NOT NULL,
                        landlord_or_agency TEXT DEFAULT '',
                        noise_isolation INTEGER NOT NULL,
                        winter_warmth_damp INTEGER NOT NULL,
                        water_pressure INTEGER NOT NULL,
                        deposit_fairness INTEGER NOT NULL,
                        agency_communication INTEGER NOT NULL DEFAULT 3,
                        rent_paid_pcm INTEGER NOT NULL,
                        comment TEXT NOT NULL,
                        verified INTEGER NOT NULL DEFAULT 1,
                        embargo_until TEXT NOT NULL,
                        FOREIGN KEY (room_id) REFERENCES physical_rooms(room_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_outcode ON physical_rooms(outcode);
                    CREATE INDEX IF NOT EXISTS idx_ad_id ON listing_snapshots(portal, external_ad_id);
                    """
                )

    def execute(self, query: str, params: Union[Tuple, List] = ()) -> Any:
        if self.is_pg:
            # Convert SQLite ? parameters to Postgres %s
            pg_query = query.replace("?", "%s")
            cur = self.conn.cursor()
            cur.execute(pg_query, params)
            return cur
        else:
            return self.conn.execute(query, params)

    def close(self):
        self.conn.close()
