"""
RoomRadar High-Precision UK Geocoder
Resolves exact building/street latitude, longitude, full address, and postcode using:
1. postcodes.io (Free, official UK Ordnance Survey coordinates for any full UK postcode)
2. OpenStreetMap Nominatim (Exact street + house number address geocoding)
3. postcodes.io /outcodes (Outcode area centroid fallback)
4. Built-in high-traffic student HMO street registry fallback
"""

import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import httpx

UK_FULL_POSTCODE_REGEX = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.IGNORECASE,
)

UK_OUTCODE_REGEX = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\b",
    re.IGNORECASE,
)

# In-memory geocode cache: query_key -> result dict
_GEOCODE_CACHE: Dict[str, Dict[str, Any]] = {}

# Pre-cached coordinates for known Exeter and UK student streets for instant 0ms fallback
KNOWN_STREET_FALLBACKS: Dict[str, Tuple[float, float, str, str]] = {
    # Exeter HMO streets: (lat, lng, default_postcode, zone)
    "monks road": (50.7318, -3.5098, "EX4 7AX", "Mount Pleasant / Polsloe"),
    "pennsylvania road": (50.7352, -3.5248, "EX4 6DS", "St James & Pennsylvania"),
    "victoria street": (50.7336, -3.5212, "EX4 6JA", "St James"),
    "springfield road": (50.7341, -3.5190, "EX4 6HF", "St James"),
    "old tiverton road": (50.7329, -3.5162, "EX4 6LG", "St James / Sidwell"),
    "culverland road": (50.7320, -3.5225, "EX4 6JJ", "St James"),
    "pinhoe road": (50.7340, -3.5040, "EX4 7HU", "Polsloe"),
    "manston road": (50.7258, -3.5032, "EX4 7DD", "Mount Pleasant / Heavitree"),
    "fore street": (50.7195, -3.5015, "EX2 4EP", "Heavitree"),
    "magdalen road": (50.7202, -3.5195, "EX2 4TA", "St Leonard's"),
    "howell road": (50.7302, -3.5328, "EX4 4LZ", "St David's"),
    "blackall road": (50.7314, -3.5290, "EX4 4HD", "St David's"),
    "sidwell street": (50.7278, -3.5220, "EX4 6PH", "City Centre"),
    # Scalable UK expansion anchors
    "whiteladies road": (51.4642, -2.6083, "BS8 2PB", "Bristol — Clifton"),
    "gloucester road": (51.4789, -2.5894, "BS7 8AA", "Bristol — Bishopston"),
    "wilmslow road": (53.4475, -2.2190, "M14 6XX", "Manchester — Fallowfield"),
    "oxford road": (53.4660, -2.2330, "M13 9PL", "Manchester — Student Corridor"),
    "otley road": (53.8202, -1.5794, "LS6 3PX", "Leeds — Headingley"),
    "cardigan road": (53.8122, -1.5732, "LS6 1LF", "Leeds — Hyde Park"),
    "bristol road": (52.4430, -1.9360, "B29 6ND", "Birmingham — Selly Oak"),
}


def normalize_postcode(raw: Optional[str]) -> Optional[str]:
    """Standardizes UK postcodes to uppercase with a single middle space (e.g. 'EX4 6QD')."""
    if not raw:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if len(cleaned) < 5 or len(cleaned) > 8:
        return raw.strip().upper() if raw else None
    # Inward code is always last 3 characters (e.g. 6QD)
    outward = cleaned[:-3]
    inward = cleaned[-3:]
    return f"{outward} {inward}"


def extract_full_postcode(text: str) -> Optional[str]:
    """Extracts first valid full UK postcode from free-form text."""
    if not text:
        return None
    match = UK_FULL_POSTCODE_REGEX.search(text)
    if match:
        return normalize_postcode(match.group(1))
    return None


def extract_outcode(text: str) -> str:
    """Extracts UK outcode (e.g. EX4, BS8, M14) from text or full postcode."""
    if not text:
        return "EX4"
    norm = normalize_postcode(text)
    if norm and " " in norm:
        return norm.split(" ")[0]
    m = UK_OUTCODE_REGEX.search(text)
    return m.group(1).upper() if m else "EX4"


