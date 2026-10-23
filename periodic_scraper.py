"""
Periodic Exeter Accommodation Scraper
Runs on a scheduled basis (GitHub Actions Cron / Render Background Worker)
Crawls active Exeter HMO listings across:
1. SpareRoom Exeter (/flatshare/exeter and /flatshare/devon/exeter)
2. Exeter Student Pads (exeterstudentpads.co.uk/properties/)
3. Exeter Studentpad (exeterstudentpad.co.uk/Advert/)

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

from exeter_hmo_matcher import (
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

DB_PATH = os.environ.get("DB_PATH", "exeter_hmo_tracker.db")


async def discover_spareroom_exeter_listings(client: httpx.AsyncClient) -> List[Tuple[str, str]]:
    """Discovers active SpareRoom listings in Exeter."""
    discovered: List[Tuple[str, str]] = []
    seen = set()
    urls = [
        "https://www.spareroom.co.uk/flatshare/exeter",
        "https://www.spareroom.co.uk/flatshare/devon/exeter",
    ]
    for search_url in urls:
        try:
            resp = await client.get(search_url, timeout=12.0)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
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
        except Exception as e:
            print(f"[WARN] Failed fetching SpareRoom {search_url}: {e}")
    return discovered


async def discover_studentpads_exeter_listings(client: httpx.AsyncClient) -> List[Tuple[str, str]]:
    """Discovers active Exeter Student Pads listings."""
    discovered: List[Tuple[str, str]] = []
    seen = set()
    try:
        resp = await client.get("https://exeterstudentpads.co.uk/properties/", timeout=12.0)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith("https://exeterstudentpads.co.uk/") and not href.endswith(
                    ("/properties/", "/contact/", ".co.uk/", "/faq/", "?page_id=372")
                ):
                    slug = href.rstrip("/").split("/")[-1]
                    # Filter out non-property pages
                    if slug and slug not in seen and not slug.startswith("?") and slug not in ("faq", "contact", "properties"):
                        seen.add(slug)
                        discovered.append((slug, href))
    except Exception as e:
        print(f"[WARN] Failed fetching Exeter Student Pads: {e}")
    return discovered


async def run_periodic_scrape(db_path: str = DB_PATH) -> int:
    """Executes a full periodic scraping run across Exeter portals."""
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Exeter HMO periodic crawler...")
    db = RoomFingerprintDB(db_path)
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scraper = ExeterHMOScraper()

    total_ingested = 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # 1. Discover SpareRoom Exeter ads
        spareroom_listings = await discover_spareroom_exeter_listings(client)
        print(f"[DISCOVERY] Found {len(spareroom_listings)} active SpareRoom Exeter adverts.")

        # 2. Discover Exeter Student Pads
        studentpad_listings = await discover_studentpads_exeter_listings(client)
        print(f"[DISCOVERY] Found {len(studentpad_listings)} active Exeter Student Pads adverts.")

        all_listings = spareroom_listings + studentpad_listings

        for ad_id, ad_url in all_listings:
            clean_id = normalize_ad_id(ad_id)
            # Skip if already scraped today
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
