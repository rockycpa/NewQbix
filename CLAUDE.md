# Qbix Centre — CLAUDE.md 

Codebase guide for AI-assisted development. Update this file whenever significant changes are made.

## Project Overview

**Qbix Centre** is a coworking space in Macon, GA (500A Northside Crossing, 31210). This repo is the complete web application — public website + member-facing booking + admin management dashboard — built with Flask and deployed on Railway.

- **Live site:** https://www.qbixcentre.com
- **Deployment:** Railway (auto-deploys from GitHub `main` branch)
- **Database:** PostgreSQL on Railway
- **Image hosting:** Cloudinary
- **SMS:** Twilio (used for booking verification codes and member notifications)
- **AI:** Anthropic API (newsletter generation, social posts, agreement drafting)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python / Flask 3.0 |
| Database | PostgreSQL via psycopg2 |
| Frontend | Vanilla JS + CSS (no framework) — single-file templates |
| Hosting | Railway |
| Images | Cloudinary |
| SMS | Twilio |
| AI | Anthropic Claude API |
| Analytics | Google Analytics 4 + Google Search Console API + Microsoft Clarity |
| SEO | IndexNow (Bing), sitemap.xml, structured data (LocalBusiness) |

---

## File Structure

```
NewQbix/
├── app.py                        # Entire Flask backend (~4000+ lines)
├── requirements.txt              # Python dependencies
├── Procfile                      # Railway: gunicorn app:app
├── railway.toml                  # Railway config
├── .env.example                  # Environment variable template
├── qbix_data.json                # Fallback data (used before DB is seeded)
├── static/
│   └── img/
│       ├── favicon.svg
│       └── logo.jpg
└── templates/
    ├── base.html                 # Shared layout for all public pages
    ├── admin/
    │   ├── dashboard.html        # Main admin SPA (~5000+ lines, all-in-one)
    │   ├── bookings.html         # Admin bookings view
    │   ├── login.html            # Admin login page
    │   ├── 2fa.html              # Admin 2FA page
    │   ├── edit_document.html    # House rules / agreement editor
    │   └── setup.html            # First-run setup
    └── public/
        ├── home.html             # Homepage
        ├── offices.html          # Office listings
        ├── office_detail.html    # Individual office page
        ├── memberships.html      # Membership plans
        ├── amenities.html        # Amenities (redirects to /#amenities)
        ├── contact.html          # Contact form
        ├── privacy.html          # Privacy policy
        ├── guidelines.html       # House rules (public)
        ├── news.html             # News/blog index
        ├── news_post.html        # Individual news post
        ├── book_home.html        # Booking sign-in (phone verification)
        ├── book_calendar.html    # Booking calendar (member only, noindex)
        ├── onboard_home.html     # Onboarding landing
        ├── onboard.html          # Onboarding form (noindex)
        ├── onboard_expired.html  # Expired onboarding link
        └── sms_optin.html        # SMS opt-in disclosure
```

---

## Environment Variables

All secrets are set in Railway's environment (never committed). See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing |
| `DATABASE_URL` | PostgreSQL connection string (set by Railway) |
| `ADMIN_EMAIL` | Bootstrap admin email |
| `ADMIN_PHONE` | Bootstrap admin phone (first-run only) |
| `APP_URL` | Full URL e.g. `https://www.qbixcentre.com` |
| `ANTHROPIC_API_KEY` | Claude AI for newsletter/agreement generation |
| `GA_MEASUREMENT_ID` | Google Analytics 4 ID (G-XXXXXXX) |
| `TWILIO_ACCOUNT_SID` | Twilio SMS |
| `TWILIO_AUTH_TOKEN` | Twilio SMS |
| `TWILIO_PHONE_NUMBER` | Twilio sender number |
| `CLOUDINARY_CLOUD_NAME` | Image hosting |
| `CLOUDINARY_API_KEY` | Image hosting |
| `CLOUDINARY_API_SECRET` | Image hosting |
| `GA_PROPERTY_ID` | Google Search Console / Analytics Data API |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google service account (base64-encoded JSON) |
| `BING_WMT_API_KEY` | Bing Webmaster Tools API |
| `TOTP_SECRET` | Admin 2FA (pyotp) |