OUTCODE_CITY_MAP: Dict[str, str] = {
    "EX": "Exeter",
    "BS": "Bristol",
    "M": "Manchester",
    "B": "Birmingham",
    "LS": "Leeds",
    "S": "Sheffield",
    "L": "Liverpool",
    "NG": "Nottingham",
    "CB": "Cambridge",
    "OX": "Oxford",
    "NE": "Newcastle",
    "CF": "Cardiff",
    "EH": "Edinburgh",
    "G": "Glasgow",
    "SO": "Southampton",
    "BN": "Brighton",
    "NR": "Norwich",
    "CO": "Colchester",
    "CV": "Coventry",
}

OUTCODE_SPECIAL_CITIES: Dict[str, str] = {
    "EX7": "Dawlish",
    "EX31": "Barnstaple",
    "EX32": "Barnstaple",
    "EX39": "Bideford",
    "EX15": "Cullompton",
    "EX5": "Clyst St Mary",
    "BS32": "Bradley Stoke",
    "BS34": "Filton",
    "BS37": "Yate",
    "BS15": "Hanham",
    "BS16": "Fishponds",
    "BS10": "Henbury",
    "BS11": "Shirehampton",
    "M30": "Eccles",
    "M27": "Swinton",
    "M25": "Prestwich",
    "M21": "Chorlton",
    "M20": "Didsbury",
    "M14": "Fallowfield",
    "M13": "Victoria Park",
    "M6": "Salford",
    "M5": "Salford",
    "M3": "Salford / Manchester",
    "M4": "Ancoats",
    "M1": "Manchester City Centre",
}


def guess_city_from_outcode(outcode: str) -> str:
    oc = (outcode or "").strip().upper()
    if oc in OUTCODE_SPECIAL_CITIES:
        return OUTCODE_SPECIAL_CITIES[oc]
    prefix2 = re.sub(r"\d.*", "", oc)
    if prefix2 in OUTCODE_CITY_MAP:
        return OUTCODE_CITY_MAP[prefix2]
    return "UK"


