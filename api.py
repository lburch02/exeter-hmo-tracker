import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db_adapter
import geocoder
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# Import the hashing helpers, DB layer, and threshold from our prototype
from roomradar_matcher import (
    HAMMING_DISTANCE_THRESHOLD,
    RoomFingerprintDB,
    compute_image_fingerprints,
    hamming_distance_int64,
    is_communal_caption,
    normalize_ad_id,
)

BASE_DIR = Path(__file__).resolve().parent
_default_sqlite = str(BASE_DIR / "roomradar.db") if (BASE_DIR / "roomradar.db").exists() else str(BASE_DIR / "exeter_hmo_tracker.db")
DB_PATH = os.environ.get("DB_PATH", _default_sqlite)
DASHBOARD_HTML_PATH = BASE_DIR / "dashboard.html"


# =====================================================================
# 1. REQUEST & RESPONSE SCHEMAS
# =====================================================================

class ListingLookupRequest(BaseModel):
    portal: str = "spareroom"
    external_ad_id: str
    outcode: str = "EX4"
    postcode: Optional[str] = None
    street_hint: Optional[str] = None
    full_address: Optional[str] = None
    current_rent_pcm: Optional[int] = None
    image_urls: List[str] = Field(default_factory=list)


class ReviewSubmitRequest(BaseModel):
    portal: str = "spareroom"
    external_ad_id: str
    outcode: str = "EX4"
    postcode: Optional[str] = None
    street_hint: str = "Unknown Street"
    full_address: Optional[str] = None
    floor_level: str = "First Floor"
    room_aspect: str = "Rear (Garden View)"
    tenancy_period: str
    rent_paid_pcm: int
    landlord_or_agency: str = ""
    noise_isolation: int = Field(..., ge=1, le=5)
    winter_warmth_damp: int = Field(..., ge=1, le=5)
    water_pressure: int = Field(..., ge=1, le=5)
    deposit_fairness: int = Field(..., ge=1, le=5)
    agency_communication: int = Field(3, ge=1, le=5)
    comment: str
    hold_for_deposit_30d: bool = False
    image_urls: List[str] = Field(default_factory=list)


class ReviewSubmitResponse(BaseModel):
    ok: bool
    room_id: int
    review_id: int
    embargoed: bool
    embargo_until: str
    canonical_label: str


class ReviewItem(BaseModel):
    tenancy_period: str
    landlord_or_agency: Optional[str] = None
    noise_isolation: int
    winter_warmth_damp: int
    water_pressure: int
    deposit_fairness: int
    agency_communication: int
    rent_paid_pcm: int
    comment: str
    verified: bool


class RoomMatchResponse(BaseModel):
    matched: bool
    match_method: Optional[str] = None  # "EXACT_AD_ID" or "PERCEPTUAL_IMAGE_HASH"
    hamming_distance: Optional[int] = None
    room_id: Optional[int] = None
    canonical_label: Optional[str] = None
    street_hint: Optional[str] = None
    full_address: Optional[str] = None
    postcode: Optional[str] = None
    outcode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    times_listed_18m: int = 0
    earliest_rent_pcm: Optional[int] = None
    rent_change_pct: Optional[float] = None
    avg_scores: Optional[dict] = None
    reviews: List[ReviewItem] = Field(default_factory=list)


# Multi-city HMO street coordinates directory (starting with Exeter, scalable across UK student/rental markets)
STREET_COORDINATES: Dict[str, tuple] = {
    # Exeter
    "Monks Road": (50.7318, -3.5098, "Exeter — Mount Pleasant / Polsloe"),
    "Pennsylvania Road": (50.7352, -3.5248, "Exeter — St James & Pennsylvania"),
    "Victoria Street": (50.7336, -3.5212, "Exeter — St James"),
    "Springfield Road": (50.7341, -3.5190, "Exeter — St James"),
    "Old Tiverton Road": (50.7329, -3.5162, "Exeter — St James / Sidwell"),
    "Culverland Road": (50.7320, -3.5225, "Exeter — St James"),
    "Pinhoe Road": (50.7340, -3.5040, "Exeter — Polsloe"),
    "Manston Road": (50.7258, -3.5032, "Exeter — Mount Pleasant / Heavitree"),
    "Fore Street": (50.7195, -3.5015, "Exeter — Heavitree"),
    "Magdalen Road": (50.7202, -3.5195, "Exeter — St Leonard's"),
    "Howell Road": (50.7302, -3.5328, "Exeter — St David's"),
    "Blackall Road": (50.7314, -3.5290, "Exeter — St David's / Pennsylvania"),
    "Sidwell Street": (50.7278, -3.5220, "Exeter — City Centre"),
    # Scalable expansion anchors (Bristol, Manchester, Leeds, Birmingham)
    "Whiteladies Road": (51.4642, -2.6083, "Bristol — Clifton"),
    "Gloucester Road": (51.4789, -2.5894, "Bristol — Bishopston"),
    "Wilmslow Road": (53.4475, -2.2190, "Manchester — Fallowfield"),
    "Oxford Road": (53.4660, -2.2330, "Manchester — Student Corridor"),
    "Otley Road": (53.8202, -1.5794, "Leeds — Headingley"),
    "Cardigan Road": (53.8122, -1.5732, "Leeds — Hyde Park"),
    "Bristol Road": (52.4430, -1.9360, "Birmingham — Selly Oak"),
}
# Backward-compatible alias
EXETER_STREET_COORDS = STREET_COORDINATES


