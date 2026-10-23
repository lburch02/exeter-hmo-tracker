"""
RoomRadar National & Regional Agency Review Scraper
Crawls real tenant reviews from allAgents.co.uk for:
1. National Student HMO Providers (Student Cribs, UniHomes)
2. Exeter Agencies (Star Lettings / Rosewood, Cardens Residential, Exeter Student Lettings)
3. Regional UK Student Hubs (Bristol, Manchester, Leeds, Birmingham)

Saves verified reviews into Neon PostgreSQL / SQLite agency_reviews table.
"""

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

import db_layer

# National and Regional UK Letting Agencies
TARGET_AGENCIES = [
    # National
    {
        "name": "Student Cribs",
        "city": "National",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/student-cribs/",
    },
    # Exeter
    {
        "name": "Star Lettings & Property Management",
        "city": "Exeter",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/star-lettings-property-management/",
    },
    {
        "name": "Cardens Residential",
        "city": "Exeter",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/cardens-residential/",
    },
    # Bristol
    {
        "name": "Bristol SU Lettings",
        "city": "Bristol",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/bristol-su-lettings/",
    },
    # Leeds
    {
        "name": "Parklane Properties",
        "city": "Leeds",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/parklane-properties/",
    },
    # Birmingham
    {
        "name": "Oakmans Estate Agents",
        "city": "Birmingham",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/oakmans-estate-agents/",
    },
    # Manchester
    {
        "name": "Philip James Manchester",
        "city": "Manchester",
        "source": "allAgents.co.uk",
        "url": "https://www.allagents.co.uk/philip-james-manchester/",
    },
]


def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    # Clean HTML entities & double spaces
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&rsquo;", "'").replace("&ldquo;", '"').replace("&rdquo;", '"').replace("&ndash;", "-")
    return re.sub(r"\s+", " ", s).strip()


def extract_allagents_reviews(html: str, agency_info: dict) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    reviews: List[dict] = []
    seen_texts = set()

    cards = soup.select("[class*='viewProfile_para']")

    for card in cards:
        meta_text = card.get_text(" ", strip=True)
        if "Services :" not in meta_text and "Rent PCM :" not in meta_text and "Would you recommend" not in meta_text:
            continue

        # 1. Reviewer Name
        m_by = re.search(r"By\s*:\s*['\"]?\s*([^'\"]+?)\s*['\"]?(?:\s+Branch|\s+Services|\s+Rent|$)", meta_text)
        reviewer_name = clean_text(m_by.group(1)) if m_by else "Tenant Reviewer"
        if len(reviewer_name) > 30:
            reviewer_name = reviewer_name[:30].strip()

        # 2. Date
        m_date = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b", meta_text)
        review_date = m_date.group(1) if m_date else datetime.now(timezone.utc).strftime("%d %b %Y")

        # 3. Rent & Postcode
        m_rent = re.search(r"Rent PCM\s*:\s*[£\?]?\s*([\d,]+)", meta_text)
        rent_paid = int(m_rent.group(1).replace(",", "")) if m_rent else None

        m_pc = re.search(r"Postcode\s*:\s*([A-Z0-9\s]{3,8})", meta_text)
        postcode = clean_text(m_pc.group(1)) if m_pc else None

        # 4. Recommendation & Star Rating
        m_rec = re.search(r"Would you recommend\s*\?\s*:\s*([A-Za-z]+)", meta_text)
        recommend = m_rec.group(1).capitalize() if m_rec else "Yes"

        star_rating = 5 if recommend == "Yes" else 1

        # 5. Extract Comment Body
        parent = card.parent
        full_text = parent.get_text(" ", strip=True) if parent else ""
        comment = full_text.replace(meta_text, "").strip()

        # Strip AllAgents navigation and actions boilerplate
        comment = re.sub(r"Comments on Agent Fees.*$", "", comment, flags=re.I).strip()
        comment = re.sub(r"Read Full Review.*$", "", comment, flags=re.I).strip()
        comment = re.sub(r"Report abuse.*$", "", comment, flags=re.I).strip()
        comment = clean_text(comment)

        if comment and len(comment) > 20:
            text_fingerprint = comment[:60].lower()
            if text_fingerprint not in seen_texts:
                seen_texts.add(text_fingerprint)
                reviews.append({
                    "agency_name": agency_info["name"],
                    "city": agency_info["city"],
                    "source": agency_info["source"],
                    "reviewer_name": reviewer_name,
                    "review_date": review_date,
                    "star_rating": star_rating,
                    "recommend": recommend,
                    "rent_paid_pcm": rent_paid,
                    "postcode": postcode,
                    "comment": comment,
                    "source_url": agency_info["url"],
                })

    return reviews


def save_reviews_to_db(reviews: List[dict]) -> int:
    """Inserts scraped agency reviews into database, avoiding duplicates."""
    conn = db_layer.get_connection()
    is_pg = db_layer.is_postgres()
    saved = 0

    for r in reviews:
        # Check if identical review already stored
        check_q = "SELECT agency_review_id FROM agency_reviews WHERE agency_name = ? AND reviewer_name = ? AND substr(comment, 1, 40) = ? LIMIT 1"
        res = db_layer.execute_query(conn, check_q, (r["agency_name"], r["reviewer_name"], r["comment"][:40]))
        existing = db_layer.fetch_one(res)
        if existing:
            continue

        insert_q = """
            INSERT INTO agency_reviews (agency_name, city, source, reviewer_name, review_date, star_rating, recommend, rent_paid_pcm, postcode, comment, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        db_layer.execute_query(
            conn,
            insert_q,
            (
                r["agency_name"],
                r["city"],
                r["source"],
                r["reviewer_name"],
                r["review_date"],
                r["star_rating"],
                r["recommend"],
                r["rent_paid_pcm"],
                r["postcode"],
                r["comment"],
                r["source_url"],
            ),
        )
        saved += 1

    if not is_pg:
        conn.commit()
    conn.close()
    return saved


async def run_national_agency_crawl() -> int:
    """Crawls all target national and regional student letting agencies."""
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting RoomRadar National Agency Review Crawler...")
    total_saved = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for agency in TARGET_AGENCIES:
            print(f"\n[CRAWLING] {agency['name']} ({agency['city']}) -> {agency['url']}")
            try:
                await page.goto(agency["url"], timeout=25000, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)  # Let content settle
                html = await page.content()
                reviews = extract_allagents_reviews(html, agency)
                print(f"  Extracted {len(reviews)} reviews for {agency['name']}.")
                new_saved = save_reviews_to_db(reviews)
                print(f"  Saved {new_saved} new reviews into database.")
                total_saved += new_saved
            except Exception as e:
                print(f"  [ERROR] Failed scraping {agency['name']}: {e}")

        await browser.close()

    print(f"\n[{datetime.now(timezone.utc).isoformat()}] National crawler complete. Total new agency reviews saved: {total_saved}")
    return total_saved


if __name__ == "__main__":
    asyncio.run(run_national_agency_crawl())
