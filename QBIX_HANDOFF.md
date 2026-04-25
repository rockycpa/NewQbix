# Qbix Centre — Project Handoff
*Last updated: April 25, 2026*

> **For the next Claude:** Read this top-to-bottom before starting work. The "Recent Decisions" and "Outstanding Issues" sections at the end are the most current.

---

## Project Overview

**Qbix Centre** — coworking space at 500A Northside Crossing, Macon GA 31210 (RoseAn Properties LLC)
**Owner:** Rocky Davidson — semi-retired CPA, non-developer, recent cataract surgery (needs large fonts, good contrast)
**Goal:** Flask web app replacing WordPress site (qbixcentre.com) and WhatSpot booking system

---

## Working with Rocky

- **No command line.** Rocky uses GitHub Desktop only — never give terminal commands as instructions.
- **Cataract surgery recovery** — keep code/text readable, prefer 16px+ font in any UI work.
- **Visual confirmations help.** Screenshots, "click X then Y" walkthroughs work better than abstract descriptions.
- **He's a CPA, not a dev.** Explain technical concepts in plain language without being condescending. Use analogies where they help.
- **He'll catch design issues you miss.** When he says something "doesn't look right" on his S24 Ultra or desktop browser, take that seriously and ask for a screenshot if needed.
- **Test plans matter.** When you make a change, tell him exactly what to look for to verify it worked.

---

## Live URLs

- **Public site:** https://web-production-395db.up.railway.app
- **Admin login:** /admin/login — username: `admin`, password: `QbixAdmin2026!`
- **Emergency bypass:** `/admin/emergency-login-rocky2026` — REMOVE after Twilio verified
- **GitHub repo:** rockycpa/NewQbix (deployed via GitHub Desktop → Railway auto-deploys)

---

## Tech Stack

| Tool | Purpose |
|---|---|
| Railway (Hobby $5/mo) | Hosts Flask app |
| GitHub (rockycpa/NewQbix) | Code — Rocky uses GitHub Desktop |
| PostgreSQL (Railway) | Persistent data storage |
| Cloudinary (Free tier) | Photo storage — WebP, CDN delivery, alt text, media library |
| Twilio (+18665096310) | SMS — login 2FA, booking confirmations, contact alerts (pending toll-free verification) |
| Google Analytics GA4 | Traffic (G-YCMYW731TM, Property ID: 390464948) |
| Google Search Console | Search query data (domain property: qbixcentre.com) |
| Google Cloud (qbixcentre project) | GA4 Data API + Search Console API, service account |
| Outlook (qbixcentre@outlook.com) | All outbound email — manual via Notify section |
| Anthropic API | AI newsletter draft + social posts + photo alt text suggestions |

**Retired (April 2026):** AWS SES. The app no longer sends email at all. All outbound email flows through the Notify section, which opens Outlook with recipients pre-populated.

---

## Railway Environment Variables

