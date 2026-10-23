"""
One-time script to migrate all local SQLite rooms, snapshots,
fingerprints, and reviews into the Neon PostgreSQL cloud database.
"""

import os
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SQLITE_PATH = "exeter_hmo_tracker.db" if os.path.exists("exeter_hmo_tracker.db") else "roomradar.db"
NEON_URL = os.environ.get("DATABASE_URL")

def migrate():
    print("Reading local SQLite data from:", SQLITE_PATH)
    s_conn = sqlite3.connect(SQLITE_PATH)
    s_conn.row_factory = sqlite3.Row

    print("Connecting to Neon PostgreSQL...")
    p_conn = psycopg2.connect(NEON_URL)
    p_conn.autocommit = True
    p_cur = p_conn.cursor()

    # Create tables in Neon if not exist
    import db_adapter
    db = db_adapter.DBConnection(NEON_URL)
    print("Neon schema verified.")

    # 1. Migrate physical_rooms
    rooms = s_conn.execute("SELECT * FROM physical_rooms ORDER BY room_id ASC").fetchall()
    print(f"Migrating {len(rooms)} physical_rooms...")
    for r in rooms:
        p_cur.execute(
            """
            INSERT INTO physical_rooms (room_id, outcode, street_hint, canonical_label, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (room_id) DO UPDATE
            SET outcode = EXCLUDED.outcode, street_hint = EXCLUDED.street_hint, canonical_label = EXCLUDED.canonical_label
            """,
            (r["room_id"], r["outcode"], r["street_hint"], r["canonical_label"], r["created_at"])
        )
    p_cur.execute("SELECT setval('physical_rooms_room_id_seq', (SELECT MAX(room_id) FROM physical_rooms));")

    # 2. Migrate listing_snapshots
    snapshots = s_conn.execute("SELECT * FROM listing_snapshots ORDER BY snapshot_id ASC").fetchall()
    print(f"Migrating {len(snapshots)} listing_snapshots...")
    for s in snapshots:
        p_cur.execute(
            """
            INSERT INTO listing_snapshots (snapshot_id, room_id, portal, external_ad_id, url, rent_pcm, bills_included, advertiser_uid, scraped_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            (s["snapshot_id"], s["room_id"], s["portal"], s["external_ad_id"], s["url"], s["rent_pcm"], s["bills_included"], s["advertiser_uid"], s["scraped_at"])
        )
    p_cur.execute("SELECT setval('listing_snapshots_snapshot_id_seq', (SELECT MAX(snapshot_id) FROM listing_snapshots));")

    # 3. Migrate image_fingerprints
    fingerprints = s_conn.execute("SELECT * FROM image_fingerprints ORDER BY fingerprint_id ASC").fetchall()
    print(f"Migrating {len(fingerprints)} image_fingerprints...")
    for f in fingerprints:
        caption = f["caption"] if "caption" in f.keys() else ""
        is_comm = f["is_communal"] if "is_communal" in f.keys() else 0
        p_cur.execute(
            """
            INSERT INTO image_fingerprints (fingerprint_id, room_id, snapshot_id, image_url, caption, is_communal, phash_hex, phash_int64, dhash_hex, dhash_int64)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint_id) DO NOTHING
            """,
            (f["fingerprint_id"], f["room_id"], f["snapshot_id"], f["image_url"], caption, is_comm, f["phash_hex"], f["phash_int64"], f["dhash_hex"], f["dhash_int64"])
        )
    p_cur.execute("SELECT setval('image_fingerprints_fingerprint_id_seq', (SELECT MAX(fingerprint_id) FROM image_fingerprints));")

    # 4. Migrate room_reviews
    reviews = s_conn.execute("SELECT * FROM room_reviews ORDER BY review_id ASC").fetchall()
    print(f"Migrating {len(reviews)} room_reviews...")
    for rev in reviews:
        p_cur.execute(
            """
            INSERT INTO room_reviews (review_id, room_id, tenancy_period, landlord_or_agency, noise_isolation, winter_warmth_damp, water_pressure, deposit_fairness, agency_communication, rent_paid_pcm, comment, verified, embargo_until)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (review_id) DO NOTHING
            """,
            (rev["review_id"], rev["room_id"], rev["tenancy_period"], rev["landlord_or_agency"], rev["noise_isolation"], rev["winter_warmth_damp"], rev["water_pressure"], rev["deposit_fairness"], rev["agency_communication"], rev["rent_paid_pcm"], rev["comment"], rev["verified"], rev["embargo_until"])
        )
    p_cur.execute("SELECT setval('room_reviews_review_id_seq', (SELECT MAX(review_id) FROM room_reviews));")

    print("\n[SUCCESS] All local data successfully migrated into Neon PostgreSQL cloud database!")
    s_conn.close()
    p_conn.close()

if __name__ == "__main__":
    migrate()
