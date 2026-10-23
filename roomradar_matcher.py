import argparse
import asyncio
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import imagehash
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from playwright.async_api import async_playwright

# =====================================================================
# 1. DATA MODELS & BITWISE HASH HELPERS
# =====================================================================

HAMMING_DISTANCE_THRESHOLD = 10  # Max differing bits (out of 64) to consider the same room photo
COMMUNAL_COLLISION_THRESHOLD = 4  # Max differing bits to auto-quarantine shared photos across active ads

COMMUNAL_CAPTION_REGEX = re.compile(
    r"\b(kitchen|bathroom|shower|garden|lounge|living|communal|exterior|hallway|patio|dining|wc|toilet|utility)\b",
    re.IGNORECASE,
)


@dataclass
class ScrapedListing:
    portal: str
    external_ad_id: str
    url: str
    title: str
    outcode: str
    street_hint: str
    rent_pcm: int
    bills_included: bool
    advertiser_uid: str
    image_urls: List[str] = field(default_factory=list)
    image_captions: Dict[str, str] = field(default_factory=dict)
    scraped_at: Optional[str] = None


def normalize_ad_id(ad_id: str) -> str:
    """
    Normalizes external advert IDs (e.g., strips 'SR_', 'SP_', 'OR_', 'UH_', 'RM_'
    prefixes so portal IDs resolve consistently).
    """
    return re.sub(r"^(?:SR_|SP_|OR_|UH_|RM_)", "", ad_id.strip(), flags=re.IGNORECASE)


def is_communal_caption(caption: Optional[str]) -> bool:
    """
    Returns True if a photo caption or alt text indicates a shared/communal space
    (kitchen, bathroom, garden, living room, etc.) rather than a private bedroom.
    """
    if not caption:
        return False
    return bool(COMMUNAL_CAPTION_REGEX.search(caption))


def hex_to_signed_int64(hex_str: str) -> int:
    """
    Converts a 16-char unsigned 64-bit hex string from imagehash into a
    signed 64-bit integer (two's complement) for SQLite/PostgreSQL BIGINT storage.
    """
    val = int(hex_str, 16)
    if val >= (1 << 63):
        val -= (1 << 64)
    return val


def hamming_distance_int64(hash1: int, hash2: int) -> int:
    """
    Computes the bitwise Hamming distance (number of differing bits)
    between two signed 64-bit integers.
    """
    # Mask with 0xFFFFFFFFFFFFFFFF to treat two's complement negatives as unsigned 64-bit
    xor_val = (hash1 ^ hash2) & 0xFFFFFFFFFFFFFFFF
    return xor_val.bit_count()