def geocode_location(
    postcode: Optional[str] = None,
    street_hint: Optional[str] = None,
    full_address: Optional[str] = None,
    city: Optional[str] = None,
    area_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Synchronously geocodes a location down to exact coordinates, resolving:
    1. Full UK postcode via postcodes.io
    2. Street address via OpenStreetMap Nominatim
    3. Outcode centroid via postcodes.io
    4. Built-in street registry fallback
    """
    clean_pc = normalize_postcode(postcode)
    clean_street = (street_hint or "").strip()
    if clean_street.lower() in ("unknown street", "unknown", "shared house", "none"):
        clean_street = ""
    clean_addr = (full_address or "").strip()
    outcode = extract_outcode(clean_pc or clean_street or "EX4")

    detected_city = city or guess_city_from_outcode(outcode)

    cache_key = f"{postcode or ''}|{clean_street}|{clean_addr}|{detected_city}|{area_hint or ''}".strip().lower()
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]

    # 1. Try Full Postcode via postcodes.io
    if clean_pc and " " in clean_pc:
        try:
            resp = httpx.get(
                f"https://api.postcodes.io/postcodes/{urllib.parse.quote(clean_pc)}",
                timeout=4.0,
            )
            if resp.status_code == 200:
                res = resp.json().get("result", {})
                lat = float(res.get("latitude"))
                lng = float(res.get("longitude"))
                ward = res.get("admin_ward") or res.get("parish") or area_hint or f"{outcode} Area"
                district = res.get("admin_district") or detected_city
                if isinstance(district, list):
                    district = district[0] if district else detected_city

                if clean_addr:
                    addr = clean_addr
                elif clean_street:
                    addr = f"{clean_street}, {district}, {clean_pc}"
                else:
                    addr = f"{ward}, {district}, {clean_pc}"

                data = {
                    "latitude": round(lat, 6),
                    "longitude": round(lng, 6),
                    "postcode": clean_pc,
                    "outcode": outcode,
                    "full_address": addr,
                    "zone": f"{district} — {ward}",
                    "method": "POSTCODE_EXACT",
                }
                _GEOCODE_CACHE[cache_key] = data
                return data
        except Exception:
            pass

    # 2. Try Exact Address via OpenStreetMap Nominatim
    search_query = clean_addr
    if not search_query and clean_street:
        search_query = f"{clean_street}, {detected_city}, UK"

    if search_query:
        try:
            headers = {"User-Agent": "RoomRadar-Geocoder/1.0 (contact@roomradar.app)"}
            resp = httpx.get(
                f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(search_query)}&format=json&limit=1",
                headers=headers,
                timeout=4.0,
            )
            if resp.status_code == 200 and resp.json():
                hit = resp.json()[0]
                lat = float(hit.get("lat"))
                lng = float(hit.get("lon"))
                display_name = hit.get("display_name", "")
                found_pc = clean_pc or extract_full_postcode(display_name)
                found_outcode = extract_outcode(found_pc or outcode)
                resolved_addr = clean_addr or (
                    f"{clean_street}, {detected_city}, {found_pc}" if found_pc else f"{clean_street}, {detected_city}"
                )
                data = {
                    "latitude": round(lat, 6),
                    "longitude": round(lng, 6),
                    "postcode": found_pc or found_outcode,
                    "outcode": found_outcode,
                    "full_address": resolved_addr,
                    "zone": f"{detected_city} — {clean_street or 'Area'}",
                    "method": "NOMINATIM_ADDRESS",
                }
                _GEOCODE_CACHE[cache_key] = data
                return data
        except Exception:
            pass

    # 3. Try Known Street Fallback Registry (City-matched)
    street_key = clean_street.lower()
    if street_key in KNOWN_STREET_FALLBACKS:
        lat, lng, default_pc, zone = KNOWN_STREET_FALLBACKS[street_key]
        fallback_outcode = extract_outcode(default_pc)
        # Only use registry if outcode prefix is compatible
        if outcode[:2] == fallback_outcode[:2]:
            resolved_pc = clean_pc or default_pc
            data = {
                "latitude": round(lat, 6),
                "longitude": round(lng, 6),
                "postcode": resolved_pc,
                "outcode": extract_outcode(resolved_pc),
                "full_address": clean_addr or f"{clean_street}, {detected_city}, {resolved_pc}",
                "zone": f"{detected_city} — {zone}",
                "method": "STREET_REGISTRY",
            }
            _GEOCODE_CACHE[cache_key] = data
            return data

    # 4. Try Outcode Centroid via postcodes.io
    try:
        resp = httpx.get(f"https://api.postcodes.io/outcodes/{outcode}", timeout=3.0)
        if resp.status_code == 200:
            res = resp.json().get("result", {})
            lat = float(res.get("latitude"))
            lng = float(res.get("longitude"))
            districts = res.get("admin_district", [])
            district = districts[0] if districts else detected_city
            if "Bristol" in district:
                district = "Bristol"
            wards = res.get("admin_ward", [])
            ward = area_hint or (wards[0] if wards else f"{outcode} Area")

            if clean_addr:
                addr = clean_addr
            elif clean_street:
                addr = f"{clean_street}, {ward}, {district} {outcode}" if ward != clean_street else f"{clean_street}, {district} {outcode}"
            else:
                addr = f"{ward}, {district} {outcode}"

            data = {
                "latitude": round(lat, 6),
                "longitude": round(lng, 6),
                "postcode": clean_pc or outcode,
                "outcode": outcode,
                "full_address": addr,
                "zone": f"{district} — {ward}",
                "method": "OUTCODE_CENTROID",
            }
            _GEOCODE_CACHE[cache_key] = data
            return data
    except Exception:
        pass

    # 5. Final fallback
    effective_name = clean_street or area_hint or f"{outcode} HMO Corridor"
    data = {
        "latitude": 50.731800,
        "longitude": -3.515000,
        "postcode": clean_pc or outcode,
        "outcode": outcode,
        "full_address": clean_addr or f"{effective_name}, {detected_city} {outcode}",
        "zone": f"{detected_city} — {effective_name}",
        "method": "DEFAULT_FALLBACK",
    }
    _GEOCODE_CACHE[cache_key] = data
    return data
