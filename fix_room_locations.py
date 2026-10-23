"""
Backfill & Fix Room Locations in Neon PostgreSQL Database
1. Eliminates all occurrences of 'Unknown Street' by resolving the real neighborhood from listing URLs or outcode wards.
2. Eliminates all bogus 'Exeter' addresses for properties in Manchester, Bristol, Dawlish, Barnstaple, etc.
3. Fixes latitude and longitude coordinates so all rooms are accurately pinned in their real city.
"""

import os
import re
from dotenv import load_dotenv

load_dotenv()
import db_adapter
import geocoder

def run_fix():
    db = db_adapter.DBConnection()
    cur = db.conn.cursor()
    cur.execute("""
        SELECT pr.room_id, pr.outcode, pr.street_hint, pr.full_address, pr.canonical_label, pr.latitude, pr.longitude, ls.url 
        FROM physical_rooms pr
        LEFT JOIN listing_snapshots ls ON ls.room_id = pr.room_id
        ORDER BY pr.room_id ASC;
    """)

    seen = set()
    updates = []

    for r in cur.fetchall():
        rid = r["room_id"]
        if rid in seen:
            continue
        seen.add(rid)

        outcode = (r["outcode"] or "EX4").strip().upper()
        old_street = (r["street_hint"] or "").strip()
        old_addr = (r["full_address"] or "").strip()
        old_label = (r["canonical_label"] or "").strip()
        url = (r["url"] or "").strip()

        # Extract area from URL if available
        url_m = re.search(r"/flatshare/([^/]+)/([^/]+)/", url)
        url_region = url_m.group(1).replace("_", " ").title() if url_m else ""
        url_area = url_m.group(2).replace("_", " ").title() if url_m else ""

        # Determine real street or neighborhood
        new_street = old_street
        if not old_street or old_street.lower() in ("unknown street", "shared house", "unknown"):
            new_street = url_area or f"{outcode} Area"

        # Determine true city
        detected_city = geocoder.guess_city_from_outcode(outcode)

        # Geocode clean coordinates and address
        geo = geocoder.geocode_location(postcode=outcode, street_hint=new_street, area_hint=url_area, city=detected_city)
        new_addr = geo["full_address"]
        new_lat = geo["latitude"]
        new_lng = geo["longitude"]

        # Clean canonical label
        new_label = old_label.replace("Unknown Street", new_street)
        if "Exeter" in new_label and not outcode.startswith("EX"):
            new_label = new_label.replace("Exeter", detected_city)

        updates.append((new_street, new_addr, new_lat, new_lng, new_label, rid))

    print(f"Applying updates to {len(updates)} rooms in Neon PostgreSQL...")
    for new_street, new_addr, new_lat, new_lng, new_label, rid in updates:
        cur.execute("""
            UPDATE physical_rooms
            SET street_hint = %s,
                full_address = %s,
                latitude = %s,
                longitude = %s,
                canonical_label = %s
            WHERE room_id = %s;
        """, (new_street, new_addr, new_lat, new_lng, new_label, rid))

    print("Successfully updated all room locations in Neon!")

if __name__ == "__main__":
    run_fix()