---

## Key Architecture Decisions

### Single-file admin SPA
`templates/admin/dashboard.html` is a ~5000-line self-contained single-page app. All admin UI tabs (Members, Offices, Bookings, Marketing, Website, Media, etc.) are rendered client-side by JavaScript that fetches data from `/admin/api/*` endpoints. This avoids page reloads and keeps deployment simple.

### No `requests` library — use `urllib.request`
`requests` is not installed. All outbound HTTP calls (IndexNow pings, Bing API, etc.) use `urllib.request` / `urllib.parse` from the standard library.

### PostgreSQL as primary store
All mutable data (members, offices, bookings, news posts, contact messages, settings) lives in PostgreSQL. `qbix_data.json` is a legacy fallback only.

### Admin auth: Phone + SMS 2FA
Admin login uses phone number + SMS verification code (via Twilio) rather than username/password. TOTP (pyotp) is also supported as a second factor. No admin passwords exist.

### Member booking auth: Phone OTP
Members authenticate for booking via a 6-digit SMS code sent to their registered phone. Tokens are stored in the DB with expiry.

### Background threads for non-blocking work
Long-running tasks (IndexNow pings, scheduled newsletter publishing) are dispatched in daemon threads so they don't block the HTTP response.

---

## Public Routes

| Route | Description | Notes |
|---|---|---|
| `/` | Homepage | |
| `/offices` | Office listings | |
| `/offices/<id>` | Office detail page | |
| `/memberships` | Membership plans | |
| `/amenities` | Redirects to `/#amenities` | Not in sitemap |
| `/contact` | Contact form (GET + POST) | |
| `/privacy` | Privacy policy | |
| `/guidelines` | House rules (public) | |
| `/news` | News index | |
| `/news/<post_id>` | Individual post | |
| `/book` | Booking sign-in | |
| `/book/calendar` | Booking calendar | `noindex` |
| `/book/request-code` | POST: send SMS code | |
| `/book/verify` | POST: verify code → token | |
| `/book/slots` | GET: available slots | |
| `/book/create` | POST: create booking | |
| `/book/cancel` | POST: cancel booking | |
| `/book/my-bookings` | Member booking history | |
| `/onboard` | Onboarding landing | |
| `/onboard/<token>` | Onboarding form | `noindex` |
| `/sms-optin` | SMS opt-in page | |
| `/sitemap.xml` | XML sitemap | |
| `/robots.txt` | Robots file | |
| `/12a5fb7e1c2143cd93db267462ec654c.txt` | IndexNow key file | |

---

## Admin Routes

All require login (`@login_required`).

| Route | Description |
|---|---|
| `/admin` | Dashboard (SPA) |
| `/admin/login` | Login page |
| `/admin/2fa` | 2FA verification |
| `/admin/logout` | Logout |
| `/admin/bookings` | Bookings management |
| `/admin/edit/house-rules` | Edit house rules document |
| `/admin/edit/agreement` | Edit membership agreement |
| `/admin/api/data` | GET all DB data (members, offices, etc.) |
| `/admin/api/save` | POST save data changes |
| `/admin/api/backup` | GET download JSON backup |
| `/admin/api/analytics` | GET Google Analytics data |
| `/admin/api/searchconsole` | GET Google Search Console data |
| `/admin/api/bingsearch` | GET Bing Webmaster search queries |
| `/admin/api/indexnow-ping` | POST ping Bing IndexNow |
| `/admin/api/generate-newsletter` | POST AI newsletter draft |
| `/admin/api/publish-newsletter` | POST publish post + ping IndexNow |
| `/admin/api/generate-social-posts` | POST AI social copy |
| `/admin/api/generate-agreement/<id>` | GET member agreement PDF |
| `/admin/api/onboard-link` | POST create onboarding link |
| `/admin/api/send-monthly-usage` | POST email usage report |
| `/admin/api/media` | GET media library |
| `/admin/api/media/upload` | POST upload to Cloudinary |