def compute_image_fingerprints(image_bytes: bytes) -> Tuple[str, int, str, int]:
    """
    Normalizes an image (fixes phone EXIF rotation, strips alpha, crops edge borders
    where SpareRoom/OpenRent/Rightmove burn corner watermarks) and returns
    (phash_hex, phash_int64, dhash_hex, dhash_int64).
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        # Trim 6% off sides/top and 12% off the bottom where agency & portal watermarks sit
        w, h = img.size
        crop_box = (int(w * 0.06), int(h * 0.06), int(w * 0.94), int(h * 0.88))
        img_cropped = img.crop(crop_box)

        p_hash = imagehash.phash(img_cropped, hash_size=8)
        d_hash = imagehash.dhash(img_cropped, hash_size=8)

        phash_hex = str(p_hash)
        dhash_hex = str(d_hash)

        return (
            phash_hex,
            hex_to_signed_int64(phash_hex),
            dhash_hex,
            hex_to_signed_int64(dhash_hex),
        )


# =====================================================================
# 2. DATABASE LAYER (SQLite Prototype with Custom Bitwise UDF)
# =====================================================================

class RoomFingerprintDB:
    def __init__(self, db_path: str = "roomradar.db"):
        # Auto-fallback to legacy SQLite name if it exists and default roomradar.db doesn't yet
        import os
        if db_path == "roomradar.db" and not os.path.exists("roomradar.db") and os.path.exists("exeter_hmo_tracker.db"):
            db_path = "exeter_hmo_tracker.db"
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # Register Python's C-speed bit_count as a custom SQLite function for SQL queries
        self.conn.create_function("HAMMING_DIST", 2, hamming_distance_int64)
        self._init_schema()

    def _init_schema(self):
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS physical_rooms (
                    room_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outcode TEXT NOT NULL,
                    street_hint TEXT,
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

                CREATE INDEX IF NOT EXISTS idx_outcode ON physical_rooms(outcode);
                CREATE INDEX IF NOT EXISTS idx_ad_id ON listing_snapshots(portal, external_ad_id);
                """
            )
            # Safe schema migration for existing DB files created before caption/is_communal columns
            existing_cols = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(image_fingerprints)").fetchall()
            }
            if "caption" not in existing_cols:
                self.conn.execute("ALTER TABLE image_fingerprints ADD COLUMN caption TEXT")
            if "is_communal" not in existing_cols:
                self.conn.execute(
                    "ALTER TABLE image_fingerprints ADD COLUMN is_communal INTEGER NOT NULL DEFAULT 0"
                )

    def find_matching_room_by_hash(
        self, phash_int64: int, dhash_int64: int, outcode: str
    ) -> Optional[sqlite3.Row]:
        """
        Matches a room in the same outcode if EITHER pHash OR dHash is within
        the Hamming threshold and the photo is not flagged as communal,
        ordering by the combined bit distance.
        """
        query = """
            SELECT
                pr.room_id,
                pr.canonical_label,
                pr.street_hint,
                ifp.phash_hex AS matched_phash_hex,
                ls.external_ad_id AS historical_ad_id,
                ls.rent_pcm AS historical_rent_pcm,
                ls.scraped_at AS historical_date,
                HAMMING_DIST(ifp.phash_int64, ?) AS p_dist,
                HAMMING_DIST(ifp.dhash_int64, ?) AS d_dist
            FROM image_fingerprints ifp
            JOIN physical_rooms pr ON pr.room_id = ifp.room_id
            JOIN listing_snapshots ls ON ls.snapshot_id = ifp.snapshot_id
            WHERE pr.outcode = ?
              AND ifp.is_communal = 0
              AND (
                  HAMMING_DIST(ifp.phash_int64, ?) <= ?
                  OR HAMMING_DIST(ifp.dhash_int64, ?) <= ?
              )
            ORDER BY MIN(HAMMING_DIST(ifp.phash_int64, ?), HAMMING_DIST(ifp.dhash_int64, ?)) ASC
            LIMIT 1;
        """
        cur = self.conn.execute(
            query,
            (
                phash_int64,
                dhash_int64,
                outcode,
                phash_int64,
                HAMMING_DISTANCE_THRESHOLD,
                dhash_int64,
                HAMMING_DISTANCE_THRESHOLD,
                phash_int64,
                dhash_int64,
            ),
        )
        return cur.fetchone()

    def check_same_day_communal_collision(
        self,
        phash_int64: int,
        dhash_int64: int,
        outcode: str,
        advertiser_uid: str,
        clean_ad_id: str,
        scraped_date_prefix: str,
    ) -> List[int]:
        """
        Detects if another concurrently active advert (same advertiser_uid, outcode, and date)
        with a DIFFERENT external_ad_id shares this exact photo (Hamming distance <= 4).
        Returns any colliding fingerprint_ids so both can be quarantined as communal (kitchen/living/stock).
        """
        if not advertiser_uid:
            return []
        rows = self.conn.execute(
            """
            SELECT ifp.fingerprint_id
            FROM image_fingerprints ifp
            JOIN listing_snapshots ls ON ls.snapshot_id = ifp.snapshot_id
            JOIN physical_rooms pr ON pr.room_id = ifp.room_id
            WHERE pr.outcode = ?
              AND ls.advertiser_uid = ?
              AND ls.external_ad_id != ?
              AND substr(ls.scraped_at, 1, 10) = ?
              AND (
                  HAMMING_DIST(ifp.phash_int64, ?) <= ?
                  OR HAMMING_DIST(ifp.dhash_int64, ?) <= ?
              )
            """,
            (
                outcode,
                advertiser_uid,
                clean_ad_id,
                scraped_date_prefix,
                phash_int64,
                COMMUNAL_COLLISION_THRESHOLD,
                dhash_int64,
                COMMUNAL_COLLISION_THRESHOLD,
            ),
        ).fetchall()
        return [r["fingerprint_id"] for r in rows]

    def ingest_listing(
        self,
        listing: ScrapedListing,
        image_payloads: List[Tuple[str, bytes]],
    ) -> int:
        """
        Hashes all photos for a scraped listing, filters out communal photos
        (via caption regex or same-day multi-ad collision), checks if any bedroom
        photo matches a known physical room in the database, and either links the
        relisted ad to the historical room OR creates a new PhysicalRoom anchor.
        """
        clean_ad_id = normalize_ad_id(listing.external_ad_id)
        now_iso = listing.scraped_at or datetime.now(timezone.utc).isoformat()
        scraped_date_prefix = now_iso[:10]

        computed_hashes = []
        matched_room = None
        colliding_fp_ids_to_quarantine: List[int] = []

        for img_url, img_bytes in image_payloads:
            p_hex, p_int, d_hex, d_int = compute_image_fingerprints(img_bytes)
            caption = listing.image_captions.get(img_url, "")
            is_comm = is_communal_caption(caption)

            # Rule 2: Same-day multi-ad collision check (two active rooms from same landlord sharing kitchen photo)
            if not is_comm:
                colliding_ids = self.check_same_day_communal_collision(
                    p_int,
                    d_int,
                    listing.outcode,
                    listing.advertiser_uid,
                    clean_ad_id,
                    scraped_date_prefix,
                )
                if colliding_ids:
                    is_comm = True
                    colliding_fp_ids_to_quarantine.extend(colliding_ids)
                    print(
                        f"  [COMMUNAL QUARANTINE] Photo {img_url} on Ad #{clean_ad_id} collided with "
                        f"active ad from '{listing.advertiser_uid}' on {scraped_date_prefix} -> flagged communal."
                    )
            else:
                print(
                    f"  [COMMUNAL CAPTION] Photo {img_url} ('{caption}') flagged communal -> excluded from room match."
                )

            computed_hashes.append((img_url, caption, int(is_comm), p_hex, p_int, d_hex, d_int))

            # Only match physical bedrooms on non-communal photos
            if not is_comm and not matched_room:
                hit = self.find_matching_room_by_hash(p_int, d_int, listing.outcode)
                if hit:
                    matched_room = hit

        with self.conn:
            # Quarantine any previously ingested fingerprints that collided with this same-day ad
            if colliding_fp_ids_to_quarantine:
                placeholders = ",".join("?" for _ in colliding_fp_ids_to_quarantine)
                self.conn.execute(
                    f"UPDATE image_fingerprints SET is_communal = 1 WHERE fingerprint_id IN ({placeholders})",
                    colliding_fp_ids_to_quarantine,
                )

            if matched_room:
                room_id = matched_room["room_id"]
                rent_diff = listing.rent_pcm - matched_room["historical_rent_pcm"]
                print(
                    f"[MATCH FOUND] Ad #{clean_ad_id} matched to Physical Room #{room_id} "
                    f"('{matched_room['canonical_label']}')!\n"
                    f"  -> Matched via Photo Hamming Distance: pHash={matched_room['p_dist']}/64 bits, "
                    f"dHash={matched_room['d_dist']}/64 bits\n"
                    f"  -> Previous Ad ID: #{matched_room['historical_ad_id']} "
                    f"(£{matched_room['historical_rent_pcm']} pcm) | "
                    f"New Rent: £{listing.rent_pcm} pcm ({rent_diff:+d}/mo)"
                )
            else:
                try:
                    import geocoder
                    geo = geocoder.geocode_location(postcode=listing.outcode, street_hint=listing.street_hint)
                    lat, lng = geo["latitude"], geo["longitude"]
                    full_addr, pc = geo["full_address"], geo["postcode"]
                except Exception:
                    lat, lng, full_addr, pc = None, None, f"{listing.street_hint}, {listing.outcode}", listing.outcode

                cur = self.conn.execute(
                    """
                    INSERT INTO physical_rooms (outcode, street_hint, full_address, postcode, latitude, longitude, canonical_label, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing.outcode,
                        listing.street_hint,
                        full_addr,
                        pc,
                        lat,
                        lng,
                        f"{listing.outcode} - {listing.street_hint} ({listing.title[:35]})",
                        now_iso,
                    ),
                )
                room_id = cur.lastrowid
                print(
                    f"[NEW ROOM] Created Physical Room #{room_id} for Ad #{clean_ad_id} "
                    f"({listing.street_hint}, {listing.outcode} @ £{listing.rent_pcm} pcm)"
                )

            # Avoid duplicate snapshot rows if re-running demo on the same persistent DB
            existing_snap = self.conn.execute(
                """
                SELECT snapshot_id FROM listing_snapshots
                WHERE portal = ? AND external_ad_id IN (?, ?)
                LIMIT 1
                """,
                (listing.portal, clean_ad_id, listing.external_ad_id),
            ).fetchone()

            if existing_snap:
                snapshot_id = existing_snap["snapshot_id"]
                self.conn.execute(
                    """
                    UPDATE listing_snapshots
                    SET room_id = ?, external_ad_id = ?, rent_pcm = ?
                    WHERE snapshot_id = ?
                    """,
                    (room_id, clean_ad_id, listing.rent_pcm, snapshot_id),
                )
            else:
                # Record the Listing Snapshot
                snap_cur = self.conn.execute(
                    """
                    INSERT INTO listing_snapshots
                    (room_id, portal, external_ad_id, url, rent_pcm, bills_included, advertiser_uid, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_id,
                        listing.portal,
                        clean_ad_id,
                        listing.url,
                        listing.rent_pcm,
                        int(listing.bills_included),
                        listing.advertiser_uid,
                        now_iso,
                    ),
                )
                snapshot_id = snap_cur.lastrowid

                # Store all Image Fingerprints
                for img_url, caption, is_comm, p_hex, p_int, d_hex, d_int in computed_hashes:
                    self.conn.execute(
                        """
                        INSERT INTO image_fingerprints
                        (room_id, snapshot_id, image_url, caption, is_communal, phash_hex, phash_int64, dhash_hex, dhash_int64)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (room_id, snapshot_id, img_url, caption, is_comm, p_hex, p_int, d_hex, d_int),
                    )

        return room_id


# =====================================================================
# 3. PLAYWRIGHT + BEAUTIFULSOUP SCRAPER (Universal UK Portals)
# =====================================================================

class RoomRadarScraper:
    def __init__(self, rate_limit_sec: float = 2.5):
        self.rate_limit_sec = rate_limit_sec
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        }

    @staticmethod
    def parse_listing_html(html: str, url: str, external_ad_id: str) -> ScrapedListing:
        """
        Uses BeautifulSoup to extract HMO room details, postcode outcode,
        rent price, photo captions, and full-resolution room photo URLs from a rendered page.
        """
        soup = BeautifulSoup(html, "html.parser")

        # 1. Title & Street Hint
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else "Room in Shared House"

        # 2. Extract Outcode (UK Outcode format: e.g. EX4, BS8, M14, LS6, B15, SW19)
        page_text = soup.get_text(" ", strip=True)
        # Search for valid UK outcodes in text
        outcode_match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b", page_text)
        outcode = outcode_match.group(1).upper() if outcode_match else "HMO"

        # Extract HMO street hint if present in description/key-features
        street_match = re.search(
            r"\b([A-Z][a-z]+\s(?:Road|Street|Avenue|Crescent|Lane|Hill|Terrace|Way|Gardens|Grove|Drive))\b",
            page_text,
        )
        street_hint = street_match.group(1) if street_match else "Unknown Street"

        # 3. Extract Monthly Rent (converts Studentpad/UniHomes pppw or pw to pcm if listed weekly)
        rent_pcm = 0
        rent_match_pcm = re.search(
            r"£\s*([\d,]+)(?:\.\d{2})?\s*(?:pcm|per\s*calendar\s*month|per\s*month|\/month|pm)\b",
            page_text,
            re.I,
        )
        rent_match_pw = re.search(
            r"£\s*([\d,]+)(?:\.\d{2})?\s*(?:pppw|pw|per\s*person\s*per\s*week|per\s*week|\/week|p\/w)\b",
            page_text,
            re.I,
        )
        # Check Student Pads format: "Property Price Per Person | £220"
        price_per_person_match = re.search(
            r"(?:price\s*per\s*person|property\s*price)[^£\d]{1,30}£\s*([\d,]+)",
            page_text,
            re.I,
        )
        if rent_match_pcm:
            rent_pcm = int(rent_match_pcm.group(1).replace(",", ""))
        elif rent_match_pw:
            rent_pw = int(rent_match_pw.group(1).replace(",", ""))
            rent_pcm = round((rent_pw * 52) / 12)
        elif price_per_person_match:
            rent_pw = int(price_per_person_match.group(1).replace(",", ""))
            rent_pcm = round((rent_pw * 52) / 12)

        bills_included = bool(re.search(r"bills\s+included\s*:\s*yes|all\s+bills\s+included|bills\s+inclusive", page_text, re.I))

        # Detect portal from URL
        url_lower = url.lower()
        portal = "spareroom"
        if "studentpad" in url_lower:
            portal = "studentpad"
        elif "openrent" in url_lower:
            portal = "openrent"
        elif "unihomes" in url_lower:
            portal = "unihomes"
        elif "rightmove" in url_lower:
            portal = "rightmove"

        # Extract advertiser UID if present in page DOM
        adv_el = soup.select_one("[data-advertiser-id], [data-user-id], [data-landlord-id]")
        advertiser_uid = (
            (adv_el.get("data-advertiser-id") or adv_el.get("data-user-id") or adv_el.get("data-landlord-id"))
            if adv_el
            else f"agent_{portal}_lister"
        )

        # 4. Extract gallery photo URLs and captions across SpareRoom, Studentpad, OpenRent, UniHomes, Rightmove
        image_urls: List[str] = []
        image_captions: Dict[str, str] = {}
        gallery_selectors = (
            "dl.photo-gallery__captions a, div.photo-gallery img, a.photo-gallery__thumbnail, "
            ".property-images img, .sp-gallery img, .carousel-inner img, .fotorama__stage img, "
            "a[data-fancybox] img, a[data-lightbox] img, .photos-container img, .property-photos img, "
            ".gallery-slider img, [data-testid='gallery-main'] img, figure img"
        )
        for el in soup.select(gallery_selectors):
            src = el.get("data-src") or el.get("data-lazy") or el.get("href") or el.get("src")
            if src and src.startswith("http") and "logo" not in src.lower():
                caption = (
                    el.get("title")
                    or el.get("alt")
                    or el.get_text(" ", strip=True)
                    or ""
                )
                if src not in image_urls:
                    image_urls.append(src)
                if caption and not image_captions.get(src):
                    image_captions[src] = caption

        return ScrapedListing(
            portal=portal,
            external_ad_id=normalize_ad_id(external_ad_id),
            url=url,
            title=title,
            outcode=outcode,
            street_hint=street_hint,
            rent_pcm=rent_pcm,
            bills_included=bills_included,
            advertiser_uid=str(advertiser_uid),
            image_urls=image_urls[:8],  # Top 8 photos per listing
            image_captions=image_captions,
        )

    @staticmethod
    def parse_search_results_html(html: str) -> List[Tuple[str, str]]:
        """
        Extracts (external_ad_id, listing_url) pairs from a SpareRoom search index page.
        """
        soup = BeautifulSoup(html, "html.parser")
        results: List[Tuple[str, str]] = []
        seen_ids = set()

        for card in soup.select("article.panel-listing-result, li.listing-result, [data-advert-id]"):
            ad_id = card.get("data-advert-id") or card.get("data-listing-id")
            link_el = card.select_one("a[href*='flatshare_detail.pl'], a[href*='/flatshare/']")
            href = link_el.get("href") if link_el else None

            if not ad_id and href:
                m = re.search(r"flatshare_id=(\d+)|/flatshare/(\d+)", href)
                if m:
                    ad_id = m.group(1) or m.group(2)

            if ad_id:
                clean_id = normalize_ad_id(str(ad_id))
                if clean_id not in seen_ids:
                    seen_ids.add(clean_id)
                    full_url = (
                        href
                        if (href and href.startswith("http"))
                        else f"https://www.spareroom.co.uk/flatshare/flatshare_detail.pl?flatshare_id={clean_id}"
                    )
                    results.append((clean_id, full_url))

        return results

    async def scrape_and_download_listing(self, url: str, external_ad_id: str) -> Tuple[ScrapedListing, List[Tuple[str, bytes]]]:
        """
        Renders the listing via headless Playwright, parses metadata with BeautifulSoup,
        and downloads the room images asynchronously via httpx.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=self.headers["User-Agent"])

            # Block heavy font/analytics/video assets to keep scraping fast and polite
            await context.route(
                "**/*.{woff,woff2,mp4,webm}", lambda route: route.abort()
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(self.rate_limit_sec)

            html = await page.content()
            await browser.close()

        listing = self.parse_listing_html(html, url, external_ad_id)

        # Download images concurrently
        image_payloads: List[Tuple[str, bytes]] = []
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            for img_url in listing.image_urls:
                try:
                    resp = await client.get(img_url)
                    if resp.status_code == 200:
                        image_payloads.append((img_url, resp.content))
                except httpx.RequestError as exc:
                    print(f"  [WARN] Failed to fetch image {img_url}: {exc}")

        return listing, image_payloads

    async def crawl_exeter_search_index(
        self,
        db: RoomFingerprintDB,
        search_url: str = "https://www.spareroom.co.uk/flatshare/?search_id=&location_type=area&where=Exeter",
        max_pages: int = 3,
    ) -> int:
        """
        Paginates through SpareRoom's Exeter search results, skips adverts already
        indexed today, scrapes gallery images + captions, flags communal photos,
        and ingests bedroom fingerprints into the database.
        """
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ingested_count = 0

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=self.headers["User-Agent"])
            await context.route("**/*.{woff,woff2,mp4,webm}", lambda route: route.abort())
            page = await context.new_page()

            for page_idx in range(max_pages):
                offset = page_idx * 10
                paged_url = f"{search_url}&offset={offset}" if "?" in search_url else f"{search_url}?offset={offset}"
                print(f"[CRAWLER] Fetching Exeter search index page {page_idx + 1}/{max_pages}: {paged_url}")

                await page.goto(paged_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self.rate_limit_sec)
                search_html = await page.content()

                discovered = self.parse_search_results_html(search_html)
                if not discovered:
                    print(f"[CRAWLER] No listing cards found on page {page_idx + 1}. Stopping.")
                    break

                for ad_id, ad_url in discovered:
                    # Skip if already scraped today
                    already_today = db.conn.execute(
                        """
                        SELECT 1 FROM listing_snapshots
                        WHERE portal = 'spareroom' AND external_ad_id = ? AND substr(scraped_at, 1, 10) = ?
                        LIMIT 1
                        """,
                        (ad_id, today_prefix),
                    ).fetchone()
                    if already_today:
                        print(f"  [SKIP] Ad #{ad_id} already indexed today ({today_prefix}).")
                        continue

                    try:
                        await page.goto(ad_url, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(self.rate_limit_sec)
                        detail_html = await page.content()
                        listing = self.parse_listing_html(detail_html, ad_url, ad_id)

                        image_payloads: List[Tuple[str, bytes]] = []
                        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
                            for img_url in listing.image_urls:
                                try:
                                    resp = await client.get(img_url)
                                    if resp.status_code == 200:
                                        image_payloads.append((img_url, resp.content))
                                except httpx.RequestError:
                                    continue

                        db.ingest_listing(listing, image_payloads)
                        ingested_count += 1
                    except Exception as exc:
                        print(f"  [WARN] Failed to scrape Ad #{ad_id}: {exc}")

            await browser.close()

        return ingested_count

    async def crawl_search_index(
        self,
        db: RoomFingerprintDB,
        search_url: str,
        max_pages: int = 3,
    ) -> int:
        return await self.crawl_exeter_search_index(db, search_url, max_pages)


# Backward-compatible alias
ExeterHMOScraper = RoomRadarScraper


# =====================================================================
# 4. SELF-CONTAINED TEST HARNESS (Simulates Relisted & Communal Rooms)
# =====================================================================

def _generate_synthetic_hmo_room_photo(
    add_watermark: bool = False,
    resize_dims: Tuple[int, int] = (800, 600),
    brightness_factor: float = 1.0,
    jpeg_quality: int = 90,
    variant: str = "bedroom_front",
) -> bytes:
    """
    Creates a synthetic HMO photo ('bedroom_front', 'bedroom_rear', or 'communal_kitchen')
    to demonstrate how pHash + dHash survive resizing, brightness shifts,
    JPEG compression, agency watermarks, and communal photo quarantine.
    """
    img = Image.new("RGB", (800, 600), color=(235, 230, 220))  # Magnolia HMO walls
    draw = ImageDraw.Draw(img)

    if variant == "communal_kitchen":
        # Shared HMO kitchen (cabinets, worktop, fridge, oven)
        draw.rectangle([40, 80, 760, 220], fill=(200, 205, 210), outline=(70, 70, 70), width=4)
        draw.rectangle([40, 320, 560, 560], fill=(140, 145, 150), outline=(50, 50, 50), width=5)
        draw.rectangle([580, 140, 750, 560], fill=(240, 245, 250), outline=(90, 90, 90), width=5)
        draw.rectangle([220, 340, 380, 540], fill=(35, 35, 40), outline=(20, 20, 20), width=4)
    elif variant == "bedroom_rear":
        # Rear garden-facing small bedroom (window on right, single bed on left)
        draw.rectangle([460, 70, 720, 300], fill=(165, 220, 185), outline=(70, 90, 70), width=6)
        draw.rectangle([60, 280, 360, 540], fill=(135, 75, 90), outline=(45, 30, 35), width=4)
        draw.rectangle([450, 360, 730, 540], fill=(210, 180, 140), outline=(80, 60, 40), width=3)
    else:
        # Default: Ground floor front bay-window room
        # Draw bay window (top-left)
        draw.rectangle([60, 50, 320, 280], fill=(180, 215, 240), outline=(80, 80, 80), width=6)
        draw.line([190, 50, 190, 280], fill=(80, 80, 80), width=5)
        draw.line([60, 165, 320, 165], fill=(80, 80, 80), width=5)

        # Draw double bed & headboard (bottom-right)
        draw.rectangle([380, 260, 740, 540], fill=(60, 90, 135), outline=(40, 40, 40), width=4)
        draw.rectangle([380, 200, 740, 265], fill=(95, 65, 45))  # Wooden headboard

        # Draw student desk & radiator (bottom-left)
        draw.rectangle([70, 310, 310, 390], fill=(245, 245, 245), outline=(190, 190, 190), width=3)
        draw.rectangle([50, 410, 280, 550], fill=(160, 120, 85), outline=(60, 40, 20), width=3)

    if add_watermark:
        # Simulate letting agent adding a banner watermark in the corner
        draw.rectangle([520, 520, 780, 580], fill=(20, 20, 20))
        draw.text((535, 542), "EXETER LETTINGS", fill=(255, 255, 255))

    if brightness_factor != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness_factor)

    if resize_dims != (800, 600):
        img = img.resize(resize_dims, Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()


def run_watermark_and_relist_demo():
    print("--- Starting Exeter HMO Perceptual Hashing Prototype ---\n")
    db = RoomFingerprintDB("exeter_hmo_tracker.db")

    shared_kitchen_photo = _generate_synthetic_hmo_room_photo(variant="communal_kitchen")

    # 1. Original 2024 Listing: Ground Floor Front Room on Monks Road
    original_photo = _generate_synthetic_hmo_room_photo(
        add_watermark=False, resize_dims=(800, 600), brightness_factor=1.0, jpeg_quality=92, variant="bedroom_front"
    )
    ad_2024 = ScrapedListing(
        portal="spareroom",
        external_ad_id="15900112",
        url="https://www.spareroom.co.uk/flatshare/flatshare_detail.pl?flatshare_id=15900112",
        title="Sunny Ground Floor Double in 5-Bed House",
        outcode="EX4",
        street_hint="Monks Road",
        rent_pcm=595,
        bills_included=True,
        advertiser_uid="landlord_9921",
        scraped_at="2024-09-10T12:00:00Z",
    )
    db.ingest_listing(
        ad_2024,
        [
            ("https://cdn.example.com/2024_room1.jpg", original_photo),
            ("https://cdn.example.com/2024_kitchen.jpg", shared_kitchen_photo),
        ],
    )

    print("-" * 72)

    # 2. Same-Day Second Room in the Same House (Room 2 - Rear Box Room):
    # Shares the EXACT same uncaptioned kitchen photo on the same day from the same landlord!
    # Our Same-Day Multi-Ad Quarantine must catch the kitchen collision and NOT merge Room 2 into Room 1.
    rear_room_photo = _generate_synthetic_hmo_room_photo(variant="bedroom_rear")
    ad_2024_room2 = ScrapedListing(
        portal="spareroom",
        external_ad_id="15900115",
        url="https://www.spareroom.co.uk/flatshare/flatshare_detail.pl?flatshare_id=15900115",
        title="Quiet First Floor Rear Room Overlooking Garden",
        outcode="EX4",
        street_hint="Monks Road",
        rent_pcm=530,
        bills_included=True,
        advertiser_uid="landlord_9921",
        scraped_at="2024-09-10T14:30:00Z",
    )
    db.ingest_listing(
        ad_2024_room2,
        [
            ("https://cdn.example.com/2024_room2_kitchen.jpg", shared_kitchen_photo),
            ("https://cdn.example.com/2024_room2_bed.jpg", rear_room_photo),
        ],
    )

    print("-" * 72)

    # 3. Relisted 2026 Listing for Room 1: Landlord deletes old ad, creates a new Ad ID,
    # hikes rent to £685 pcm, resizes the image, boosts brightness by 8%,
    # re-compresses at lower JPEG quality, and stamps a watermark in the corner.
    relisted_photo = _generate_synthetic_hmo_room_photo(
        add_watermark=True, resize_dims=(640, 480), brightness_factor=1.08, jpeg_quality=65, variant="bedroom_front"
    )
    ad_2026 = ScrapedListing(
        portal="spareroom",
        external_ad_id="17488920",  # Brand new Ad ID!
        url="https://www.spareroom.co.uk/flatshare/flatshare_detail.pl?flatshare_id=17488920",
        title="Spacious Double Room Near Streatham & Polsloe",  # Reworded title!
        outcode="EX4",
        street_hint="Monks Road",
        rent_pcm=685,
        bills_included=True,
        advertiser_uid="landlord_9921",
        image_captions={"https://cdn.example.com/2026_kitchen.jpg": "Shared Kitchen & Dining"},
        scraped_at="2026-08-01T12:00:00Z",
    )
    db.ingest_listing(
        ad_2026,
        [
            ("https://cdn.example.com/2026_kitchen.jpg", shared_kitchen_photo),
            ("https://cdn.example.com/2026_new_ad.jpg", relisted_photo),
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exeter HMO Room Matcher & Crawler")
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="Run the live Playwright Exeter SpareRoom search-index crawler instead of the synthetic demo.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="Max search result pages to crawl when --crawl is set (default: 2).",
    )
    args = parser.parse_args()

    if args.crawl:
        scraper = ExeterHMOScraper()
        database = RoomFingerprintDB("exeter_hmo_tracker.db")
        count = asyncio.run(scraper.crawl_exeter_search_index(database, max_pages=args.pages))
        print(f"\n[CRAWLER COMPLETE] Ingested {count} new Exeter HMO room listings.")
    else:
        run_watermark_and_relist_demo()