# =====================================================================
# 2. DATABASE CONNECTION & SEEDER (With Exeter Streets & Reviews)
# =====================================================================

def get_db_connection():
    return db_adapter.DBConnection()


def init_and_seed_reviews():
    """Ensures all base tables + room_reviews exist and seeds realistic Exeter HMO rooms."""
    fingerprint_db = RoomFingerprintDB(DB_PATH)
    if fingerprint_db.is_pg:
        # PostgreSQL schema is already created and maintained via db_adapter
        return
    conn = fingerprint_db.conn
    with conn:
        conn.executescript(
            """
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
            """
        )
        # Safe migration for existing databases created before agency_communication / landlord_or_agency
        existing_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(room_reviews)").fetchall()
        }
        if "agency_communication" not in existing_cols:
            conn.execute(
                "ALTER TABLE room_reviews ADD COLUMN agency_communication INTEGER NOT NULL DEFAULT 3"
            )
        if "landlord_or_agency" not in existing_cols:
            conn.execute(
                "ALTER TABLE room_reviews ADD COLUMN landlord_or_agency TEXT DEFAULT ''"
            )

        # Normalize any legacy 'SR_...' external_ad_ids in listing_snapshots
        legacy_rows = conn.execute(
            "SELECT snapshot_id, external_ad_id FROM listing_snapshots WHERE external_ad_id LIKE 'SR_%'"
        ).fetchall()
        for r in legacy_rows:
            conn.execute(
                "UPDATE listing_snapshots SET external_ad_id = ? WHERE snapshot_id = ?",
                (normalize_ad_id(r["external_ad_id"]), r["snapshot_id"]),
            )

        # Seed Exeter HMO rooms & verified reviews if DB has fewer than 3 reviews
        count = conn.execute("SELECT COUNT(*) FROM room_reviews").fetchone()[0]
        if count == 0:
            now_iso = datetime.now(timezone.utc).isoformat()
            demo_rooms = [
                (1, "EX4", "Monks Road", "Ground Floor Front Bay-Window Room", now_iso),
                (2, "EX4", "Monks Road", "First Floor Rear Garden-View Room", now_iso),
                (3, "EX4", "Pennsylvania Road", "Attic / Loft Ensuite Room", now_iso),
                (4, "EX2", "Fore Street", "First Floor Front Double (Heavitree)", now_iso),
                (5, "EX4", "Victoria Street", "Basement Street-Side Room", now_iso),
                (6, "EX1", "Manston Road", "First Floor Quiet Double (3-Bed House)", now_iso),
            ]
            for r_id, outc, street, label, created in demo_rooms:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO physical_rooms (room_id, outcode, street_hint, canonical_label, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (r_id, outc, street, label, created),
                )

            demo_snapshots = [
                # Room 1: Monks Road Ground Floor Front (SpareRoom -> relisted on Exeter Studentpad!)
                (1, "spareroom", "15900112", "https://www.spareroom.co.uk/15900112", 595, "ll_99", "2024-09-10T12:00:00Z"),
                (1, "studentpad", "884120", "https://www.exeterstudentpad.co.uk/Accommodation/Details/884120", 640, "ll_99", "2025-07-15T12:00:00Z"),
                (1, "spareroom", "17488920", "https://www.spareroom.co.uk/17488920", 685, "ll_99", "2026-08-01T12:00:00Z"),
                # Room 2: Monks Road First Floor Rear
                (2, "spareroom", "15900115", "https://www.spareroom.co.uk/15900115", 530, "ll_99", "2024-09-10T14:30:00Z"),
                (2, "studentpad", "884125", "https://www.exeterstudentpad.co.uk/Accommodation/Details/884125", 560, "ll_99", "2026-07-20T10:00:00Z"),
                # Room 3: Pennsylvania Road Attic Room (Studentpad & UniHomes)
                (3, "studentpad", "871040", "https://www.exeterstudentpad.co.uk/Accommodation/Details/871040", 650, "star_exeter", "2024-10-01T12:00:00Z"),
                (3, "unihomes", "99201", "https://www.unihomes.co.uk/property/exeter/99201", 725, "star_exeter", "2026-08-10T12:00:00Z"),
                # Room 4: Fore Street Heavitree Professional Share (SpareRoom & OpenRent)
                (4, "spareroom", "16500888", "https://www.spareroom.co.uk/16500888", 620, "heavitree_lets", "2025-02-01T12:00:00Z"),
                (4, "openrent", "2145990", "https://www.openrent.co.uk/property-to-rent/exeter/2145990", 645, "heavitree_lets", "2026-08-12T12:00:00Z"),
                # Room 5: Victoria Street Basement Room (High turnover across Studentpad, ExeterStudentPads & SpareRoom!)
                (5, "studentpad", "5-victoria-st", "https://exeterstudentpads.co.uk/5-victoria-st/", 540, "gillams_ex4", "2024-08-01T12:00:00Z"),
                (5, "spareroom", "16200002", "https://www.spareroom.co.uk/16200002", 575, "gillams_ex4", "2025-01-15T12:00:00Z"),
                (5, "studentpad", "869222", "https://www.exeterstudentpad.co.uk/Accommodation/Details/869222", 610, "gillams_ex4", "2025-09-01T12:00:00Z"),
                (5, "spareroom", "17550004", "https://www.spareroom.co.uk/17550004", 650, "gillams_ex4", "2026-08-18T12:00:00Z"),
                # Room 6: Manston Road Exeter Studentpad Advert #21135193
                (6, "studentpad", "19840110", "https://www.exeterstudentpad.co.uk/Advert/19840110", 580, "jane_jauncey", "2025-09-01T12:00:00Z"),
                (6, "studentpad", "21135193", "https://www.exeterstudentpad.co.uk/Advert/21135193", 625, "jane_jauncey", "2026-09-01T12:00:00Z"),
            ]
            for r_id, portal_name, ad_id, url, rent, adv_uid, scraped in demo_snapshots:
                exists = conn.execute(
                    "SELECT 1 FROM listing_snapshots WHERE external_ad_id = ?",
                    (ad_id,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """
                        INSERT INTO listing_snapshots
                        (room_id, portal, external_ad_id, url, rent_pcm, bills_included, advertiser_uid, scraped_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (r_id, portal_name, ad_id, url, rent, adv_uid, scraped),
                    )

            conn.execute(
                """
                INSERT INTO room_reviews
                (room_id, tenancy_period, landlord_or_agency, noise_isolation, winter_warmth_damp, water_pressure, deposit_fairness, agency_communication, rent_paid_pcm, comment, verified, embargo_until)
                VALUES
                (1, 'Sep 2024 – Jul 2025', 'Exeter Student Lettings', 2, 2, 4, 3, 2, 595,
                 'Right on Monks Road footpath so you hear every late-night group walking back from town. Black mould forms behind the wardrobe on the bay window wall unless you run a dehumidifier daily. Agency took 3 weeks to reply to maintenance tickets.',
                 1, '2025-08-15'),
                (1, 'Aug 2025 – Jul 2026', 'Exeter Student Lettings', 2, 3, 4, 2, 2, 640,
                 'Good water pressure in the upstairs shower, but the ground floor front room takes ages to warm up in January. Agency turned up for viewings with less than 24h notice and tried to deduct £90 for professional cleaning despite leaving it spotless.',
                 1, '2026-08-15'),
                (2, 'Sep 2024 – Jul 2025', 'Exeter Student Lettings', 5, 4, 4, 3, 3, 530,
                 'Quiet first-floor rear room overlooking the back garden—completely insulated from Monks Road street noise. Small desk space, but stays much warmer than the ground-floor bay room.',
                 1, '2025-08-20'),
                (3, 'Sep 2024 – Jun 2025', 'Star Lettings Exeter', 4, 4, 3, 5, 5, 650,
                 'Great views over Streatham campus from the top floor and very quiet. En-suite shower pressure drops slightly at 8am, but Star Lettings fixed a faulty radiator valve within 24 hours and returned full deposit in 4 days.',
                 1, '2025-07-15'),
                (4, 'Feb 2025 – Jul 2026', 'Heavitree Professional Lets', 4, 5, 5, 5, 5, 620,
                 'Ideal for RD&E hospital staff (10 min walk). Modern combi boiler with instant hot water, double glazing blocks Fore Street buses well, and landlord always gives 48 hours notice via WhatsApp before any maintenance visit.',
                 1, '2026-08-01'),
                (5, 'Sep 2025 – Jun 2026', 'Gillams Lettings', 1, 1, 2, 1, 1, 610,
                 'Avoid the basement room! Constant damp smell even with window open, zero natural light, and you hear footsteps on the floorboards above at all hours. Agency ignored three emails about peeling plaster and contested £180 of deposit.',
                 1, '2026-07-20'),
                (6, 'Sep 2025 – Jul 2026', 'Ms Jane Jauncey (Resident Landlord)', 5, 4, 4, 5, 5, 625,
                 'Well-maintained 3-bed house on Manston Road (15 min walk to St Luke’s). Bills and fast Wi-Fi included in the £625/mo rent. Very quiet residential street compared to Pennsylvania Road and landlord is super helpful.',
                 1, '2026-08-10')
                """
            )
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_and_seed_reviews()
    yield


app = FastAPI(
    title="RoomRadar — Universal HMO Room & Rent Tracking API",
    description="Cross-portal perceptual image matching, historical rent tracking, and tenant reviews for shared houses.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow requests from the Chrome Extension, SpareRoom, Exeter Studentpad, Exeter Student Pads, OpenRent, UniHomes, Rightmove, and localhost
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://([a-z0-9-]+\.)?(spareroom\.co\.uk|exeterstudentpad\.co\.uk|exeterstudentpads\.co\.uk|studentpad\.co\.uk|openrent\.co\.uk|openrent\.com|unihomes\.co\.uk|rightmove\.co\.uk|onrender\.com)|chrome-extension://.*|http://localhost:\d+|http://127\.0\.0\.1:\d+)$",
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


# =====================================================================
# 3. WEB DASHBOARD & SUMMARY API (Exeter Map, Rooms & Agency Leaderboard)
# =====================================================================

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if DASHBOARD_HTML_PATH.exists():
        return HTMLResponse(content=DASHBOARD_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard file not found</h1>", status_code=404)


@app.get("/api/v1/dashboard/summary")
async def get_dashboard_summary():
    conn = get_db_connection()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rooms_rows = conn.execute(
        "SELECT * FROM physical_rooms ORDER BY room_id ASC"
    ).fetchall()

    rooms_payload = []
    agency_stats: Dict[str, dict] = {}

    for rm in rooms_rows:
        r_id = rm["room_id"]
        snapshots = conn.execute(
            """
            SELECT external_ad_id, portal, rent_pcm, scraped_at, url
            FROM listing_snapshots
            WHERE room_id = ?
            ORDER BY scraped_at ASC
            """,
            (r_id,),
        ).fetchall()

        reviews = conn.execute(
            """
            SELECT * FROM room_reviews
            WHERE room_id = ? AND embargo_until <= ?
            ORDER BY review_id DESC
            """,
            (r_id, today_iso),
        ).fetchall()

        earliest_rent = snapshots[0]["rent_pcm"] if snapshots else None
        latest_rent = snapshots[-1]["rent_pcm"] if snapshots else None
        latest_ad_id = snapshots[-1]["external_ad_id"] if snapshots else None
        rent_change_pct = 0.0
        if earliest_rent and latest_rent and earliest_rent > 0:
            rent_change_pct = round(((latest_rent - earliest_rent) / earliest_rent) * 100, 1)

        avg_scores = None
        overall_score = None
        primary_agency = None
        review_items = []

        for r in reviews:
            agency_name = (r["landlord_or_agency"] or "").strip()
            if agency_name and not primary_agency:
                primary_agency = agency_name

            if agency_name:
                if agency_name not in agency_stats:
                    agency_stats[agency_name] = {
                        "name": agency_name,
                        "review_count": 0,
                        "comm_sum": 0,
                        "deposit_sum": 0,
                        "warmth_sum": 0,
                    }
                agency_stats[agency_name]["review_count"] += 1
                agency_stats[agency_name]["comm_sum"] += r["agency_communication"]
                agency_stats[agency_name]["deposit_sum"] += r["deposit_fairness"]
                agency_stats[agency_name]["warmth_sum"] += r["winter_warmth_damp"]

            review_items.append(
                {
                    "tenancy_period": r["tenancy_period"],
                    "landlord_or_agency": agency_name or None,
                    "noise_isolation": r["noise_isolation"],
                    "winter_warmth_damp": r["winter_warmth_damp"],
                    "water_pressure": r["water_pressure"],
                    "deposit_fairness": r["deposit_fairness"],
                    "agency_communication": r["agency_communication"],
                    "rent_paid_pcm": r["rent_paid_pcm"],
                    "comment": r["comment"],
                    "verified": bool(r["verified"]),
                }
            )

        if review_items:
            n = len(review_items)
            avg_scores = {
                "noise": round(sum(x["noise_isolation"] for x in review_items) / n, 1),
                "warmth_damp": round(sum(x["winter_warmth_damp"] for x in review_items) / n, 1),
                "water_pressure": round(sum(x["water_pressure"] for x in review_items) / n, 1),
                "deposit_fairness": round(sum(x["deposit_fairness"] for x in review_items) / n, 1),
                "agency_comm": round(sum(x["agency_communication"] for x in review_items) / n, 1),
            }
            overall_score = round(sum(avg_scores.values()) / 5.0, 1)

        street = rm["street_hint"] or ""
        postcode = rm.get("postcode") or rm["outcode"]
        full_address = rm.get("full_address") or (f"{street}, {postcode}" if street else postcode)
        lat = rm.get("latitude")
        lng = rm.get("longitude")
        if lat is None or lng is None:
            geo = geocoder.geocode_location(postcode=postcode, street_hint=street, full_address=full_address)
            lat = geo["latitude"]
            lng = geo["longitude"]
            zone = geo["zone"]
        else:
            city_guess = geocoder.guess_city_from_outcode(rm["outcode"])
            zone = f"{city_guess} — {street or rm['outcode']}"

        rooms_payload.append(
            {
                "room_id": r_id,
                "outcode": rm["outcode"],
                "postcode": postcode,
                "street_hint": street,
                "full_address": full_address,
                "zone": zone,
                "canonical_label": rm["canonical_label"],
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "times_listed": len(snapshots),
                "earliest_rent_pcm": earliest_rent,
                "latest_rent_pcm": latest_rent,
                "latest_ad_id": latest_ad_id,
                "rent_change_pct": rent_change_pct,
                "rent_history": [
                    {
                        "portal": s["portal"],
                        "ad_id": s["external_ad_id"],
                        "rent_pcm": s["rent_pcm"],
                        "date": s["scraped_at"].strftime("%Y-%m-%d") if hasattr(s["scraped_at"], "strftime") else str(s["scraped_at"])[:10],
                    }
                    for s in snapshots
                ],
                "primary_agency": primary_agency or "Independent / Unspecified",
                "overall_score": overall_score,
                "avg_scores": avg_scores,
                "reviews": review_items,
            }
        )

    # Fetch real online reviews from allAgents & Trustpilot
    try:
        external_rows = conn.execute(
            """
            SELECT agency_name, city, source, reviewer_name, review_date, star_rating, recommend, rent_paid_pcm, postcode, comment, source_url
            FROM agency_reviews
            ORDER BY agency_review_id DESC
            """
        ).fetchall()
    except Exception:
        external_rows = []

    conn.close()

    # Aggregate external online reviews by agency
    external_stats: Dict[str, dict] = {}
    for er in external_rows:
        ag_name = er["agency_name"]
        if ag_name not in external_stats:
            external_stats[ag_name] = {
                "name": ag_name,
                "city": er["city"],
                "source": er["source"],
                "review_count": 0,
                "star_sum": 0,
                "recommend_yes": 0,
                "recent_reviews": [],
            }
        external_stats[ag_name]["review_count"] += 1
        external_stats[ag_name]["star_sum"] += er["star_rating"]
        if er.get("recommend") == "Yes":
            external_stats[ag_name]["recommend_yes"] += 1
        if len(external_stats[ag_name]["recent_reviews"]) < 4:
            external_stats[ag_name]["recent_reviews"].append({
                "reviewer": er["reviewer_name"],
                "date": er["review_date"],
                "rating": er["star_rating"],
                "recommend": er["recommend"],
                "rent": er.get("rent_paid_pcm"),
                "postcode": er.get("postcode"),
                "comment": er["comment"][:220] + ("..." if len(er["comment"]) > 220 else ""),
            })

    agency_leaderboard = []
    # 1. Add agencies from internal room reviews
    for ag in agency_stats.values():
        cnt = ag["review_count"]
        comm_avg = round(ag["comm_sum"] / cnt, 1)
        dep_avg = round(ag["deposit_sum"] / cnt, 1)
        warm_avg = round(ag["warmth_sum"] / cnt, 1)
        # Check if matching external stats exist
        ext = external_stats.get(ag["name"])
        agency_leaderboard.append(
            {
                "name": ag["name"],
                "review_count": cnt,
                "agency_comm_avg": comm_avg,
                "deposit_fairness_avg": dep_avg,
                "warmth_damp_avg": warm_avg,
                "combined_score": round((comm_avg + dep_avg + warm_avg) / 3.0, 1),
                "external_reviews": ext["recent_reviews"] if ext else [],
                "external_count": ext["review_count"] if ext else 0,
                "external_avg": round(ext["star_sum"] / ext["review_count"], 1) if ext else None,
                "source": ext["source"] if ext else "RoomRadar",
            }
        )

    # 2. Add national/regional agencies from allAgents not yet in local room reviews
    for name, ext in external_stats.items():
        if not any(a["name"].lower() == name.lower() for a in agency_leaderboard):
            star_avg = round(ext["star_sum"] / ext["review_count"], 1)
            agency_leaderboard.append(
                {
                    "name": name,
                    "review_count": ext["review_count"],
                    "agency_comm_avg": star_avg,
                    "deposit_fairness_avg": star_avg,
                    "warmth_damp_avg": star_avg,
                    "combined_score": star_avg,
                    "external_reviews": ext["recent_reviews"],
                    "external_count": ext["review_count"],
                    "external_avg": star_avg,
                    "source": ext["source"],
                    "city": ext["city"],
                }
            )

    agency_leaderboard.sort(key=lambda x: x["combined_score"], reverse=True)

    return {
        "total_rooms": len(rooms_payload),
        "total_reviews": sum(len(r["reviews"]) for r in rooms_payload) + len(external_rows),
        "rooms": rooms_payload,
        "agencies": agency_leaderboard,
        "external_reviews_count": len(external_rows),
    }


@app.get("/api/v1/agencies/reviews")
async def get_agency_reviews(agency_name: Optional[str] = None):
    conn = get_db_connection()
    if agency_name:
        rows = conn.execute(
            "SELECT * FROM agency_reviews WHERE lower(agency_name) LIKE lower(?) ORDER BY agency_review_id DESC",
            (f"%{agency_name.strip()}%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM agency_reviews ORDER BY agency_review_id DESC LIMIT 100"
        ).fetchall()
    conn.close()
    return {"total": len(rows), "reviews": [dict(r) for r in rows]}


# =====================================================================
# 4. DUAL-STAGE LOOKUP ENDPOINT (Ad ID -> Live Image pHash/dHash Fallback)
# =====================================================================

@app.post("/api/v1/lookup/spareroom", response_model=RoomMatchResponse)
async def lookup_spareroom_listing(payload: ListingLookupRequest):
    fingerprint_db = RoomFingerprintDB(DB_PATH)
    conn = fingerprint_db.conn
    room_id: Optional[int] = None
    match_method: Optional[str] = None
    hamming_dist: Optional[int] = None

    clean_ad_id = normalize_ad_id(payload.external_ad_id)

    # STAGE 1: Fast lookup by known external_ad_id (supports cross-portal IDs & slugs)
    row = conn.execute(
        """
        SELECT room_id FROM listing_snapshots
        WHERE external_ad_id IN (?, ?)
        ORDER BY CASE WHEN portal = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (clean_ad_id, payload.external_ad_id.strip(), payload.portal),
    ).fetchone()

    if row:
        room_id = row["room_id"]
        match_method = "EXACT_AD_ID"
        hamming_dist = 0

    # STAGE 2: Live Perceptual Image Hash Fallback (composite pHash OR dHash)
    if room_id is None and payload.image_urls:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for img_url in payload.image_urls[:4]:
                try:
                    resp = await client.get(img_url)
                    if resp.status_code != 200:
                        continue
                    _, p_int, _, d_int = compute_image_fingerprints(resp.content)

                    hit = fingerprint_db.find_matching_room_by_hash(
                        p_int, d_int, payload.outcode.upper()
                    )

                    if hit:
                        room_id = hit["room_id"]
                        match_method = "PERCEPTUAL_IMAGE_HASH"
                        hamming_dist = min(hit["p_dist"], hit["d_dist"])
                        break
                except Exception:
                    continue

    # STAGE 3: Street Address Fallback (e.g., browsing a Studentpad property page on a known street)
    if (
        room_id is None
        and payload.street_hint
        and payload.street_hint not in ("Unknown Street", "Exeter Shared House", "Shared House")
        and clean_ad_id not in ("accommodation", "exeter-studentpad", "studentpad")
    ):
        street_row = conn.execute(
            """
            SELECT room_id FROM physical_rooms
            WHERE lower(street_hint) = lower(?)
            ORDER BY room_id ASC
            LIMIT 1
            """,
            (payload.street_hint.strip(),),
        ).fetchone()
        if street_row:
            room_id = street_row["room_id"]
            match_method = "STREET_ADDRESS_MATCH"
            hamming_dist = 0

    if room_id is None:
        conn.close()
        return RoomMatchResponse(matched=False)

    # Fetch Physical Room metadata & listing history
    room_meta = conn.execute(
        "SELECT * FROM physical_rooms WHERE room_id = ?", (room_id,)
    ).fetchone()

    snapshots = conn.execute(
        """
        SELECT rent_pcm, scraped_at FROM listing_snapshots
        WHERE room_id = ? ORDER BY scraped_at ASC
        """,
        (room_id,),
    ).fetchall()

    earliest_rent = snapshots[0]["rent_pcm"] if snapshots else payload.current_rent_pcm
    latest_rent = payload.current_rent_pcm or (snapshots[-1]["rent_pcm"] if snapshots else None)
    rent_change_pct = None
    if earliest_rent and latest_rent and earliest_rent > 0:
        rent_change_pct = round(((latest_rent - earliest_rent) / earliest_rent) * 100, 1)

    # Fetch un-embargoed Reviews
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    review_rows = conn.execute(
        """
        SELECT * FROM room_reviews
        WHERE room_id = ? AND embargo_until <= ?
        ORDER BY review_id DESC
        """,
        (room_id, today_iso),
    ).fetchall()
    conn.close()

    reviews = [
        ReviewItem(
            tenancy_period=r["tenancy_period"],
            landlord_or_agency=r["landlord_or_agency"] or None,
            noise_isolation=r["noise_isolation"],
            winter_warmth_damp=r["winter_warmth_damp"],
            water_pressure=r["water_pressure"],
            deposit_fairness=r["deposit_fairness"],
            agency_communication=r["agency_communication"],
            rent_paid_pcm=r["rent_paid_pcm"],
            comment=r["comment"],
            verified=bool(r["verified"]),
        )
        for r in review_rows
    ]

    avg_scores = None
    if reviews:
        n = len(reviews)
        avg_scores = {
            "noise": round(sum(r.noise_isolation for r in reviews) / n, 1),
            "warmth_damp": round(sum(r.winter_warmth_damp for r in reviews) / n, 1),
            "water_pressure": round(sum(r.water_pressure for r in reviews) / n, 1),
            "deposit_fairness": round(sum(r.deposit_fairness for r in reviews) / n, 1),
            "agency_comm": round(sum(r.agency_communication for r in reviews) / n, 1),
        }

    return RoomMatchResponse(
        matched=True,
        match_method=match_method,
        hamming_distance=hamming_dist,
        room_id=room_id,
        canonical_label=room_meta["canonical_label"],
        street_hint=room_meta["street_hint"],
        full_address=room_meta.get("full_address"),
        postcode=room_meta.get("postcode") or room_meta["outcode"],
        outcode=room_meta["outcode"],
        latitude=room_meta.get("latitude"),
        longitude=room_meta.get("longitude"),
        times_listed_18m=len(snapshots),
        earliest_rent_pcm=earliest_rent,
        rent_change_pct=rent_change_pct,
        avg_scores=avg_scores,
        reviews=reviews,
    )


# =====================================================================
# 5. SUBMIT TENANT REVIEW ENDPOINT (Creates Room Anchor + Hashes Photos)
# =====================================================================

@app.post("/api/v1/reviews/submit", response_model=ReviewSubmitResponse)
async def submit_room_review(payload: ReviewSubmitRequest):
    fingerprint_db = RoomFingerprintDB(DB_PATH)
    conn = fingerprint_db.conn
    clean_ad_id = normalize_ad_id(payload.external_ad_id)
    outcode = payload.outcode.strip().upper() or "EX4"
    street_hint = payload.street_hint.strip() or "Shared House"
    canonical_label = f"{payload.floor_level.strip()} {payload.room_aspect.strip()} Room"
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    # Geocode exact building/street coordinates and full postcode
    postcode = geocoder.normalize_postcode(payload.postcode) or outcode
    full_address = payload.full_address or (f"{street_hint}, {postcode}" if street_hint != "Shared House" else postcode)
    geo = geocoder.geocode_location(postcode=postcode, street_hint=street_hint, full_address=full_address)

    # Check if this ad already maps to a physical room
    row = conn.execute(
        """
        SELECT room_id FROM listing_snapshots
        WHERE portal = ? AND external_ad_id IN (?, ?)
        LIMIT 1
        """,
        (payload.portal, clean_ad_id, payload.external_ad_id.strip()),
    ).fetchone()

    room_id: Optional[int] = row["room_id"] if row else None

    # Download and hash gallery photos so future relisted adverts for this room automatically match
    computed_hashes = []
    if payload.image_urls:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for img_url in payload.image_urls[:5]:
                try:
                    resp = await client.get(img_url)
                    if resp.status_code == 200:
                        p_hex, p_int, d_hex, d_int = compute_image_fingerprints(resp.content)
                        computed_hashes.append((img_url, p_hex, p_int, d_hex, d_int))
                        if room_id is None:
                            hit = fingerprint_db.find_matching_room_by_hash(p_int, d_int, outcode)
                            if hit:
                                room_id = hit["room_id"]
                except Exception:
                    continue

    embargo_date = (
        (now_dt + timedelta(days=30)).strftime("%Y-%m-%d")
        if payload.hold_for_deposit_30d
        else now_dt.strftime("%Y-%m-%d")
    )

    with conn:
        if room_id is None:
            cur = conn.execute(
                """
                INSERT INTO physical_rooms (outcode, street_hint, full_address, postcode, latitude, longitude, canonical_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (outcode, street_hint, geo["full_address"], geo["postcode"], geo["latitude"], geo["longitude"], canonical_label, now_iso),
            )
            room_id = cur.lastrowid
        else:
            conn.execute(
                """
                UPDATE physical_rooms
                SET street_hint = CASE WHEN street_hint IS NULL OR street_hint = 'Unknown Street' THEN ? ELSE street_hint END,
                    full_address = COALESCE(full_address, ?),
                    postcode = COALESCE(postcode, ?),
                    latitude = COALESCE(latitude, ?),
                    longitude = COALESCE(longitude, ?)
                WHERE room_id = ?
                """,
                (street_hint, geo["full_address"], geo["postcode"], geo["latitude"], geo["longitude"], room_id),
            )
            existing_meta = conn.execute(
                "SELECT canonical_label FROM physical_rooms WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            if existing_meta:
                canonical_label = existing_meta["canonical_label"]

        # Ensure a listing snapshot exists for this ad ID
        snap_row = conn.execute(
            """
            SELECT snapshot_id FROM listing_snapshots
            WHERE portal = ? AND external_ad_id = ?
            LIMIT 1
            """,
            (payload.portal, clean_ad_id),
        ).fetchone()

        if snap_row:
            snapshot_id = snap_row["snapshot_id"]
        else:
            snap_cur = conn.execute(
                """
                INSERT INTO listing_snapshots
                (room_id, portal, external_ad_id, url, rent_pcm, bills_included, advertiser_uid, scraped_at)
                VALUES (?, ?, ?, ?, ?, 1, 'tenant_submitted', ?)
                """,
                (
                    room_id,
                    payload.portal,
                    clean_ad_id,
                    f"https://www.spareroom.co.uk/flatshare/flatshare_detail.pl?flatshare_id={clean_ad_id}",
                    payload.rent_paid_pcm,
                    now_iso,
                ),
            )
            snapshot_id = snap_cur.lastrowid

            for img_url, p_hex, p_int, d_hex, d_int in computed_hashes:
                conn.execute(
                    """
                    INSERT INTO image_fingerprints
                    (room_id, snapshot_id, image_url, caption, is_communal, phash_hex, phash_int64, dhash_hex, dhash_int64)
                    VALUES (?, ?, ?, '', 0, ?, ?, ?, ?)
                    """,
                    (room_id, snapshot_id, img_url, p_hex, p_int, d_hex, d_int),
                )

        rev_cur = conn.execute(
            """
            INSERT INTO room_reviews
            (room_id, tenancy_period, landlord_or_agency, noise_isolation, winter_warmth_damp, water_pressure, deposit_fairness, agency_communication, rent_paid_pcm, comment, verified, embargo_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                room_id,
                payload.tenancy_period.strip(),
                payload.landlord_or_agency.strip(),
                payload.noise_isolation,
                payload.winter_warmth_damp,
                payload.water_pressure,
                payload.deposit_fairness,
                payload.agency_communication,
                payload.rent_paid_pcm,
                payload.comment.strip(),
                embargo_date,
            ),
        )
        review_id = rev_cur.lastrowid

    conn.close()
    return ReviewSubmitResponse(
        ok=True,
        room_id=room_id,
        review_id=review_id,
        embargoed=payload.hold_for_deposit_30d,
        embargo_until=embargo_date,
        canonical_label=canonical_label,
    )
