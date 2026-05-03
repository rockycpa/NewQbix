# Qbix Centre — Project Handoff
*Last updated: May 2, 2026*

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

- **Public site:** https://www.qbixcentre.com (canonical, www form)
- **Admin login:** https://www.qbixcentre.com/admin/login — username: `admin`, password: `QbixAdmin2026!`
- **Railway URL (still works as fallback):** https://web-production-395db.up.railway.app
- **Emergency bypass:** `/admin/emergency-login-rocky2026` — REMOVE after Twilio verified
- **GitHub repo:** rockycpa/NewQbix (deployed via GitHub Desktop → Railway auto-deploys)

> **Important:** Always log into admin via the **www.qbixcentre.com** URL, not the Railway one. The newsletter teaser flow builds the email link from `window.location.origin`, so accessing admin via the wrong URL puts the wrong domain in your sent newsletters.

> **Apex (`qbixcentre.com` without www) is currently NOT pointed at Railway** — only the `www.` subdomain is. Setting up an apex → www redirect in Railway custom domains is a "nice to have" cleanup, not urgent.

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
| APP_URL | https://www.qbixcentre.com (set May 2026 after domain transfer) |
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

All photos in Cloudinary. Always reference `photo.url` (never `photo.data`, that's legacy).

The orphan scanner (Media tab) compares Cloudinary against `public_id` references in: `DB.offices[].photos`, `DB.posts[].heroPhoto/galleryPhotos`, `DB.newsletter[].heroPhoto/galleryPhotos`, `DB.homeGallery`, `DB.attractionPhotos`. Any new photo storage location must be added to `getUsedPublicIds()` in dashboard.html.

**Image delivery optimization (May 2026).** A Jinja filter `cl_optimize(width)` in app.py injects `f_auto,q_auto,w_<n>,c_limit/` after `/upload/` in Cloudinary URLs at render time. `f_auto` delivers WebP/AVIF to modern browsers; `q_auto` picks the best size/quality tradeoff per-image; `c_limit` prevents upscaling. Used in every public template (news.html, news_post.html, home.html, offices.html, office_detail.html). Some places combine the filter with existing inline transforms like `ar_16:9,c_fill,w_1200` — those preserve aspect-ratio crops and add `f_auto,q_auto` inside the same transform string. Most below-fold images also have `loading="lazy" decoding="async"`. News page went from ~5s to ~2s; home page from ~3s to ~2.5s after this landed.

### Newsletter — Send via Outlook (May 2026 rebuild)

Newsletter compose has two entry paths from the same admin tab:

- **Generate Draft with AI** (existing) — fills the Quill editor from an Anthropic call
- **Write My Own — No AI** (new) — opens the editor empty so Rocky can write a newsletter from scratch without firing the AI call

Sending uses **Send via Outlook** (replaced the old "Save & Email Members" button). Opens a modal with:

1. Recipient summary — counts Active Occupants with email on file (BCC list source)
2. **"Also publish to website"** checkbox, defaulted to **checked** (recommended path) — locked when editing an already-published post (un-publishing from this flow would silently 404 the post; instead use Delete in Past Newsletters)
3. **Editable teaser textarea** — only visible when the website checkbox is checked. Pre-filled with: greeting + auto-extracted first paragraph(s) of body (~280-420 chars, walks `p`/`h1-6`/`li`/`blockquote` until threshold) + visible URL of post-to-be + sign-off

Two send modes:
- **Teaser mode** (publishToSite=true, default): email body is the (editable) teaser + visible URL. Plain text only — no clipboard HTML needed. Recipients click through to read the full newsletter on the website with full formatting and photos.
- **Full-body mode** (publishToSite=false, opt-out): full Quill content goes in the email body as plain text via mailto, AND the formatted HTML is copied to the clipboard so Rocky can `Ctrl+V` in Outlook to paste with full formatting (bold, lists, headings, etc.).

**Critical timing detail.** `window.open(mailto)` and `navigator.clipboard.write()` BOTH have to fire synchronously inside the click handler — Edge (and other browsers) consume the user-gesture token across `await` boundaries, so calling them after a save round-trip is silently swallowed as a popup. Order in `sendNewsletterViaOutlook()`: build emails + mailto string + clipboard payload, then close modal, then fire clipboard.write + window.open SYNCHRONOUSLY, THEN await the save. If save fails, Outlook is still open with the newsletter ready — toast says "Save failed (Outlook still opened)".

**Pre-generated postIds.** The frontend generates a `_xxxxxxx` id when Rocky toggles the website checkbox, includes it in the email URL, and passes it to `/admin/api/publish-newsletter` via the `postId` field so the saved post lands at the same URL. Backend validates the shape (must start with `_`, alphanumeric, 2-16 chars) and falls back to a server-generated id if missing or invalid.

**mailto line endings.** Outlook on Windows strips bare `\n` in mailto bodies, collapsing emails to one line. All teaser/plain-text bodies use `\r\n` (CRLF), and `sendNewsletterViaOutlook` normalizes `\r?\n` → `\r\n` on the textarea content before encoding. **Don't switch to `\n` in any new mailto code path.**

**Quill nested lists.** Quill exports indented list items as `<li class="ql-indent-1">` etc. on a flat list, NOT as nested `<ul>` tags. The website CSS in news_post.html has `.nl-body li.ql-indent-1..5` rules that restore visual indentation and vary the bullet marker. The clipboard-paste path also runs `_quillHtmlToEmailHtml()` which inlines those `ql-indent-N` classes as inline `style="padding-left:..."` so indentation survives Outlook's CSS-class stripping when pasted.

**Newsletter typography on /news.** `templates/base.html` applies a global `*{margin:0;padding:0}` reset that flattens Quill HTML. `news_post.html` has a scoped `<style>` block defining `.nl-body p/h1-h4/ul/ol/li/strong/em/a/blockquote` rules to restore prose-style typography only inside the post body. **Don't try to fix this in base.html — the reset is intentional for the rest of the site.**

### Search Console API (May 2026 — was missing)

The `/admin/api/searchconsole` route in app.py was missing entirely until May 2026 — frontend had been calling it since the marketing dashboard was built, hitting a 404 silently. The route now uses the same `GA_SERVICE_ACCOUNT_JSON` service account as GA4 analytics, with `SC_SITE_URL=sc-domain:qbixcentre.com` (set in Railway). Two API calls: one for aggregate KPIs (no dimensions = single totals row), one for top 100 queries by clicks (default sort), then re-sorted by impressions client-side and trimmed to top 20. Helpful error messages mapped for 403 (service account permissions) and 404 (property URL mismatch).

### Admin Login

Username + password + SMS 2FA via Twilio (the 2FA code is sent to `ADMIN_PHONE`). The 2FA pending-code store lives in the DB blob under `_pending2fa` so it survives deploys / works across gunicorn workers.

### Booking

Phone-based login at `/book`. Files: `templates/public/book_home.html` (phone entry), `templates/public/book_calendar.html` (the calendar + booking UI).

**Auth flow.** `/book/request-code` looks up the phone in **occupants only** (members are intentionally not matched — see "Occupants vs members" below). When Twilio toll-free verification clears, a 6-digit SMS code goes out and `/book/verify` issues a session token. Until then, only `ADMIN_PHONE` (4787379107) bypasses the SMS step — that occupant must exist as Active and linked to an Active member.

**Session tokens.** Booking session tokens, 2FA pending codes, and onboarding-link tokens all persist in the JSONB blob (keys: `_bookingTokens`, `_pending2fa`, `_onboardTokens`). They were originally module-level dicts but that broke under multi-worker / dyno-restart scenarios — tokens issued on one worker would 401 on another. The helper functions `_bt_get/_set/_del`, `_p2fa_get/_set/_del`, and `_ot_get/_set/_del` in app.py handle reads/writes; `_get` filters out expired entries; `_set` lazily prunes the store. After deploy, **existing in-flight tokens evaporate** (members must re-login, in-flight onboarding links must be regenerated).

**Occupants vs members.**
- A *member* is a company / billing account. Hour limits and overage rates live here (`member.confHours` summed from offices).
- An *occupant* is a person, with `occupant.company` pointing at the parent member's name.
- All bookings are made *by occupants*. The booking record stamps:
  - `memberName` — the occupant's name (the booker)
  - `memberEmail` — the occupant's email
  - `parentMember` — the member account whose monthly hours bucket this booking draws from
- Multiple occupants under one member share the same monthly bucket.
- The helper `_booking_billed_to(b)` returns `b.parentMember or b.memberName`, so legacy bookings (created before this rollup field existed) still credit the right account by name match.

**Scheduling rules.** 15-minute increments, 7am–6pm, two-month visible window (current month + next). Conference room and offices both bookable. Conflict check excludes the booking being edited. Overage gate prompts the user to accept additional charges before saving.

**SMS messages.** Confirmation, 24-hour reminder, edit, and cancel SMS templates are admin-editable in the Booking Settings panel.

**Endpoints.** `/book` (home), `/book/request-code`, `/book/verify`, `/book/calendar`, `/book/slots` (one month of availability + the user's hours-used), `/book/my-bookings` (all of the occupant's upcoming bookings across every resource — single round-trip so the panel doesn't hang on month-by-month polling), `/book/create`, `/book/edit`, `/book/cancel`, plus `/admin/api/booking-create|edit|cancel|bookable-resources` on the admin side.

**Admin side.** Bookings tab has a calendar view + a flat table. The Add/Edit Booking modal's "Occupant" picker shows occupants only (members are not bookable directly). Saving an admin booking validates that the occupant is Active and linked to an Active member account; otherwise returns a clear error.

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

### Booking
```json
{
  "id": "_abc123",
  "memberName":   "Rolando Davidson",       // the occupant who booked
  "memberEmail":  "rolando@example.com",
  "parentMember": "Davidson Companies LLC",  // member account hours roll up to
  "resourceType": "conference_room|office",
  "resourceId":   "conference_room|<office id>",
  "date":         "2026-05-06",
  "year":         2026, "month": 5,
  "start":        "07:00", "end": "07:15",
  "title":        "Meeting",
  "status":       "confirmed|cancelled",
  "createdAt":    "...",
  "createdBy":    "admin (when applicable)",
  "overageHours": 0.25,                      // present only when this booking incurred overage
  "overageRate":  25,
  "overageCharge": 6.25
}
```

Use the helper `_booking_billed_to(b)` (returns `b.parentMember or b.memberName`) anywhere you're aggregating hours. Direct `b.memberName == ...` comparisons miss legacy rows and split occupant bookings off the parent member's bucket.

### Booking Settings
```json
{
  "smsConfirmationTemplate": "...",
  "smsReminderTemplate":     "...",
  "smsEditTemplate":         "...",
  "smsCancelTemplate":       "...",
  "overageRatePerHour":      25,
  "overageWarningMessage":   "...",
  "optInDisclosure":         "..."
}
```

### Internal token stores (do not edit by hand)

`_bookingTokens`, `_pending2fa`, `_onboardTokens` — see "Booking" architectural note.

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

## Recent Decisions (May 2026)

1. **Newsletter teaser+link is the default send mode.** When sending via Outlook, "Also publish to website" defaults to checked, and the email becomes a short editable teaser linking to the post on qbixcentre.com. Spam-friendlier than full-content emails, drives traffic to the site, and lets recipients see the full formatting/photos. Full-body-in-email mode still available by unchecking the box.
2. **www.qbixcentre.com is canonical**, not the bare apex. Apex `qbixcentre.com` is currently NOT pointed at Railway; only the www form is bound. Setting up an apex → www redirect is on the cleanup list but not urgent.
3. **Cloudinary delivery optimization is now baked in.** Don't write `<img src="{{ photo.url }}">` directly in public templates — pipe through `| cl_optimize(<width>)` to get f_auto/q_auto/w_N/c_limit transformations. Cut News page load time from 5s to ~2s.
4. **Search Console route was missing from day one.** Built in May 2026; uses the same service account as GA4. If the marketing dashboard's search-queries panel ever stops loading, check `SC_SITE_URL` env var first.

## Recent Decisions (April 2026)

1. **AWS SES retired.** All outbound app email is gone. `send_email()` is a stub no-op.
2. **Notify → Outlook workflow.** Admin uses Notify to assemble recipients, then formats in Outlook with Quick Parts/Templates. Rich text editing inside the app was considered and explicitly rejected — Outlook handles it better.
3. **No app-side rich text editor.** Keeps things simple. (Note: Quill is used in the Newsletter compose flow only, since the post body needs structured HTML for the website.)
4. **Phone-based auth coming, not yet live.** When Twilio toll-free verification completes, login (member AND admin) switches to phone + SMS code. Members without phone numbers on file just won't be able to self-serve until Rocky adds them. Admin login already uses SMS 2FA.
5. **Public browsing stays open.** Only the booking action is gated behind login.
6. **Occupants book, members get billed.** Phone lookup at `/book` matches occupants only — never members directly. Bookings stamp `parentMember` so the occupant's hours roll up to their company's monthly bucket. The admin Add Booking picker also lists occupants only. This keeps multi-occupant companies clean: every occupant under one member shares the same hour pool.
7. **Tokens persist in DB.** Booking session tokens, 2FA codes, and onboarding-link tokens all live in the JSONB blob, not module-level dicts. Required because Railway can run multiple workers / restart the dyno mid-session.
8. **No rate limiting needed yet.** Volume is low; non-members never reach SMS code phase.

---

## Outstanding Issues

1. **Upgrade Twilio from trial to paid plan** — toll-free verification cleared May 2026 and SMS is working end-to-end. Trial account caps recipients to numbers Rocky has explicitly verified in the Twilio console. Upgrade in console.twilio.com → Billing → Upgrade. Expect ~$3-5/month total (toll-free number ~$2 + per-SMS $0.0079, ~50-100 SMS/month). No code changes needed; Railway env vars stay the same.
2. **Rotate AWS SES IAM keys** — Rocky to delete user `ses-smtp-user.20260401-160520` in AWS Console (those keys were committed to GitHub before being removed).
3. **Rotate Twilio Auth Token** — has been parked from earlier session.
4. **Delete leftover Railway env vars** — SMTP_PASS, SMTP_HOST, SMTP_PORT, SMTP_USER, SENDGRID_API_KEY, FROM_EMAIL, FROM_NAME if any still exist.
5. **Cancel WhatSpot ($192/yr)** — new booking system has been working; safe to cut.
6. **Cancel GoDaddy** — domain has been transferred away from them.
7. **JSON-LD geo coordinates** — currently approximate (32.9, -83.7); update with exact from Google Maps.
8. **Verify GA_MEASUREMENT_ID is in Railway** — was not visible in environment variable list during last review.
9. **Confirm Rolando is set up correctly** — must be an Active occupant with phone 4787379107, `company` field pointing at an Active member, for the admin bypass login to work end-to-end.

### Resolved this session (May 2, 2026)

- ~~Domain pointing — still on WordPress/GoDaddy~~ → www.qbixcentre.com pointed at Railway, APP_URL updated, sitemap submitted to Google + Bing
- ~~Search Console route missing~~ → built and working, returning 20 top queries
- ~~Apex domain redirect~~ → `qbixcentre.com` (no www) now redirects to `https://www.qbixcentre.com/` correctly
- ~~Backfill occupant phone numbers~~ → done
- ~~Backfill occupant email addresses~~ → done
- ~~Twilio toll-free verification — pending~~ → cleared, SMS booking flow live and tested. (Upgrade trial→paid still pending — see item 1 above.)

---

## Recent Completed Work

### May 2, 2026 session — Newsletter rebuild + domain transfer + perf

**Newsletter compose + send flow**
- New "Write My Own — No AI" entry path next to the existing AI generator — opens Quill editor empty for manual composition
- Old "Save & Email Members" button replaced with "Send via Outlook" — opens a new modal with recipient summary, "Also publish to website" checkbox (defaults to checked, locked when editing already-published posts), and an editable teaser textarea
- Teaser textarea pre-fills with: greeting + auto-extracted first paragraph(s) of body (~280-420 chars, walks block-level elements) + visible URL of post-to-be + sign-off
- Two send modes: teaser+link (default, plain text body) and full-body-in-email (with HTML clipboard copy for Ctrl+V paste in Outlook)
- `mailto` line endings normalized to CRLF — Outlook on Windows silently strips bare LFs
- Pre-generated postId threaded from the modal through `/admin/api/publish-newsletter` so the email URL matches the saved post URL exactly
- Backend `publish_newsletter` accepts client-supplied `postId` (validated for shape) with fallback to server-generated
- Photo attachment bug fixed in `publishNewsletter` override: was using `find(x => editId ? x.id===editId : true)` which matched `DB.newsletter[0]` for new posts (clobbering an unrelated post's photos); now uses `slice(-1)[0]` to grab the just-pushed entry

**Public newsletter page typography (`news_post.html`)**
- Added scoped `.nl-body` style block to restore prose typography (headings, lists, paragraphs, blockquote, links) inside the post body
- Quill nested-list support: rules for `.nl-body li.ql-indent-1..5` since Quill exports indented LIs as classes on a flat list, not nested ULs
- News index excerpt expanded from 200 chars / 80px max-height to 500 chars / 6-line CSS line-clamp
- Email clipboard helper `_quillHtmlToEmailHtml()` inlines `ql-indent-N` and `ql-align-*` classes as inline styles so Outlook preserves them

**Admin polish**
- "View Public Site" link button next to the logo in admin header (opens `/` in new tab)
- Booking page hero tightened (60px → 28px padding, smaller heading) so the sign-in card is above the fold on phones
- Nav button "Book Conference Room" → "Book A Meeting Space" (better matches the "meeting space"/"meeting rooms" search queries that have the highest impressions in Search Console)

**Domain transfer**
- qbixcentre.com transferred from GoDaddy/WordPress; www.qbixcentre.com now serves the Flask app
- `APP_URL` env var updated on Railway → `https://www.qbixcentre.com`
- JSON-LD fallback in `base.html` updated to www.qbixcentre.com
- Sitemap submitted to Google Search Console and Bing Webmaster Tools (20 URLs, including legacy WordPress newsletter posts going back to 2018)
- Apex `qbixcentre.com` (no www) currently 404s — apex→www redirect on the Outstanding list

**Search Console endpoint built**
- `/admin/api/searchconsole` was being called by the marketing dashboard JS but had no backend route — was 404ing silently since the dashboard was built
- New route uses GA_SERVICE_ACCOUNT_JSON + SC_SITE_URL env vars, two API calls (totals + top queries), client-side sort by impressions, top 20 returned
- Helpful error mapping for 403 (service account permissions) and 404 (property URL mismatch)

**Image performance (Cloudinary delivery)**
- New `cl_optimize(width)` Jinja filter in app.py inserts `f_auto,q_auto,w_<n>,c_limit/` into Cloudinary URLs
- Applied to all `<img>` tags in news.html, news_post.html, home.html, offices.html, office_detail.html
- `loading="lazy" decoding="async"` added on most below-fold images
- Result: News page load time 5s → ~2s; Home 3s → ~2.5s; photos confirmed delivering as WebP

### April 26, 2026 session — Booking module
**Phases 1–8 — initial build**
- Resource model on bookings (`resourceType`, `resourceId`); both conference room and offices bookable through one calendar
- 15-minute increments, 7am–6pm, two-month visible window (current + next)
- Hours-remaining display in header; overage gate with admin-configurable rate and warning message
- Member-side edit/cancel for future bookings; admin "create on behalf of" with full picker
- Booking Settings panel (admin) with editable SMS templates: confirmation, reminder, edit, cancel
- Admin calendar view of bookings (with conflict-aware Add Booking from a clicked cell)
- Refresh after admin booking save (`loadAdminData()` was a silent no-op — fixed: endpoint returns DB directly, not wrapped in `{data: ...}`)

**Round 1 polish**
- Add Booking modal: Start/End on the same row (Start first, End second)
- Member calendar widened to 1700px max, day cell padding bumped, day-number font 16/600
- Pip font 12px, padding 3×6, weight 500 — was painfully small before
- "My Upcoming Bookings" panel rebuilt around new `/book/my-bookings` endpoint (single round-trip across all resources, replaces sequential month-by-month polling)

**Round 2 — occupants-only model**
- `_member_by_phone` matches occupants only; returns occupant record stamped with `_phone_member_name`, `_phone_member_email`, `_phone_parent_member`
- Booking session token carries `parentMember`; `book_calendar` looks up the parent member by name (not by entry email)
- New helper `_booking_billed_to(b)` → `b.parentMember or b.memberName` (legacy fallback)
- `book_create`, `_apply_booking_edit`, `get_member_hours_used`, `send_monthly_usage` all updated to roll hours up to the parent member
- `/book/slots` keys hours-used / hours-included off the parent member account (was incorrectly keyed off the occupant)
- `/admin/api/bookable-resources` returns occupants only; admin Add/Edit Booking modal label/picker/placeholder all switched to "Occupant"
- `admin_create_booking` validates the occupant is Active and linked to an Active member; rejects with a helpful error otherwise

**Round 3 — bug fixes from Rocky's testing**
- `templates/public/book_calendar.html`: `{% block extra_js %}` was nested inside `{% block content %}`, causing Jinja to render the `<script>` block twice → `Identifier 'TOKEN' has already been declared` and silent failure of `loadMyBookings`/`loadSlots`/click handlers. Fixed block structure.
- Booking session tokens, 2FA codes, and onboarding tokens migrated from module-level dicts (`_booking_tokens`, `_pending_2fa`, `_onboard_tokens`) to DB-backed stores (`_bookingTokens`, `_pending2fa`, `_onboardTokens` in the JSONB blob) so they survive deploys and are visible to all gunicorn workers. New helpers `_bt_*`, `_p2fa_*`, `_ot_*` in app.py. **All in-flight tokens / pending onboarding links from before the deploy are invalidated.**
- `/book/my-bookings._is_mine` also matches on `parentMember` so legacy bookings whose `memberName` is the company name (rather than the occupant name) still show up in the occupant's upcoming-bookings panel.
- Auto-refresh after confirm/edit/cancel is now reliable (it was already wired but unreachable due to the Jinja-duplicate-script issue above).

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

1. **Twilio SMS auth turn-on** — when toll-free verification clears: enable the real `/book/verify` flow (right now only `ADMIN_PHONE` bypass works). Backfill member/occupant phone numbers as needed; remove the test-mode block in `book_request_code`.
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
