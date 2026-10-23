(function () {
  "use strict";

  const COMMUNAL_CAPTION_RE =
    /\b(kitchen|bathroom|shower|garden|lounge|living|communal|exterior|hallway|patio|dining|wc|toilet|utility)\b/i;

  // Normalizes abbreviated Exeter street names ("5 Victoria St" -> "Victoria Street", "Manston Road, Exeter" -> "Manston Road")
  function normalizeStreetName(raw) {
    if (!raw) return "Exeter Shared House";
    let s = raw
      .trim()
      .replace(/,.*$/, "") // Strip ", Exeter" suffix
      .replace(/^\d+[a-zA-Z]?\s+/, ""); // Strip leading house number like "5 Victoria St"
    s = s
      .replace(/\bSt\.?$/i, "Street")
      .replace(/\bRd\.?$/i, "Road")
      .replace(/\bAve\.?$/i, "Avenue")
      .replace(/\bCres\.?$/i, "Crescent")
      .replace(/\bLn\.?$/i, "Lane")
      .replace(/\bPl\.?$/i, "Place")
      .replace(/\bTerr\.?$/i, "Terrace");
    return s;
  }

  // 1. Detect Active Portal & Extract Listing ID (Supports Studentpad /Advert/21135193, SpareRoom, OpenRent, etc.)
  function detectPortalAndAdId() {
    const host = window.location.hostname.toLowerCase();
    const path = window.location.pathname;
    const params = new URLSearchParams(window.location.search);

    let portal = "spareroom";
    if (host.includes("studentpad")) {
      portal = "studentpad";
    } else if (host.includes("openrent")) {
      portal = "openrent";
    } else if (host.includes("unihomes")) {
      portal = "unihomes";
    } else if (host.includes("rightmove")) {
      portal = "rightmove";
    }

    // 1a. Check query parameters
    for (const key of [
      "flatshare_id",
      "propertyId",
      "property_id",
      "advertId",
      "advert_id",
      "id",
    ]) {
      if (params.has(key) && params.get(key)) {
        return { portal, adId: params.get(key).trim() };
      }
    }

    // 1b. Check URL path patterns:
    // - Studentpad: /Advert/21135193 or /Accommodation/Details/...
    // - SpareRoom: /flatshare/17488920
    // - OpenRent: /property-to-rent/.../2145990
    // - Rightmove: /properties/149201122
    const pathPatterns = [
      /\/Advert\/(\d+)/i,
      /\/flatshare\/(\d+)/i,
      /\/(?:Accommodation\/Details|Property|Details)\/([a-zA-Z0-9_-]+)/i,
      /\/properties\/(\d+)/i,
      /property-(\d+)/i,
      /\/(\d{4,12})(?:\/|\?|$)/,
    ];
    for (const regex of pathPatterns) {
      const m = path.match(regex);
      if (m && m[1]) {
        return { portal, adId: m[1] };
      }
    }

    // 1c. Check DOM data attributes
    const adIdEl = document.querySelector(
      "[data-advert-id], [data-property-id], [data-listing-id], [data-id]"
    );
    if (adIdEl) {
      const attrId =
        adIdEl.getAttribute("data-advert-id") ||
        adIdEl.getAttribute("data-property-id") ||
        adIdEl.getAttribute("data-listing-id") ||
        adIdEl.getAttribute("data-id");
      if (attrId && String(attrId).trim()) {
        return { portal, adId: String(attrId).trim() };
      }
    }

    // 1d. Check URL slug (e.g., exeterstudentpads.co.uk/5-victoria-st/ or exeterstudentpad.co.uk/Accommodation)
    const segments = path
      .split("/")
      .map((s) => s.trim())
      .filter(Boolean);
    if (segments.length > 0) {
      const lastSeg = segments[segments.length - 1].toLowerCase();
      if (portal === "studentpad" || !["flatshare", "properties", "contact"].includes(lastSeg)) {
        return { portal, adId: lastSeg };
      }
    }

    if (portal === "studentpad") {
      return { portal, adId: "exeter-studentpad" };
    }

    return { portal, adId: null };
  }

  // 2. Scrape Outcode, Street Hint, Landlord/Agency Name, Monthly Rent, and Bedroom Photos
  function scrapePageMetadata() {
    const bodyText = document.body.innerText || "";

    // Studentpad places the street title inside h2#ctl00_ContentPlaceHolder1_ctl00_Ctrl_AdvertHeader_advertTitleWrapper or .property_details_content_wrapper .title h2
    const studentpadTitleEl =
      document.querySelector(
        "#ctl00_ContentPlaceHolder1_ctl00_Ctrl_AdvertHeader_advertTitleWrapper"
      ) ||
      document.querySelector(".property_details_content_wrapper .title h2");
    const h1Text = (
      studentpadTitleEl?.innerText ||
      document.querySelector("h1")?.innerText ||
      ""
    ).trim();

    // Extract Landlord / Agency contact name if shown on Studentpad or SpareRoom
    const landlordEl =
      document.querySelector(".agent-details-link") ||
      document.querySelector("[id*='AdvertHeader_display_name']");
    const detectedLandlord = landlordEl ? landlordEl.innerText.trim() : "";

    // Find Exeter postcode outcode (default EX4 if not found)
    const outcodeMatch = bodyText.match(/\b(EX[1-4])\b/i);
    const outcode = outcodeMatch ? outcodeMatch[1].toUpperCase() : "EX4";

    // Extract Exeter street name from Studentpad H2 title first (e.g. "Manston Road, Exeter") or body text
    const streetRegex =
      /\b(?:\d+[a-zA-Z]?\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:Road|Rd|Street|St|Avenue|Ave|Crescent|Cres|Lane|Ln|Hill|Terrace|Terr|Place|Pl|Close|Park|Way|Elmside))\b/;
    const h1StreetMatch = h1Text.match(streetRegex);
    const bodyStreetMatch = bodyText.match(streetRegex);

    let streetHint = "Monks Road";
    if (h1StreetMatch) {
      streetHint = normalizeStreetName(h1StreetMatch[1]);
    } else if (studentpadTitleEl && h1Text) {
      streetHint = normalizeStreetName(h1Text);
    } else if (bodyStreetMatch) {
      streetHint = normalizeStreetName(bodyStreetMatch[1]);
    } else if (/elmside/i.test(h1Text) || /elmside/i.test(bodyText)) {
      streetHint = "Elmside";
    }

    // Parse rent:
    // - Studentpad monthly format: "Rent:£625 Month" or "£625 Per person per month"
    // - Studentpad weekly format: "£144.23 PP/Per Week" or "£165 pppw"
    // - SpareRoom/OpenRent format: "£685 pcm" or "£155 pw"
    let currentRentPcm = null;
    const pcmMatch = bodyText.match(
      /£\s*([\d,]+)(?:\.\d{2})?\s*(?:pcm|per\s*person\s*per\s*month|per\s*calendar\s*month|per\s*month|\/month|month|pm)\b/i
    );
    const pwMatch = bodyText.match(
      /£\s*([\d,]+)(?:\.\d{2})?\s*(?:pppw|pp\/per\s*week|pw|per\s*person\s*per\s*week|per\s*week|\/week|p\/w|week)\b/i
    );
    if (pcmMatch) {
      currentRentPcm = parseInt(pcmMatch[1].replace(/,/g, ""), 10);
    } else if (pwMatch) {
      const pw = parseFloat(pwMatch[1].replace(/,/g, ""));
      currentRentPcm = Math.round((pw * 52) / 12);
    }

    // Collect full-size bedroom gallery URLs across Studentpad (img.sp-image data-src), SpareRoom, OpenRent, etc.
    const imageUrls = [];
    const imgSelectors = [
      // Exeter Studentpad (uses img.sp-image with real URL in data-src and blank.gif in src)
      "img.sp-image",
      "img.sp-thumbnail",
      ".property-images img",
      ".sp-gallery img",
      // SpareRoom
      "dl.photo-gallery__captions a",
      "div.photo-gallery img",
      "figure.photo-gallery__main-image img",
      // Exeter Student Pads & others
      ".carousel-inner img",
      ".fotorama__stage img",
      ".elementor-gallery-item",
      ".elementor-widget-image-carousel img",
      ".wp-block-gallery img",
      "a[data-fancybox] img",
      "a[data-lightbox] img",
      ".photos-container img",
      ".property-photos img",
      ".gallery-slider img",
      "[data-testid='gallery-main'] img",
      "figure img",
    ];
    document.querySelectorAll(imgSelectors.join(", ")).forEach((el) => {
      const rawSrc =
        el.getAttribute("data-src") ||
        el.getAttribute("data-lazy") ||
        el.getAttribute("href") ||
        el.getAttribute("src");
      if (!rawSrc) return;

      // Resolve relative URLs if needed
      let src = rawSrc;
      if (rawSrc.startsWith("/")) {
        src = window.location.origin + rawSrc;
      }

      const lower = src.toLowerCase();
      const caption =
        el.getAttribute("title") ||
        el.getAttribute("alt") ||
        el.textContent ||
        "";
      if (
        src.startsWith("http") &&
        !lower.includes("blank.gif") &&
        !lower.includes("logo") &&
        !lower.includes("favicon") &&
        !lower.includes("avatar") &&
        !lower.includes("searchaction") &&
        !COMMUNAL_CAPTION_RE.test(caption) &&
        !imageUrls.includes(src)
      ) {
        imageUrls.push(src);
      }
    });

    return {
      outcode,
      streetHint,
      detectedLandlord,
      currentRentPcm,
      imageUrls: imageUrls.slice(0, 5),
    };
  }

  // 3. Helper to colour-code 1-5 scores
  function scoreBadgeColor(score) {
    if (score >= 4.0) return "#16a34a"; // Green
    if (score >= 2.8) return "#d97706"; // Amber
    return "#dc2626"; // Red warning
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatPortalLabel(portal) {
    const names = {
      spareroom: "SpareRoom",
      studentpad: "Exeter Studentpad",
      openrent: "OpenRent",
      unihomes: "UniHomes",
      rightmove: "Rightmove",
    };
    return names[portal] || portal;
  }

  // 4. Shared Review Submission Drawer HTML + Wiring
  function buildReviewDrawerHtml(meta, apiData, portal) {
    const defaultStreet =
      (apiData && apiData.street_hint) || meta.streetHint || "Monks Road";
    const defaultRent = meta.currentRentPcm || 625;
    const defaultLandlord = meta.detectedLandlord || "";

    return `
      <div class="review-drawer" id="review-drawer" style="display: none;">
        <div class="drawer-title">✍️ Submit a Room-Specific HMO Review (${escapeHtml(formatPortalLabel(portal))})</div>
        <div class="drawer-subtitle">
          Anchor this review to the physical room position + photo fingerprint so future tenants see it across Studentpad, SpareRoom, and OpenRent even if relisted under a new ID.
        </div>
        <form id="hmo-review-form">
          <div class="form-grid-3">
            <label class="field">
              <span>Floor Level</span>
              <select name="floor_level" required>
                <option value="Ground Floor">Ground Floor</option>
                <option value="First Floor" selected>First Floor</option>
                <option value="Second Floor">Second Floor</option>
                <option value="Attic / Loft">Attic / Loft</option>
                <option value="Basement">Basement</option>
              </select>
            </label>
            <label class="field">
              <span>Window Aspect / Position</span>
              <select name="room_aspect" required>
                <option value="Front (Street Side)">Front (Street Side)</option>
                <option value="Rear (Garden View)" selected>Rear (Garden View)</option>
                <option value="Side Return">Side Return</option>
                <option value="Middle / Internal">Middle / Internal</option>
              </select>
            </label>
            <label class="field">
              <span>Street Name (${escapeHtml(meta.outcode)})</span>
              <input type="text" name="street_hint" value="${escapeHtml(defaultStreet)}" required />
            </label>
          </div>

          <div class="form-grid-3">
            <label class="field">
              <span>Tenancy Period</span>
              <input type="text" name="tenancy_period" placeholder="e.g. Sep 2025 – Jul 2026" value="Sep 2025 – Jul 2026" required />
            </label>
            <label class="field">
              <span>Actual Rent Paid (£ pcm)</span>
              <input type="number" name="rent_paid_pcm" min="200" max="2500" value="${defaultRent}" required />
            </label>
            <label class="field">
              <span>Landlord / Agency Name</span>
              <input type="text" name="landlord_or_agency" value="${escapeHtml(defaultLandlord)}" placeholder="e.g. Star Lettings / Private Landlord" />
            </label>
          </div>

          <div class="form-grid-5">
            <label class="field">
              <span>Noise Isolation (1–5)</span>
              <select name="noise_isolation">
                <option value="1">1 - Paper-thin / Road noise</option>
                <option value="2">2 - Poor</option>
                <option value="3" selected>3 - Average</option>
                <option value="4">4 - Quiet</option>
                <option value="5">5 - Soundproof</option>
              </select>
            </label>
            <label class="field">
              <span>Warmth & Mould (1–5)</span>
              <select name="winter_warmth_damp">
                <option value="1">1 - Severe damp / Freezing</option>
                <option value="2">2 - Cold / Condensation</option>
                <option value="3" selected>3 - Average</option>
                <option value="4">4 - Warm & Dry</option>
                <option value="5">5 - Toasty / Zero damp</option>
              </select>
            </label>
            <label class="field">
              <span>Water Pressure (1–5)</span>
              <select name="water_pressure">
                <option value="1">1 - Trickle / Cold</option>
                <option value="2">2 - Drops when tap runs</option>
                <option value="3">3 - Okay</option>
                <option value="4" selected>4 - Good pressure</option>
                <option value="5">5 - Power shower</option>
              </select>
            </label>
            <label class="field">
              <span>Deposit Fairness (1–5)</span>
              <select name="deposit_fairness">
                <option value="1">1 - Unfair deductions</option>
                <option value="2">2 - Contested charges</option>
                <option value="3">3 - Slow return</option>
                <option value="4" selected>4 - Fair return</option>
                <option value="5">5 - Full instant return</option>
              </select>
            </label>
            <label class="field">
              <span>Landlord / Agency Comms (1–5)</span>
              <select name="agency_communication">
                <option value="1">1 - Ghosted / Unannounced entry</option>
                <option value="2">2 - Slow repairs / Poor notice</option>
                <option value="3">3 - Average</option>
                <option value="4" selected>4 - Responsive & polite</option>
                <option value="5">5 - Fast fixes & 24h+ notice</option>
              </select>
            </label>
          </div>

          <label class="field">
            <span>Room-Specific Review (mention mould spots, boiler noise, landlord responsiveness, Wi-Fi, etc.)</span>
            <textarea name="comment" rows="3" placeholder="e.g. First floor rear room overlooking the garden. Quiet at night, but mould appears behind the desk in winter..." required></textarea>
          </label>

          <div class="drawer-footer">
            <label class="embargo-check">
              <input type="checkbox" name="hold_for_deposit_30d" />
              <span>🔒 Embargo publication for 30 days until my TDS/DPS deposit is safely returned</span>
            </label>
            <div class="drawer-actions">
              <button type="button" class="btn-secondary" id="cancel-review-btn">Cancel</button>
              <button type="submit" class="btn" id="save-review-btn">Publish Room Review</button>
            </div>
          </div>
          <div class="submit-status" id="submit-status"></div>
        </form>
      </div>
    `;
  }

  const SHARED_DRAWER_STYLES = `
    .btn {
      background: #0f172a;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 7px 13px;
      font-size: 12px;
      cursor: pointer;
      font-weight: 600;
      text-decoration: none;
      display: inline-block;
    }
    .btn:hover {
      background: #1e293b;
    }
    .btn-outline {
      background: #ffffff;
      color: #0f172a;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 6px 11px;
      font-size: 12px;
      cursor: pointer;
      font-weight: 600;
      text-decoration: none;
      display: inline-block;
    }
    .btn-secondary {
      background: #e2e8f0;
      color: #1e293b;
      border: none;
      border-radius: 6px;
      padding: 7px 12px;
      font-size: 12px;
      cursor: pointer;
      font-weight: 600;
    }
    .review-drawer {
      margin-top: 12px;
      background: #f8fafc;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 14px 16px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #0f172a;
      text-align: left;
    }
    .drawer-title {
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 2px;
    }
    .drawer-subtitle {
      font-size: 12px;
      color: #475569;
      margin-bottom: 12px;
    }
    .form-grid-3 {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin-bottom: 10px;
    }
    .form-grid-5 {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 8px;
      margin-bottom: 10px;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 11px;
      font-weight: 600;
      color: #334155;
      margin-bottom: 8px;
    }
    .field input,
    .field select,
    .field textarea {
      font-family: inherit;
      font-size: 12px;
      padding: 6px 8px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: #fff;
      color: #0f172a;
    }
    .drawer-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 6px;
    }
    .embargo-check {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: #334155;
      cursor: pointer;
    }
    .drawer-actions {
      display: flex;
      gap: 8px;
    }
    .submit-status {
      margin-top: 8px;
      font-size: 12px;
      font-weight: 600;
    }
  `;

  // 5. Inject Isolated Shadow DOM Overlay Card into Studentpad / SpareRoom / OpenRent / UniHomes / Rightmove
  function renderShadowContent(shadow, apiData, adId, portal, meta) {
    const portalLabel = formatPortalLabel(portal);
    if (!apiData.matched) {
      shadow.innerHTML = `
        <style>
          .unmatched-banner {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-left: 5px solid #2563eb;
            border-radius: 10px;
            padding: 14px 18px;
            color: #1e293b;
            font-size: 14px;
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
            text-align: left;
          }
          .unmatched-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
          }
          ${SHARED_DRAWER_STYLES}
        </style>
        <div class="unmatched-banner">
          <div class="unmatched-top">
            <div>
              <strong>🏠 Exeter HMO Room Tracker (${escapeHtml(portalLabel)}):</strong>
              No past tenant reviews yet for <b>${escapeHtml(meta.streetHint)} (${escapeHtml(meta.outcode)})</b>
              ${meta.detectedLandlord ? `• Landlord/Agent: <b>${escapeHtml(meta.detectedLandlord)}</b>` : ""}
              • Advert <code>#${escapeHtml(adId)}</code>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              <a class="btn-outline" href="https://exeter-hmo-tracker.onrender.com/" target="_blank" rel="noopener">Exeter Map Dashboard ↗</a>
              <button class="btn" id="submit-review-btn">Lived here? Add Room Review</button>
            </div>
          </div>
          ${buildReviewDrawerHtml(meta, apiData, portal)}
        </div>
      `;
      wireDrawerEvents(shadow, adId, portal, meta);
      return;
    }

    const s = apiData.avg_scores || {
      noise: 0,
      warmth_damp: 0,
      water_pressure: 0,
      deposit_fairness: 0,
      agency_comm: 0,
    };
    let matchTag = `Verified Room Profile (${portalLabel})`;
    if (apiData.match_method === "PERCEPTUAL_IMAGE_HASH") {
      matchTag = `Cross-Portal Photo Match (${64 - apiData.hamming_distance}/64 bits identical)`;
    } else if (apiData.match_method === "STREET_ADDRESS_MATCH") {
      matchTag = `Matched via Exeter Street Profile (${apiData.street_hint})`;
    }

    const rentAlert =
      apiData.rent_change_pct !== null && apiData.rent_change_pct > 0
        ? `<span class="pill pill-warn">Rent +${apiData.rent_change_pct}% (was £${apiData.earliest_rent_pcm})</span>`
        : "";

    const reviewsHtml = apiData.reviews
      .map(
        (r) => `
        <div class="review-item">
          <div class="review-meta">
            <strong>Tenancy: ${escapeHtml(r.tenancy_period)}</strong>
            <span>Paid: £${escapeHtml(r.rent_paid_pcm)} pcm</span>
            ${r.landlord_or_agency ? `<span class="agency-tag">🏢 ${escapeHtml(r.landlord_or_agency)}</span>` : ""}
            ${r.verified ? '<span class="verified-tag">✓ Verified Tenancy</span>' : ""}
          </div>
          <p class="review-text">"${escapeHtml(r.comment)}"</p>
        </div>
      `
      )
      .join("");

    shadow.innerHTML = `
      <style>
        .card {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #ffffff;
          border: 1px solid #e2e8f0;
          border-left: 5px solid #2563eb;
          border-radius: 10px;
          padding: 16px;
          box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
          color: #0f172a;
          text-align: left;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 12px;
          flex-wrap: wrap;
          border-bottom: 1px solid #f1f5f9;
          padding-bottom: 12px;
        }
        .title {
          font-size: 16px;
          font-weight: 700;
          margin: 0 0 4px 0;
        }
        .subtitle {
          font-size: 12px;
          color: #64748b;
        }
        .pills {
          display: flex;
          gap: 6px;
          align-items: center;
          flex-wrap: wrap;
        }
        .pill {
          font-size: 11px;
          font-weight: 600;
          padding: 4px 8px;
          border-radius: 999px;
          background: #f1f5f9;
          color: #334155;
        }
        .pill-warn {
          background: #fef2f2;
          color: #dc2626;
          border: 1px solid #fecaca;
        }
        .metrics-grid {
          display: grid;
          grid-template-columns: repeat(5, 1fr);
          gap: 8px;
          margin: 14px 0;
        }
        .metric-box {
          background: #f8fafc;
          border: 1px solid #e2e8f0;
          border-radius: 8px;
          padding: 8px 8px;
          text-align: center;
        }
        .metric-label {
          font-size: 11px;
          color: #475569;
          display: block;
          margin-bottom: 4px;
        }
        .metric-val {
          font-size: 14px;
          font-weight: 700;
          color: #fff;
          padding: 2px 8px;
          border-radius: 6px;
          display: inline-block;
        }
        .reviews-list {
          margin-top: 12px;
          border-top: 1px solid #f1f5f9;
          padding-top: 10px;
        }
        .review-item {
          background: #f8fafc;
          border-radius: 6px;
          padding: 10px 12px;
          margin-bottom: 8px;
        }
        .review-meta {
          display: flex;
          flex-wrap: wrap;
          gap: 12px;
          font-size: 12px;
          color: #475569;
          margin-bottom: 4px;
        }
        .agency-tag {
          color: #1e40af;
          background: #dbeafe;
          padding: 1px 6px;
          border-radius: 4px;
          font-weight: 600;
          font-size: 11px;
        }
        .verified-tag {
          color: #16a34a;
          font-weight: 600;
        }
        .review-text {
          margin: 0;
          font-size: 13px;
          line-height: 1.45;
          color: #1e293b;
        }
        ${SHARED_DRAWER_STYLES}
      </style>

      <div class="card">
        <div class="header">
          <div>
            <div class="title">🏠 ${escapeHtml(apiData.canonical_label)} (${escapeHtml(apiData.street_hint)}, ${escapeHtml(apiData.outcode)})</div>
            <div class="subtitle">${escapeHtml(matchTag)} • Room ID #${escapeHtml(apiData.room_id)}</div>
          </div>
          <div class="pills">
            <span class="pill">Relisted ${escapeHtml(apiData.times_listed_18m)}x across portals</span>
            ${rentAlert}
            <a class="btn-outline" href="https://exeter-hmo-tracker.onrender.com/" target="_blank" rel="noopener">Map Dashboard ↗</a>
            <button class="btn" id="submit-review-btn">+ Add Review</button>
          </div>
        </div>

        <div class="metrics-grid">
          <div class="metric-box">
            <span class="metric-label">Noise Isolation</span>
            <span class="metric-val" style="background:${scoreBadgeColor(s.noise)}">${s.noise} / 5</span>
          </div>
          <div class="metric-box">
            <span class="metric-label">Warmth & Mould</span>
            <span class="metric-val" style="background:${scoreBadgeColor(s.warmth_damp)}">${s.warmth_damp} / 5</span>
          </div>
          <div class="metric-box">
            <span class="metric-label">Water Pressure</span>
            <span class="metric-val" style="background:${scoreBadgeColor(s.water_pressure)}">${s.water_pressure} / 5</span>
          </div>
          <div class="metric-box">
            <span class="metric-label">Deposit Return</span>
            <span class="metric-val" style="background:${scoreBadgeColor(s.deposit_fairness)}">${s.deposit_fairness} / 5</span>
          </div>
          <div class="metric-box">
            <span class="metric-label">Landlord / Agency</span>
            <span class="metric-val" style="background:${scoreBadgeColor(s.agency_comm ?? 3.0)}">${s.agency_comm ?? 3.0} / 5</span>
          </div>
        </div>

        <div class="reviews-list">
          ${reviewsHtml || '<div class="subtitle">No un-embargoed reviews published yet.</div>'}
        </div>

        ${buildReviewDrawerHtml(meta, apiData, portal)}
      </div>
    `;
    wireDrawerEvents(shadow, adId, portal, meta);
  }

  function wireDrawerEvents(shadow, adId, portal, meta) {
    const openBtn = shadow.getElementById("submit-review-btn");
    const cancelBtn = shadow.getElementById("cancel-review-btn");
    const drawer = shadow.getElementById("review-drawer");
    const form = shadow.getElementById("hmo-review-form");
    const statusEl = shadow.getElementById("submit-status");

    if (openBtn && drawer) {
      openBtn.addEventListener("click", () => {
        drawer.style.display =
          drawer.style.display === "none" ? "block" : "none";
      });
    }
    if (cancelBtn && drawer) {
      cancelBtn.addEventListener("click", () => {
        drawer.style.display = "none";
      });
    }
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        const submitPayload = {
          portal: portal,
          external_ad_id: adId,
          outcode: meta.outcode,
          street_hint: fd.get("street_hint"),
          floor_level: fd.get("floor_level"),
          room_aspect: fd.get("room_aspect"),
          tenancy_period: fd.get("tenancy_period"),
          rent_paid_pcm: parseInt(fd.get("rent_paid_pcm"), 10),
          landlord_or_agency: fd.get("landlord_or_agency") || "",
          noise_isolation: parseInt(fd.get("noise_isolation"), 10),
          winter_warmth_damp: parseInt(fd.get("winter_warmth_damp"), 10),
          water_pressure: parseInt(fd.get("water_pressure"), 10),
          deposit_fairness: parseInt(fd.get("deposit_fairness"), 10),
          agency_communication: parseInt(fd.get("agency_communication"), 10),
          comment: fd.get("comment"),
          hold_for_deposit_30d: fd.get("hold_for_deposit_30d") === "on",
          image_urls: meta.imageUrls,
        };

        statusEl.style.color = "#2563eb";
        statusEl.textContent = "Hashing room photos & saving review...";

        chrome.runtime.sendMessage(
          {
            type: "SUBMIT_SPAREROOM_REVIEW",
            payload: submitPayload,
          },
          (res) => {
            if (!res || !res.ok) {
              statusEl.style.color = "#dc2626";
              statusEl.textContent = `Failed to save review: ${(res && res.error) || "Is FastAPI running on port 8000?"}`;
              return;
            }
            if (res.data.embargoed) {
              statusEl.style.color = "#16a34a";
              statusEl.textContent = `✓ Saved! Embargoed until ${res.data.embargo_until} while your deposit is returned.`;
              setTimeout(() => {
                drawer.style.display = "none";
              }, 2200);
            } else {
              chrome.runtime.sendMessage(
                {
                  type: "LOOKUP_SPAREROOM_AD",
                  payload: {
                    portal: portal,
                    external_ad_id: adId,
                    outcode: meta.outcode,
                    street_hint: meta.streetHint,
                    current_rent_pcm: meta.currentRentPcm,
                    image_urls: meta.imageUrls,
                  },
                },
                (lookupRes) => {
                  if (lookupRes && lookupRes.ok) {
                    renderShadowContent(
                      shadow,
                      lookupRes.data,
                      adId,
                      portal,
                      meta
                    );
                  }
                }
              );
            }
          }
        );
      });
    }
  }

  function findBestAnchor() {
    // 1. Exeter Studentpad Advert Detail Page (/Advert/21135193):
    // Inject directly below the visible property title H2 in the right column (.topRight .keyFacts h2)
    const studentpadAdvertH2 = document.querySelector(
      "h2#ctl00_ContentPlaceHolder1_ctl00_Ctrl_AdvertHeader_advertTitleWrapper, .topRight .keyFacts h2, .property_details_content_wrapper .keyFacts h2"
    );
    if (studentpadAdvertH2) return studentpadAdvertH2;

    // 2. Exeter Studentpad Search / Home Page (/Accommodation):
    const studentpadSearch =
      document.querySelector("#propertySearch") ||
      document.querySelector(".v2_search_main") ||
      document.querySelector("#pageContent");
    if (studentpadSearch) return studentpadSearch;

    // 3. SpareRoom / OpenRent / Rightmove / Exeter Student Pads:
    return (
      document.querySelector("div.listing-header") ||
      document.querySelector(".property-header") ||
      document.querySelector(".advert-header") ||
      document.querySelector("h1") ||
      document.querySelector("main") ||
      document.body.firstElementChild
    );
  }

  function injectReviewOverlay(apiData, adId, portal, meta) {
    const anchor = findBestAnchor();
    if (!anchor) return;

    let host = document.getElementById("exeter-hmo-shadow-host");
    let shadow;
    if (!host) {
      host = document.createElement("div");
      host.id = "exeter-hmo-shadow-host";
      host.style.cssText =
        "margin: 12px 0; width: 100%; box-sizing: border-box; clear: both; position: relative; z-index: 9999;";
      anchor.insertAdjacentElement("afterend", host);
      shadow = host.attachShadow({ mode: "open" });
    } else {
      shadow = host.shadowRoot;
    }

    renderShadowContent(shadow, apiData, adId, portal, meta);
  }

  // 6. Bootstrapping logic
  function init() {
    const { portal, adId } = detectPortalAndAdId();
    if (!adId) return;

    const meta = scrapePageMetadata();
    chrome.runtime.sendMessage(
      {
        type: "LOOKUP_SPAREROOM_AD",
        payload: {
          portal: portal,
          external_ad_id: adId,
          outcode: meta.outcode,
          street_hint: meta.streetHint,
          current_rent_pcm: meta.currentRentPcm,
          image_urls: meta.imageUrls,
        },
      },
      (response) => {
        if (response && response.ok) {
          injectReviewOverlay(response.data, adId, portal, meta);
        } else {
          injectReviewOverlay({ matched: false }, adId, portal, meta);
        }
      }
    );
  }

  init();
})();