---

## SEO & Analytics

### Google
- **GA4:** Script in `base.html` `<head>`, controlled by `GA_MEASUREMENT_ID` env var
- **Search Console:** Data API called from `/admin/api/searchconsole` using service account
- **Structured data:** LocalBusiness JSON-LD on homepage only

### Bing
- **IndexNow key:** `12a5fb7e1c2143cd93db267462ec654c` (served at `/<key>.txt` route)
- **IndexNow pings:** Auto-triggered on newsletter publish; manual "Ping Bing" button on admin Marketing page
- **Bing Webmaster API:** `/admin/api/bingsearch` calls `GetQueryStats` endpoint with `BING_WMT_API_KEY`
- **Sitemap:** `/sitemap.xml` includes home, offices, memberships, contact, privacy, guidelines, news, /news/* posts

### Microsoft Clarity
- **Project ID:** `wp0905qqxv`
- Script in `base.html` `<head>` (loads on all public pages)
- Admin dashboard does NOT extend `base.html` so staff sessions are not tracked

### noindex pages
These pages intentionally have `noindex, nofollow`:
- `/book/calendar` (member-only booking)
- `/onboard` and `/onboard/<token>` (private onboarding links)

---

## JavaScript Utilities (dashboard.html)

Key helper functions defined in the admin dashboard:

| Function | Purpose |
|---|---|
| `fmtPhone(p)` | Format 10-digit number as `(478) 394-0417` |
| `esc(s)` | HTML-escape a string |
| `toast(msg, col, dur)` | Show toast notification |
| `openMemberMo(id)` | Open member detail modal (view + edit mode) |
| `renderContactMessages()` | Render contact form submissions |
| `loadBingSearch()` / `setBingDays()` | Bing search query widget |
| `pingIndexNow()` | Trigger IndexNow ping from admin UI |

---

## Deployment

1. Push to `main` branch on GitHub
2. Railway auto-deploys (takes ~2 minutes)
3. No build step — Railway runs `gunicorn app:app` per Procfile
4. Environment variables are set in Railway dashboard (never in code)

### Python packages note
Do NOT add `requests` to requirements — use `urllib.request` instead. All current HTTP calls use stdlib.

---

## Recent Work (May 2026)

- **Email (Microsoft 365 SMTP):** Replaced `send_email` stub with real implementation using `smtplib` + STARTTLS to `smtp.office365.com:587`. Credentials: `SMTP_EMAIL` / `SMTP_PASSWORD` env vars. Async helper `send_email_async` wraps sends in daemon threads so HTTP responses are never blocked. Four active email flows:
  - **Contact form:** Admin alert to `ADMIN_EMAIL` (full message detail table) + auto-reply to submitter
  - **Booking confirmation:** Formatted confirmation email to occupant after successful booking
  - **Onboarding link:** Email to prospect when admin generates an onboard link
  - **Onboarding complete:** Welcome email to new member + admin notification on form submission
  - **Newsletter:** Bulk email to all Active members on publish (background thread)
- **Bing SEO optimization:** IndexNow setup, unique meta descriptions on all pages, intentional noindex on member-only pages, Bing Search Queries widget in admin Marketing tab
- **Microsoft Clarity:** Added to `base.html` `<head>` (project `wp0905qqxv`)
- **Admin member view:** Emergency contacts now visible in view mode (not just edit mode); phone numbers formatted with `fmtPhone()`
- **Contact messages:** Phone numbers formatted with `fmtPhone()` in the contact messages list
- **Book page title:** Fixed hardcoded short title to use `page_title` variable for proper Bing SEO
