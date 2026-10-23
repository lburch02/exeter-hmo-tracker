"""
Periodic Accommodation Scraper (RoomRadar)
Runs on a scheduled basis (GitHub Actions Cron / Render Background Worker).
Crawls active HMO listings across UK student/rental markets:
1. SpareRoom (configurable cities: Exeter, Bristol, Manchester, etc.)
2. Regional Student Pads (e.g. exeterstudentpads.co.uk)
3. Studentpad universities (e.g. exeterstudentpad.co.uk)

Hashes room photos with pHash + dHash, filters communal areas,
and records historical rent changes into the database.
"""

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Set, Tuple

import httpx
from bs4 import BeautifulSoup

from roomradar_matcher import (
    RoomRadarScraper,
    ExeterHMOScraper,
    RoomFingerprintDB,
    normalize_ad_id,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}

_default_sqlite = "roomradar.db" if os.path.exists("roomradar.db") else "exeter_hmo_tracker.db"
DB_PATH = os.environ.get("DB_PATH", _default_sqlite)

# Target cities for SpareRoom crawlers (defaults to national student/rental hubs: Exeter, Bristol, Manchester, Birmingham, Leeds, etc.)
TARGET_CITIES = [
    c.strip().lower()
    for c in os.environ.get(
        "TARGET_CITIES", "exeter,bristol,manchester,birmingham,leeds,sheffield,liverpool,nottingham"
    ).split(",")
    if c.strip()
]
MAX_PAGES_PER_CITY = int(os.environ.get("MAX_PAGES_PER_CITY", "5"))


async def discover_spareroom_listings(
    client: httpx.AsyncClient, 
    cities: List[str] = TARGET_CITIES, 
    max_pages: int = MAX_PAGES_PER_CITY
) -> List[Tuple[str, str]]:
    """Discovers active SpareRoom listings across configured target cities with multi-page pagination."""
    discovered: List[Tuple[str, str]] = []
    seen = set()

    for city in cities:
        base_urls = [
            f"https://www.spareroom.co.uk/flatshare/{city}",
        ]
        if city == "exeter":
            base_urls.append("https://www.spareroom.co.uk/flatshare/devon/exeter")

        for base_url in base_urls:
            for page_idx in range(max_pages):
                offset = page_idx * 10
                search_url = f"{base_url}?offset={offset}" if offset > 0 else base_url
                try:
                    resp = await client.get(search_url, timeout=12.0)
                    if resp.status_code != 200:
                        continue
                    soup = BeautifulSoup(resp.text, "html.parser")
                    page_count = 0
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        # Match /flatshare/devon/exeter/18472412 or ?flatshare_id=18472412
                        m = re.search(r"/flatshare/.*?/(\d{7,10})", href) or re.search(r"flatshare_id=(\d{7,10})", href)
                        if m:
                            ad_id = m.group(1)
                            if ad_id not in seen:
                                seen.add(ad_id)
                                full_url = href if href.startswith("http") else f"https://www.spareroom.co.uk{href}"
                                discovered.append((ad_id, full_url))
                                page_count += 1
                    if page_count == 0 and page_idx > 0:
                        break  # Reached end of results for this query
                except Exception as e:
                    print(f"[WARN] Failed fetching SpareRoom {search_url}: {e}")

    return discovered


# Backward-compatible alias
discover_spareroom_exeter_listings = discover_spareroom_listings


async def discover_studentpads_listings(client: httpx.AsyncClient) -> List[Tuple[str, str]]:
    """Discovers active regional Student Pads listings."""
    discovered: List[Tuple[str, str]] = []
    seen = set()
    portals = [
        "https://exeterstudentpads.co.uk/properties/",
    ]
    for portal_url in portals:
        try:
            resp = await client.get(portal_url, timeout=12.0)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if ("studentpads.co.uk/" in href or href.startswith("/")) and not href.endswith(
                        ("/properties/", "/contact/", ".co.uk/", "/faq/", "?page_id=372")
                    ):
                        slug = href.rstrip("/").split("/")[-1]
                        if slug and slug not in seen and not slug.startswith("?") and slug not in ("faq", "contact", "properties"):
                            seen.add(slug)
                            discovered.append((slug, href))
        except Exception as e:
            print(f"[WARN] Failed fetching Student Pads {portal_url}: {e}")
    return discovered


# Backward-compatible alias
discover_studentpads_exeter_listings = discover_studentpads_listings


async def run_periodic_scrape(db_path: str = DB_PATH) -> int:
    """Executes a full periodic scraping run across configured portals."""
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting RoomRadar periodic crawler for cities: {TARGET_CITIES}...")
    db = RoomFingerprintDB(db_path)
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scraper = RoomRadarScraper()

    total_ingested = 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # 1. Discover SpareRoom ads
        spareroom_listings = await discover_spareroom_listings(client)
        print(f"[DISCOVERY] Found {len(spareroom_listings)} active SpareRoom adverts.")

        # 2. Discover Student Pads
        studentpad_listings = await discover_studentpads_listings(client)
        print(f"[DISCOVERY] Found {len(studentpad_listings)} active Student Pads adverts.")

        all_listings = spareroom_listings + studentpad_listings

        for ad_id, ad_url in all_listings:
            clean_id = normalize_ad_id(ad_id)
            # Skip if already scraped today
            if db.is_pg:
                with db.conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM listing_snapshots
                        WHERE external_ad_id = %s AND scraped_at::date = %s::date
                        LIMIT 1
                        """,
                        (clean_id, today_prefix),
                    )
                    already = cur.fetchone()
            else:
                already = db.conn.execute(
                    """
                    SELECT 1 FROM listing_snapshots
                    WHERE external_ad_id = ? AND substr(scraped_at, 1, 10) = ?
                    LIMIT 1
                    """,
                    (clean_id, today_prefix),
                ).fetchone()

            if already:
                print(f"  [SKIP] Advert #{clean_id} already indexed today ({today_prefix}).")
                continue

            try:
                print(f"  [FETCHING] Advert #{clean_id}: {ad_url}")
                page_resp = await client.get(ad_url, timeout=12.0)
                if page_resp.status_code != 200:
                    continue

                listing = scraper.parse_listing_html(page_resp.text, ad_url, clean_id)

                # Fetch images concurrently
                image_payloads: List[Tuple[str, bytes]] = []
                for img_url in listing.image_urls[:6]:
                    try:
                        img_resp = await client.get(img_url, timeout=10.0)
                        if img_resp.status_code == 200:
                            image_payloads.append((img_url, img_resp.content))
                    except Exception:
                        continue

                db.ingest_listing(listing, image_payloads)
                total_ingested += 1
                await asyncio.sleep(1.0)  # Polite crawling delay
            except Exception as exc:
                print(f"  [ERROR] Failed ingesting #{clean_id}: {exc}")

    print(f"[{datetime.now(timezone.utc).isoformat()}] Crawler finished. Ingested {total_ingested} new adverts.\n")
    return total_ingested


if __name__ == "__main__":
    asyncio.run(run_periodic_scrape())
