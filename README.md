# 📡 RoomRadar — Universal HMO Room & Rent Tracking Platform

**RoomRadar** is an open-source, scalable room-level tracking and tenant transparency platform for UK shared housing (HMOs).

Unlike traditional property portals that track entire houses or fleeting adverts, RoomRadar indexes individual bedrooms over time using **64-bit perceptual image hashing** (`pHash` + `dHash`), tracks historical rent hikes across repeated relistings, and crowdsources verified tenant reviews (Noise, Warmth/Mould, Water Pressure, Deposit Fairness, Agency Comms).

Tenants can view reviews, price histories, and landlord ratings directly on property portals via the **RoomRadar Chrome Extension**, or explore cities on the **Interactive Web Dashboard**.

---

## 🌟 Key Features

1. **Cross-Portal Bedroom Recognition (Perceptual Image Hashing):**
   - Computes dual 64-bit perceptual hashes (`pHash` + `dHash`) for every listing photo.
   - Matches redecorated or relisted bedrooms across SpareRoom, Studentpad, OpenRent, UniHomes, and Rightmove within a 10-bit Hamming distance threshold.
   - Filters out communal spaces (kitchens, bathrooms, hallways) using caption heuristics and same-day multi-listing collision quarantine.

2. **Multi-City & National Scalability:**
   - Designed to scale seamlessly across all UK student and rental markets (e.g. Exeter, Bristol, Manchester, Leeds, Birmingham, London).
   - Generalized UK outcode parser (`\b[A-Z]{1,2}\d[A-Z\d]?\b`).
   - Configurable scraping targets via `TARGET_CITIES="exeter,bristol,manchester"`.

3. **Cloud Neon PostgreSQL + SQLite Fallback:**
   - Production cloud database hosted on Neon PostgreSQL with native 64-bit bitwise Hamming distance operators:
     ```sql
     bit_count((phash_int64 # query_hash)::bit(64))
     ```
   - Zero-configuration local development fallback to SQLite with custom C-speed Python bitwise UDFs.

4. **Automated Scheduled Scraping:**
   - Nightly GitHub Actions crawler (`.github/workflows/scraper.yml`) crawls active listings, hashes bedroom photos, and updates price trends automatically.

5. **Browser Extension (Manifest V3):**
   - Clean Shadow DOM injection overlay on SpareRoom, Studentpad (`studentpad.co.uk`), OpenRent, UniHomes, and Rightmove.
   - Shows historical rent hikes, tenant ratings, and an embedded review submission drawer.

---

## 🏗️ Architecture

```
                       ┌───────────────────────────────┐
                       │   RoomRadar Chrome Extension  │
                       │   (SpareRoom, Studentpad...)  │
                       └───────────────┬───────────────┘
                                       │ Lookup & Submit
                                       ▼
┌──────────────────────┐      ┌─────────────────────────┐
│ GitHub Actions Cron  ├─────►│  FastAPI Cloud Service  │
│  (periodic_scraper)  │      │  (Render / Docker / Uvic)│
└──────────────────────┘      └────────────┬────────────┘
                                           │
                                           ▼
                             ┌───────────────────────────┐
                             │    Neon Cloud Postgres    │
                             │   (or SQLite Local DB)    │
                             └───────────────────────────┘
```

---

## 🚀 Quick Start (Local Development)

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/lburch02/exeter-hmo-tracker.git roomradar
cd roomradar
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure Environment (`.env`)

```ini
DATABASE_URL=postgresql://neondb_owner:***@ep-red-hill-za4jveku-pooler.c-2.eu-west-2.aws.neon.tech/neondb?sslmode=require
TARGET_CITIES=exeter
PORT=8000
```
*(If `DATABASE_URL` is omitted, RoomRadar automatically falls back to local `roomradar.db`)*

### 3. Run the Web Dashboard & API

```bash
uvicorn api:app --reload --port 8000
```
Open [http://localhost:8000](http://localhost:8000) to view the interactive street map, room cards, and agency leaderboard.

### 4. Run the Periodic Scraper

```bash
python periodic_scraper.py
```

---

## 🧩 Installing the Chrome Extension

1. Open Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode** (toggle in top right).
3. Click **Load unpacked**.
4. Select the `roomradar-extension/` directory.
5. Visit any listing on SpareRoom, Studentpad, OpenRent, UniHomes, or Rightmove to see the live RoomRadar card.

---

## 📊 Database Schema

* `physical_rooms`: Canonical physical bedrooms anchored by outcode and street.
* `listing_snapshots`: Historical advert snapshots with rents, dates, portal IDs, and URLs.
* `image_fingerprints`: 64-bit pHash and dHash fingerprints of room photos.
* `room_reviews`: Tenant ratings (1–5 scale), comments, tenancy periods, and 30-day deposit protection embargoes.