| Variable | Notes |
|---|---|
| ADMIN_USERNAME | admin |
| ADMIN_PASSWORD_HASH | hashed |
| ADMIN_EMAIL | qbixcentre@outlook.com |
| ADMIN_PHONE | 4787379107 |
| SECRET_KEY | set |
| APP_URL | update to https://qbixcentre.com after domain points |
| TWILIO_ACCOUNT_SID | set in Railway (starts with AC...) |
| TWILIO_AUTH_TOKEN | set |
| TWILIO_PHONE_NUMBER | +18665096310 |
| GA_MEASUREMENT_ID | G-YCMYW731TM (verify it's set — was missing in last review) |
| GA_PROPERTY_ID | 390464948 |
| GA_SERVICE_ACCOUNT_JSON | full JSON set — also used for Search Console |
| SC_SITE_URL | sc-domain:qbixcentre.com |
| DATABASE_URL | auto from Railway PostgreSQL |
| ANTHROPIC_API_KEY | set |
| CLOUDINARY_CLOUD_NAME | dglvplrc0 |
| CLOUDINARY_API_KEY | set |
| CLOUDINARY_API_SECRET | set |

**Removed (April 2026):** SMTP_PASS, SMTP_HOST, SMTP_PORT, SMTP_USER, SENDGRID_API_KEY, FROM_EMAIL, FROM_NAME — Rocky to delete any of these still in Railway.

---

## File Structure

```
Documents/GitHub/NewQbix/
├── app.py                          ← Main Flask app (~2000 lines)
├── requirements.txt
├── .env.example
├── templates/
│   ├── admin/
│   │   └── dashboard.html          ← Main admin UI (~5000 lines, all-in-one)
│   ├── public/
│   │   ├── home.html               ← One-page site
│   │   ├── office_detail.html
│   │   ├── news.html, news_post.html
│   │   ├── contact.html, privacy.html
│   │   ├── book_home.html, book_calendar.html
│   ├── base.html                   ← Shared layout
│   ├── login.html, setup.html
│   ├── onboard.html, onboard_home.html, onboard_expired.html
│   └── ...
├── static/
│   └── img/favicon.svg             ← Gold Q on navy
```

---

## Architectural Notes

### Data Persistence

All data lives in **PostgreSQL** via `save_data(data)` and `load_data()` in app.py. The DB is a single JSON blob — every read pulls the whole tree, every write replaces it. Fine for this scale.

`DEFAULT_DATA` at the top of app.py defines the initial schema. New keys default to safe empty values.

### Email — Disabled

A stub `send_email()` function near the top of app.py logs and returns False. Old call sites (newsletter publish, onboarding link, conference room summary, admin notifications) are kept intact but no-op. **Don't try to wire up real email** — Rocky has explicitly chosen the Notify→Outlook workflow.

### Notify Section

Admin → Notify tab → pick audience → Open in Email Client → opens `mailto:` URL with BCC pre-populated → Rocky composes/formats in Outlook and uses Outlook Quick Parts for templates.

### Photo Storage

All photos in Cloudinary as WebP. Always reference `photo.url` (never `photo.data`, that's legacy).

The orphan scanner (Media tab) compares Cloudinary against `public_id` references in: `DB.offices[].photos`, `DB.posts[].heroPhoto/galleryPhotos`, `DB.newsletter[].heroPhoto/galleryPhotos`, `DB.homeGallery`, `DB.attractionPhotos`. Any new photo storage location must be added to `getUsedPublicIds()` in dashboard.html.

### Admin Login

Currently username + password + TOTP 2FA. Will switch to phone + SMS code once Twilio is live.

### Booking (current state — to be reworked)

Currently in `templates/public/book_home.html` and `book_calendar.html`. Member identifies via email; pending the Twilio switch to phone-based.

---

## Admin Tab Structure

1. **Content** — SEO keywords, page meta editor, news categories, home gallery, site amenities
2. **Home Page** — WYSIWYG editor for all home page text, landmark photos, attraction tiles
3. **Marketing** — contact messages, GBP health, action queue, GA4, lead sources, search console
4. **Media** — Cloudinary library, orphan scanner
5. **Offices** — tiles, edit/add, per-office amenities
6. **Members** — active/pending/archive, conf hours, agreements
7. **Occupants** — people per office
8. **Waiting List** — prospective tenants
9. **Notify** — recipient picker → Outlook (no rich text in app, formatting happens in Outlook)
10. **Newsletter** — AI draft, scheduling, social posts, history
11. **Bookings** — conference room bookings

---

## DB Key Fields

### Office
```json
{
  "id": "_abc123", "num": "19A", "status": "Vacant", "member": "",
  "sqft": 140, "dormer": null, "listDues": 725, "discount": 0, "confHours": 6,
  "description": "...", "amenities": ["Corner Office"],
  "photos": [{ "url": "...", "public_id": "...", "alt": "..." }],
  "tenantStart": ""
}
```

### Member
Has email, phone, status (Active/Pending/Archived), conf hours, agreements. Phone field exists but may not be populated for all members — backfill needed before SMS login goes live.

### Newsletter post
```json
{
  "id": "_abc123", "subject": "", "body": "",
  "category": "Monthly Update",
  "date": "2026-04-16T09:00:00", "scheduledFor": null,
  "draft": true, "sent": false,
  "heroPhoto": {...}, "galleryPhotos": [...]
}
```

### Contact message
```json
{ "id": "_abc123", "name": "", "email": "", "phone": "", "subject": "", "message": "", "timestamp": "", "read": false }
```

### Booking (existing)
Conference room booking record — date, start/end time, member, title. Used by `book_calendar.html`. Member identification by email currently.

---

## CSS Variables

```css
/* Public site (base.html) */
--navy:#1a2744; --gold:#c9a84c; --gold2:#e8c97a;
--light:#f8f6f1; --txt:#2d2d2d; --txt2:#666; --border:#e2ddd5; --r:10px;

/* Admin dashboard */
--bg:#0b0d11; --sur:#13161e; --sur2:#1b1f2b; --bdr:#2e3448;
--acc:#d4a843; --blue:#5b9bd5; --grn:#4db887; --red:#d96060;
--txt:#edf1fb; --txt2:#a8b4cc; --txt3:#6b7a96;
```

---

## Recent Decisions (April 2026)

1. **AWS SES retired.** All outbound app email is gone. `send_email()` is a stub no-op.
2. **Notify → Outlook workflow.** Admin uses Notify to assemble recipients, then formats in Outlook with Quick Parts/Templates. Rich text editing inside the app was considered and explicitly rejected — Outlook handles it better.
3. **No app-side rich text editor.** Keeps things simple.
4. **Phone-based auth coming, not yet live.** When Twilio toll-free verification completes, login (member AND admin) switches to phone + SMS code. Members without phone numbers on file just won't be able to self-serve until Rocky adds them.
5. **Public browsing stays open.** Only the booking action is gated behind login.
6. **Members-only booking.** Non-members hit a wall with a "contact us about membership" message.
7. **No rate limiting needed yet.** Volume is low; non-members never reach SMS code phase.

---

## Outstanding Issues

1. **Twilio toll-free verification** — pending. When it goes live, build the SMS code login flow.
2. **Rotate AWS SES IAM keys** — Rocky to delete user `ses-smtp-user.20260401-160520` in AWS Console (those keys were committed to GitHub before being removed).
3. **Delete leftover Railway env vars** — SMTP_PASS, SMTP_HOST, SMTP_PORT, SMTP_USER, SENDGRID_API_KEY, FROM_EMAIL, FROM_NAME if any still exist.
4. **Domain pointing** — still on WordPress/GoDaddy. After pointing to Railway: update `APP_URL`, submit sitemap.
5. **Cancel WhatSpot ($192/yr)** — after new booking system confirmed working.
6. **Cancel GoDaddy** — after domain pointed.
7. **JSON-LD geo coordinates** — currently approximate (32.9, -83.7); update with exact from Google Maps.
8. **Backfill member phone numbers** — needed before SMS login goes live.
9. **Verify GA_MEASUREMENT_ID is in Railway** — was not visible in environment variable list during last review.

---

## Recent Completed Work

### April 25, 2026 session
- Mobile nav crunching fix (S24 Ultra)
- Desktop nav fix — stacked "Macon, GA" under "Qbix Centre"
- Favicon (gold Q on navy) added at static/img/favicon.svg
- Office card hero photo now clickable to detail page
- Orphan photo scanner — added DB.newsletter, DB.homeGallery, DB.attractionPhotos coverage
- AWS SES code retirement — credentials CSV deleted from repo, .env.example cleaned, send_email stubbed

### Earlier (April 23)
- "Why North Macon" attractions section with landmark photos and category tiles
- Per-office amenity pills on home page office cards
- Office detail page prev/next navigation
- Home Page admin tab (WYSIWYG)
- Jinja bracket notation fix (`tile['items']` not `tile.items`)

---

## Build Queue — What's Next

1. **Booking section overhaul** ← next focus. The current implementation is email-based and predates the Notify/Twilio decisions. Rocky wants to start working through it. The auth piece waits for Twilio, but the calendar/availability/hours-tracking pieces can move now.
2. **Conference Room public page** — `/conference-room` for SEO (people search "meeting space Macon")
3. **Marketing Campaign Tracker** — manual log: platform, dates, spend, impressions, inquiries, conversions
4. **House Guidelines Document** — `/guidelines` page or downloadable PDF
5. **PWA evaluation** — admin installable as phone app

---

## How to Deploy

1. Edits land in `Documents/GitHub/NewQbix/` (the workspace folder Rocky selected)
2. GitHub Desktop → review diff → Commit to main → Push origin
3. Railway auto-deploys ~2 min
4. If broken: F12 Console for JS errors; Railway logs for Python errors
5. If DB empty: Restore from Backup in admin save bar

---

## Rocky's Contact

- Phone: (478) 737-9107 (AT&T personal) | Google Voice on site: (478) 216-2876
- Email: rockycpa@gmail.com (admin: qbixcentre@outlook.com)
- Browser: Microsoft Edge (sometimes needs restart for downloads); Chrome has Claude extension
