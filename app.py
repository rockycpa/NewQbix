# -*- coding: utf-8 -*-
"""
Qbix Centre — Complete Web Application
Runs on Railway. Serves both the public website and the management app.
Data stored in PostgreSQL on Railway.
v2 — office detail pages, agreement status, contrast improvements
"""

import json
import os
import secrets
import threading
import time
import pyotp
from datetime import datetime, timedelta, date as datetime_date
from functools import wraps
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor
import tempfile

from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, session, flash, send_file, abort)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv

try:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.api
    _cloudinary_available = True
except ImportError:
    _cloudinary_available = False

load_dotenv()

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
BACKUP_DIR = BASE_DIR / 'backups'

# ── Config from environment ───────────────────────────────────────────────────
# ADMIN_USERNAME / ADMIN_PASSWORD_HASH are no longer used — admin auth is now
# phone + SMS 2FA against records in DB.users (managed in the Admin tab).
# ADMIN_PHONE is only consulted on first deploy to bootstrap the initial user
# record (see load_data()).
ADMIN_EMAIL         = os.environ.get('ADMIN_EMAIL', 'qbixcentre@outlook.com')
ADMIN_PHONE         = os.environ.get('ADMIN_PHONE', '4787379107')
APP_URL             = os.environ.get('APP_URL', 'http://localhost:5000')
ANTHROPIC_API_KEY   = os.environ.get('ANTHROPIC_API_KEY', '')
GA_MEASUREMENT_ID   = os.environ.get('GA_MEASUREMENT_ID', '')

# Twilio SMS config
TWILIO_ACCOUNT_SID  = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN   = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '')

# TOTP secret for 2FA (generated once and stored in env)
TOTP_SECRET = os.environ.get('TOTP_SECRET', pyotp.random_base32())

# ── Cloudinary config ────────────────────────────────────────────────────────
if _cloudinary_available:
    cloudinary.config(
        cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
        api_key    = os.environ.get('CLOUDINARY_API_KEY', ''),
        api_secret = os.environ.get('CLOUDINARY_API_SECRET', ''),
        secure     = True
    )

# Serializer for signed tokens (onboarding links, booking tokens)
serializer = URLSafeTimedSerializer(app.secret_key)

# Booking tokens, 2FA codes, and onboarding tokens all used to live in
# module-level dicts. That broke under multiple gunicorn workers / dyno
# restarts (a token issued on worker A would 401 on worker B). Everything
# is now persisted in the JSONB blob via the helpers below.

def _bt_get(data, token):
    """Return a booking-token entry (with `expires` as datetime) if it exists
    and hasn't expired; None otherwise. Reads from data['_bookingTokens']."""
    if not token: return None
    rec = (data.get('_bookingTokens') or {}).get(token)
    if not rec: return None
    try:
        expires = datetime.fromisoformat(str(rec.get('expires','')))
    except (ValueError, TypeError):
        return None
    if datetime.now() > expires:
        return None
    out = dict(rec)
    out['expires'] = expires
    return out

def _bt_set(data, token, entry):
    """Stamp a booking-token entry into data. Caller must save_data(data).
    Also lazily prunes expired tokens so the blob doesn't grow forever."""
    store = data.setdefault('_bookingTokens', {})
    rec = dict(entry)
    exp = rec.get('expires')
    if isinstance(exp, datetime):
        rec['expires'] = exp.isoformat()
    store[token] = rec
    _bt_prune(data)

def _bt_del(data, token):
    """Remove a booking token. Caller must save_data(data)."""
    if not token: return
    store = data.get('_bookingTokens') or {}
    store.pop(token, None)

def _bt_prune(data):
    """Drop expired booking tokens from data['_bookingTokens'] in place."""
    store = data.get('_bookingTokens') or {}
    if not store: return
    now_iso = datetime.now().isoformat()
    for k in [k for k, v in list(store.items()) if str(v.get('expires','')) < now_iso]:
        store.pop(k, None)

def _p2fa_get(data, sid):
    """Return a pending-2FA entry (with `expires` as datetime) or None."""
    if not sid: return None
    rec = (data.get('_pending2fa') or {}).get(sid)
    if not rec: return None
    try:
        expires = datetime.fromisoformat(str(rec.get('expires','')))
    except (ValueError, TypeError):
        return None
    if datetime.now() > expires:
        return None
    out = dict(rec)
    out['expires'] = expires
    return out

def _p2fa_set(data, sid, entry):
    """Stamp a pending-2FA entry into data. Caller must save_data(data)."""
    store = data.setdefault('_pending2fa', {})
    rec = dict(entry)
    exp = rec.get('expires')
    if isinstance(exp, datetime):
        rec['expires'] = exp.isoformat()
    store[sid] = rec
    _p2fa_prune(data)

def _p2fa_del(data, sid):
    """Remove a pending-2FA entry. Caller must save_data(data)."""
    if not sid: return
    store = data.get('_pending2fa') or {}
    store.pop(sid, None)

def _p2fa_prune(data):
    """Drop expired pending-2FA entries from data['_pending2fa'] in place."""
    store = data.get('_pending2fa') or {}
    if not store: return
    now_iso = datetime.now().isoformat()
    for k in [k for k, v in list(store.items()) if str(v.get('expires','')) < now_iso]:
        store.pop(k, None)

def _ot_get(data, token):
    """Return an onboarding-token entry (with `expires` as datetime) or None."""
    if not token: return None
    rec = (data.get('_onboardTokens') or {}).get(token)
    if not rec: return None
    try:
        expires = datetime.fromisoformat(str(rec.get('expires','')))
    except (ValueError, TypeError):
        return None
    if datetime.now() > expires:
        return None
    out = dict(rec)
    out['expires'] = expires
    return out

def _ot_set(data, token, entry):
    """Stamp an onboarding-token entry into data. Caller must save_data(data)."""
    store = data.setdefault('_onboardTokens', {})
    rec = dict(entry)
    exp = rec.get('expires')
    if isinstance(exp, datetime):
        rec['expires'] = exp.isoformat()
    store[token] = rec
    _ot_prune(data)

def _ot_del(data, token):
    """Remove an onboarding token. Caller must save_data(data)."""
    if not token: return
    store = data.get('_onboardTokens') or {}
    store.pop(token, None)

def _ot_prune(data):
    """Drop expired onboarding tokens from data['_onboardTokens'] in place."""
    store = data.get('_onboardTokens') or {}
    if not store: return
    now_iso = datetime.now().isoformat()
    for k in [k for k, v in list(store.items()) if str(v.get('expires','')) < now_iso]:
        store.pop(k, None)

# ── Email sending ─────────────────────────────────────────────────────────────
# Email delivery from the app has been disabled. All outbound email is now
# handled manually through the Notify section (which opens the admin's email
# client with recipients pre-populated). This stub keeps call sites intact so
# the app does not crash if an old code path references it. Returns False so
# any caller that counts successes will see zero sent.
def send_email(to_email, to_name, subject, body_html):
    app.logger.info(
        'send_email stub invoked; app-side email is disabled. '
        'to=%s subject=%r', to_email, subject
    )
    return False

# ── Default data ──────────────────────────────────────────────────────────────
DEFAULT_DATA = {
    "offices": [
        {"id":"o1","num":"11","status":"Occupied","member":"HighBar Accounting","tenantStart":"9/1/2025","sqft":103,"dormer":None,"listDues":None},
        {"id":"o2","num":"12","status":"Occupied","member":"Pinnacle Accounting","tenantStart":"9/1/2025","sqft":139,"dormer":None,"listDues":None},
        {"id":"o3","num":"13","status":"Occupied","member":"Retail 1","tenantStart":"12/11/2018","sqft":196,"dormer":None,"listDues":None},
        {"id":"o4","num":"14","status":"Occupied","member":"Pinnacle Accounting","tenantStart":"9/1/2025","sqft":147,"dormer":None,"listDues":None},
        {"id":"o5","num":"15","status":"Occupied","member":"Pinnacle Accounting","tenantStart":"9/1/2025","sqft":150,"dormer":None,"listDues":None},
        {"id":"o6","num":"16","status":"Occupied","member":"Biren Patel Engineering","tenantStart":"6/8/2019","sqft":245,"dormer":None,"listDues":None},
        {"id":"o7","num":"17","status":"Occupied","member":"Biren Patel Engineering","tenantStart":"6/8/2019","sqft":196,"dormer":None,"listDues":None},
        {"id":"o8","num":"18","status":"Occupied","member":"Pettis Group","tenantStart":"12/1/2022","sqft":209,"dormer":None,"listDues":None},
        {"id":"o9","num":"19","status":"Occupied","member":"HighBar Accounting","tenantStart":"9/1/2025","sqft":176,"dormer":None,"listDues":None},
        {"id":"o10","num":"19A","status":"Occupied","member":"Preferred Provider Network","tenantStart":"9/2/2025","sqft":140,"dormer":None,"listDues":None},
        {"id":"o11","num":"19B","status":"Vacant","member":"","tenantStart":"","sqft":140,"dormer":None,"listDues":725},
        {"id":"o12","num":"21","status":"Occupied","member":"Gilbert Gomez CPA","tenantStart":"9/2/2025","sqft":90,"dormer":31,"listDues":None},
        {"id":"o13","num":"22","status":"Occupied","member":"McLendon Law","tenantStart":"10/1/2021","sqft":90,"dormer":31,"listDues":None},
        {"id":"o14","num":"23","status":"Occupied","member":"NAG Enterprise Group, LLC","tenantStart":"6/1/2025","sqft":99,"dormer":None,"listDues":None},
        {"id":"o15","num":"24","status":"Occupied","member":"HTNB Corp","tenantStart":"5/10/2025","sqft":90,"dormer":31,"listDues":None},
        {"id":"o16","num":"25","status":"Occupied","member":"McLendon Law","tenantStart":"10/1/2021","sqft":87,"dormer":None,"listDues":None},
        {"id":"o17","num":"26","status":"Vacant","member":"","tenantStart":"","sqft":87,"dormer":None,"listDues":500},
        {"id":"o18","num":"27","status":"Occupied","member":"Care Forth","tenantStart":"5/1/2021","sqft":87,"dormer":None,"listDues":None},
        {"id":"o19","num":"28","status":"Occupied","member":"Joshua David Nicholson","tenantStart":"10/1/2022","sqft":87,"dormer":None,"listDues":None},
        {"id":"o20","num":"29A","status":"Occupied","member":"Larry Fouche","tenantStart":"10/1/2025","sqft":142,"dormer":None,"listDues":None},
        {"id":"o21","num":"29B","status":"Vacant","member":"","tenantStart":"","sqft":158,"dormer":None,"listDues":725},
        {"id":"o22","num":"31","status":"Occupied","member":"Preferred Provider Network","tenantStart":"9/2/2025","sqft":139,"dormer":None,"listDues":None},
        {"id":"o23","num":"32","status":"Occupied","member":"National Youth Advocate Program","tenantStart":"5/1/2025","sqft":144,"dormer":None,"listDues":None},
        {"id":"o24","num":"33","status":"Occupied","member":"Wilson PC","tenantStart":"7/1/2023","sqft":161,"dormer":None,"listDues":None},
        {"id":"o25","num":"34","status":"Occupied","member":"Ram Bay","tenantStart":"10/1/2024","sqft":189,"dormer":None,"listDues":None},
        {"id":"o26","num":"35","status":"Occupied","member":"Rid A Critter","tenantStart":"2/1/2026","sqft":81,"dormer":None,"listDues":None},
        {"id":"o27","num":"36","status":"Occupied","member":"Ram Bay","tenantStart":"10/1/2024","sqft":136,"dormer":None,"listDues":None},
    ],
    "members": [],
    "occupants": [],
    "waitlist": [],
    "bookings": [],
    "templates": [
        {"id":"t1","name":"Power Outage","subject":"Power Outage Notice — Qbix Centre","body":"Dear {name},\n\nPlease be advised that Qbix Centre is currently experiencing a power outage. We are working to restore power as quickly as possible.\n\nWe apologize for any inconvenience and will keep you updated.\n\nThank you for your patience,\nQbix Centre Management"},
        {"id":"t2","name":"Monthly Dues Reminder","subject":"Monthly Dues Reminder — Qbix Centre","body":"Dear {name},\n\nThis is a friendly reminder that your monthly dues of {dues} are due. Please arrange payment at your earliest convenience.\n\nThank you,\nQbix Centre Management"},
        {"id":"t3","name":"Building Maintenance","subject":"Planned Maintenance Notice — Qbix Centre","body":"Dear {name},\n\nWe wanted to notify you that Qbix Centre will be undergoing scheduled maintenance. During this time, some services may be temporarily unavailable.\n\nBest regards,\nQbix Centre Management"},
        {"id":"t4","name":"General Notice","subject":"Important Notice from Qbix Centre","body":"Dear {name},\n\n[Your message here]\n\nBest regards,\nQbix Centre Management"},
    ],
    "lastBackup": "",
    "newsletter": [],
    "leadSources": [
        "Drive-by / Signage",
        "Facebook",
        "Nextdoor",
        "Referral from Member",
        "Google Search",
        "Website",
        "Other"
    ],
    # Admin users — login by phone + SMS 2FA. Bootstrap on first deploy from
    # the ADMIN_PHONE env var (see load_data()). All users are full-rights
    # admins; there is no booking-only tier (occupants book on the public
    # /book flow with their own phone-based auth).
    "users": [],
}


# ── Database helpers ─────────────────────────────────────────────────────────
_data_lock = threading.Lock()
DATABASE_URL = os.environ.get('DATABASE_URL', '')
# Railway uses postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

def get_conn():
    """Get a PostgreSQL connection."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Create the data table if it doesn't exist and seed with DEFAULT_DATA."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qbix_store (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # Analytics table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qbix_pageviews (
                    id SERIAL PRIMARY KEY,
                    path VARCHAR(255) NOT NULL,
                    visited_at TIMESTAMP DEFAULT NOW(),
                    referrer VARCHAR(500),
                    user_agent VARCHAR(500)
                )
            """)
            # Check if data exists
            cur.execute("SELECT COUNT(*) FROM qbix_store WHERE id = 1")
            count = cur.fetchone()[0]
            if count == 0:
                # Seed with default data
                cur.execute(
                    "INSERT INTO qbix_store (id, data) VALUES (1, %s)",
                    (json.dumps(DEFAULT_DATA),)
                )
        conn.commit()
    print("[DB] Database initialized")

def load_data():
    """Load data from PostgreSQL."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM qbix_store WHERE id = 1")
                row = cur.fetchone()
                if row:
                    d = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                else:
                    d = json.loads(json.dumps(DEFAULT_DATA))
        # Migrate: ensure new fields exist
        d.setdefault('bookings', [])
        # Backfill resource fields on bookings created before multi-resource
        # support — every legacy booking is the conference room.
        # Also zero-pad start/end times: legacy records used "7:00" but the
        # new 15-min picker emits "07:00", and conflict checks compare strings
        # — mismatched padding sorts wrong (e.g. "08:00" < "7:00").
        for b in d.get('bookings', []):
            b.setdefault('resourceType', 'conference_room')
            b.setdefault('resourceId',   'conference_room')
            for k in ('start', 'end'):
                v = b.get(k, '')
                if v and ':' in v and len(v.split(':')[0]) == 1:
                    b[k] = '0' + v
        d.setdefault('newsletter', [])
        d.setdefault('leadSources', [
            'Drive-by / Signage', 'Facebook', 'Nextdoor',
            'Referral from Member', 'Google Search', 'Website', 'Other'
        ])
        for m in d.get('members', []):
            m.setdefault('attachments', [])
            m.setdefault('discount', 0)
            m.setdefault('agreementSent', '')
            m.setdefault('agreementSigned', '')
            # Migrate agreementStatus — infer from existing fields
            if 'agreementStatus' not in m:
                if m.get('attachments'):
                    m['agreementStatus'] = 'Received'
                elif m.get('agreementSigned'):
                    m['agreementStatus'] = 'Received'
                elif m.get('agreementSent'):
                    m['agreementStatus'] = 'Sent'
                else:
                    m['agreementStatus'] = 'Pending'
        for o in d.get('offices', []):
            o.setdefault('confHours', 6)
        for p in d.get('occupants', []):
            p.setdefault('dlAttachment', None)
            p.setdefault('birthday', '')   # ISO YYYY-MM-DD, empty if unknown
        # Admin users — bootstrap the first record from ADMIN_PHONE on first
        # deploy so we don't lock anyone out. Subsequent users (backup phone,
        # second admin) are added through the Admin tab → Users panel.
        # Exactly one user holds isPrimary=True at any time. The primary admin
        # cannot be deleted or deactivated through the UI; the role must be
        # transferred to another Active user first ("Make Primary" button).
        d.setdefault('users', [])
        if not d['users'] and ADMIN_PHONE:
            d['users'].append({
                'id':        '_u' + secrets.token_hex(6),
                'name':      'Rocky Davidson',
                'phone':     ADMIN_PHONE,
                'status':    'Active',
                'isPrimary': True,
                'dateAdded': datetime.now().strftime('%Y-%m-%d'),
            })
        # Backfill isPrimary on existing user records and ensure exactly one
        # primary. If none yet, promote the user matching ADMIN_PHONE; failing
        # that, the first Active user; failing that, the first user.
        for u in d['users']:
            u.setdefault('isPrimary', False)
        if d['users'] and not any(u.get('isPrimary') for u in d['users']):
            admin_norm = ''.join(filter(str.isdigit, ADMIN_PHONE or ''))[-10:]
            promote = (
                next((u for u in d['users']
                      if ''.join(filter(str.isdigit, u.get('phone',''))).endswith(admin_norm)
                      and u.get('status') == 'Active'), None)
                or next((u for u in d['users'] if u.get('status') == 'Active'), None)
                or d['users'][0]
            )
            promote['isPrimary'] = True
        # Migrate: marketingSettings
        ms = d.setdefault('marketingSettings', {})
        ms.setdefault('gbpHealth', {
            'lastPosted': '', 'lastReplied': '',
            'reviewCount': 8, 'rating': 4.5
        })
        ms.setdefault('marketingAlerts', [])
        ms.setdefault('facebookTracker', {'history': []})
        ms.setdefault('nextdoorTracker', {'history': []})
        ms.setdefault('seoKeywords', [
            'north Macon office space', 'Northside Crossing',
            'private office Macon GA', 'flexible office membership',
            'coworking Macon', 'north Macon coworking', 'furnished office Macon'
        ])
        ms.setdefault('newsletterCategories', [
            'Monthly Update', 'Member Spotlight', 'Community', 'Availability'
        ])
        # Booking settings — admin-editable in phase 5. Defaults set here so the
        # overage gate has values to read against on the very first deploy.
        bs = d.setdefault('bookingSettings', {})
        bs.setdefault('overageRatePerHour', 25)
        bs.setdefault('overageWarningMessage',
            'This booking will put you over your included hours for the month. '
            'By confirming, you agree to incur additional charges at the rate above.')
        # SMS templates — admin-editable in the Booking Settings panel.
        # Variables: {space} {date} {start} {end} {member}
        bs.setdefault('smsConfirmationTemplate',
            'Qbix Centre: {space} booking confirmed for {date} {start}-{end}. '
            '500A Northside Crossing, Macon GA. See you then!')
        bs.setdefault('smsReminderTemplate',
            'Qbix Centre reminder: You have the {space} tomorrow {date} at {start}. '
            '500A Northside Crossing, Macon GA.')
        bs.setdefault('smsEditTemplate',
            'Qbix Centre: Your booking has been updated. {space} on {date} from {start}-{end}.')
        bs.setdefault('smsCancelTemplate',
            'Qbix Centre: Your booking has been cancelled. {space} on {date} {start}-{end}.')
        # Disclosure shown on the /book flow before a member confirms.
        bs.setdefault('optInDisclosure',
            'By booking, you agree to receive SMS text messages from Qbix Centre about '
            'your booking (confirmation, 24-hour reminder, edits, and cancellations). '
            'Message and data rates may apply. Reply STOP to opt out, HELP for help.')
        # SMS Messaging section of /privacy. Stored as raw HTML so the admin can
        # tweak wording (carrier-required STOP/HELP language stays in the default).
        bs.setdefault('privacySmsHtml',
            '<p>By providing your mobile phone number on our website booking or login '
            'forms, you consent to receive SMS text messages from Qbix Centre for the '
            'purposes of account verification and membership-related communications. '
            'Consent is collected via our website forms, where you are informed of SMS '
            'messaging before submitting your phone number. No messages are sent prior '
            'to that submission.</p>'
            '<p>Message frequency: 1&ndash;3 messages per booking session (verification '
            'code plus booking confirmation). Message and data rates may apply. You may '
            'opt out at any time by replying STOP to any message. Reply HELP for '
            'assistance. You will not receive further messages after opting out.</p>')
        return d
    except Exception as e:
        print(f"[DB ERROR] load_data: {e}")
        return json.loads(json.dumps(DEFAULT_DATA))

def save_data(data):
    """Save data to PostgreSQL."""
    with _data_lock:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE qbix_store SET data = %s, updated_at = NOW() WHERE id = 1",
                        (json.dumps(data),)
                    )
                conn.commit()
        except Exception as e:
            print(f"[DB ERROR] save_data: {e}")

def get_db():
    return load_data()

def track_pageview(path):
    """Record a page visit to the analytics table."""
    try:
        # Skip admin, static, health routes and bots
        skip = ['/admin', '/static', '/health', '/favicon', '/book/slots',
                '/book/my-bookings', '/book/verify', '/book/create',
                '/book/cancel', '/book/request-code']
        if any(path.startswith(s) for s in skip):
            return
        referrer = request.referrer or ''
        ua = request.user_agent.string or ''
        # Skip obvious bots
        bot_keywords = ['bot', 'crawler', 'spider', 'slurp', 'bingpreview', 'facebookexternalhit']
        if any(k in ua.lower() for k in bot_keywords):
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO qbix_pageviews (path, referrer, user_agent) VALUES (%s, %s, %s)",
                    (path[:255], referrer[:500], ua[:500])
                )
            conn.commit()
    except Exception as e:
        print(f"[ANALYTICS] track error: {e}")

# Initialize database on startup
if DATABASE_URL:
    try:
        init_db()
    except Exception as e:
        print(f"[DB] Init failed: {e}")
else:
    print("[DB] No DATABASE_URL set — running without database")

def net_dues(member):
    return max(0, (member.get('dues') or 0) - (member.get('discount') or 0))

def offices_for(data, name):
    return [o['num'] for o in data['offices'] if o.get('member') == name]

def hours_included(data, member_name):
    """Sum confHours across all offices held by this member (default 6 per office)."""
    return sum(int(o.get('confHours') or 6) for o in data['offices'] if o.get('member') == member_name)


# ── Booking resource helpers ──────────────────────────────────────────────────
# A "resource" is anything members can book. Today: the conference room (always)
# plus any office whose status == 'Vacant'. Each booking carries resourceId so
# we can scope availability and conflict checks per resource.
CONFERENCE_ROOM_ID    = 'conference_room'
CONFERENCE_ROOM_LABEL = 'Conference Room'

def get_bookable_resources(data):
    """Return list of bookable resources: conference room + vacant offices.
    Each item: {id, type, label}. Used by the calendar resource picker and to
    validate resourceId on booking create/edit."""
    resources = [{
        'id':    CONFERENCE_ROOM_ID,
        'type':  'conference_room',
        'label': CONFERENCE_ROOM_LABEL,
    }]
    for o in data.get('offices', []):
        if o.get('status') == 'Vacant':
            resources.append({
                'id':    o['id'],
                'type':  'office',
                'label': f"Office {o.get('num', '')}".strip(),
            })
    return resources

def resource_label(data, resource_id):
    """Look up the human-readable label for a resourceId (for SMS templates,
    confirmation messages, admin views)."""
    if resource_id == CONFERENCE_ROOM_ID:
        return CONFERENCE_ROOM_LABEL
    for o in data.get('offices', []):
        if o.get('id') == resource_id:
            return f"Office {o.get('num', '')}".strip()
    return resource_id  # fallback — shouldn't happen for valid IDs

def _booking_duration_hours(b):
    """Compute booking duration in hours from start/end HH:MM strings.
    Returns 0 on parse error so a malformed record can't blow up totals."""
    try:
        sh, sm = (int(x) for x in b.get('start', '0:0').split(':'))
        eh, em = (int(x) for x in b.get('end',   '0:0').split(':'))
        minutes = (eh * 60 + em) - (sh * 60 + sm)
        return max(0, minutes) / 60.0
    except (ValueError, AttributeError):
        return 0.0

def _booking_billed_to(b):
    """Resolve which member account a booking's hours roll up to. Bookings
    made by occupants stamp `parentMember` (the company / member name); old
    bookings without that field fall back to `memberName` so their hours
    don't get lost when accounting changed."""
    return (b.get('parentMember') or b.get('memberName') or '')

def get_member_hours_used(data, member_name, year, month):
    """Sum confirmed booking hours billed to this member in the given month,
    across all resources (conference room + offices both draw from the same
    bucket). Occupants' bookings under this member roll up here via the
    parentMember field. Returns a float (e.g. 4.25 for 4h 15m)."""
    total = 0.0
    for b in data.get('bookings', []):
        if (_booking_billed_to(b) == member_name
                and b.get('year')   == year
                and b.get('month')  == month
                and b.get('status') != 'cancelled'):
            total += _booking_duration_hours(b)
    return total


# ── SMS helper (Twilio) ───────────────────────────────────────────────────────
def send_sms(to_phone, message):
    """Send SMS via Twilio. Phone number should be 10 digits, e.g. 4787379107.
    Empty messages are silently skipped — admin can blank an SMS template in
    the Booking Settings panel to disable that message entirely."""
    if not message or not str(message).strip():
        return False
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        print(f"[SMS] Twilio not configured — would send to {to_phone}: {message}")
        return False

    # Normalize phone to E.164 format
    digits = ''.join(filter(str.isdigit, str(to_phone)))
    if len(digits) == 10:
        to_e164 = f'+1{digits}'
    elif len(digits) == 11 and digits.startswith('1'):
        to_e164 = f'+{digits}'
    else:
        to_e164 = f'+{digits}'

    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=to_e164
        )
        print(f"[SMS] Sent to {to_e164}")
        return True
    except Exception as e:
        print(f"[SMS ERROR] {e}")
        return False


# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_authenticated'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def generate_code():
    return str(secrets.randbelow(900000) + 100000)  # 6-digit


# ── Template context ─────────────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    return {'now': datetime.now()}


# ── Health check ──────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'ok': True, 'time': datetime.now().isoformat()})


# ══════════════════════════════════════════════════════════════════════════════
# SEO HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_site_settings(page_key):
    """Return page_title, page_desc, page_h1 for a public page, falling back to defaults."""
    defaults = {
        'home': {
            'title': 'Private Office & Coworking Space in North Macon, GA | Qbix Centre',
            'description': 'Furnished private offices and coworking space at Northside Crossing in north Macon, GA. 24/7 access, AT&T Gigabit fiber, Starbucks coffee, conference room included. No long-term lease.',
            'h1': 'Your Professional Home in North Macon, Georgia',
        },
        'offices': {
            'title': 'Private Office Space for Rent in North Macon, GA | Qbix Centre',
            'description': "Browse available furnished offices at Qbix Centre — north Macon's flexible office membership community. Private, lockable offices from $575/mo. Schedule a tour today.",
            'h1': 'Office Space in North Macon, GA',
        },
        'amenities': {
            'title': 'Office Amenities & Included Features | Qbix Centre Macon GA',
            'description': 'Every furnished office at Qbix Centre includes AT&T Gigabit fiber, Starbucks coffee, conference room access, 24/7 entry, free parking, and janitorial service — all included.',
            'h1': 'Everything Included with Your Qbix Membership',
        },
        'news': {
            'title': 'News & Updates from Qbix Centre | North Macon Coworking',
            'description': "Stay up to date with Qbix Centre — announcements, member spotlights, and community news from north Macon's premier coworking and private office community.",
            'h1': 'Qbix Centre News & Updates',
        },
        'contact': {
            'title': 'Contact Qbix Centre | Schedule a Tour | North Macon Office Space',
            'description': 'Ready to see Qbix Centre in person? Schedule a tour at 500A Northside Crossing, Macon GA 31210. Call (478) 216-2876 or send a message — we respond within one business day.',
            'h1': 'Get in Touch with Qbix Centre',
        },
    }
    db = get_db()
    saved = db.get('siteSettings', {}).get(page_key, {})
    d = defaults.get(page_key, {})
    return {
        'page_title': saved.get('title') or d.get('title', ''),
        'page_desc':  saved.get('description') or d.get('description', ''),
        'page_h1':    saved.get('h1') or d.get('h1', ''),
    }



# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED POST PUBLISHER
# Runs in a background thread every 60 seconds.
# Any post with scheduledFor <= now and draft=True gets published automatically.
# ══════════════════════════════════════════════════════════════════════════════

def _publish_scheduled_posts():
    """Check for scheduled posts whose publish time has arrived and publish them."""
    while True:
        try:
            now_iso = datetime.now().isoformat()
            data = get_db()
            changed = False
            for post in data.get('newsletter', []):
                scheduled = post.get('scheduledFor')
                if scheduled and post.get('draft') and scheduled <= now_iso:
                    post['draft'] = False
                    post['date'] = scheduled  # use scheduled time as publish date
                    post.pop('scheduledFor', None)
                    changed = True
            if changed:
                save_data(data)
        except Exception:
            pass  # never crash the thread
        time.sleep(60)

# Start the scheduler thread once (not in debug reloader child process)
if not os.environ.get('WERKZEUG_RUN_MAIN'):
    _sched_thread = threading.Thread(target=_publish_scheduled_posts, daemon=True)
    _sched_thread.start()

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC WEBSITE ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/robots.txt')
def robots_txt():
    lines = [
        'User-agent: *',
        'Allow: /',
        'Disallow: /admin',
        'Disallow: /admin/',
        '',
        f'Sitemap: {APP_URL}/sitemap.xml',
    ]
    return '\n'.join(lines), 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def sitemap():
    data = get_db()
    today = datetime_date.today().isoformat()

    # Static pages
    urls = [
        {'loc': APP_URL + '/',        'priority': '1.0', 'changefreq': 'weekly',  'lastmod': today},
        {'loc': APP_URL + '/news',    'priority': '0.7', 'changefreq': 'weekly',  'lastmod': today},
        {'loc': APP_URL + '/contact', 'priority': '0.8', 'changefreq': 'monthly', 'lastmod': today},
        {'loc': APP_URL + '/book',    'priority': '0.6', 'changefreq': 'monthly', 'lastmod': today},
    ]

    # Vacant office detail pages
    for o in data.get('offices', []):
        if o.get('status') == 'Vacant':
            urls.append({
                'loc': APP_URL + f'/offices/{o["id"]}',
                'priority': '0.9',
                'changefreq': 'weekly',
                'lastmod': today,
            })

    # Published news posts
    for p in data.get('newsletter', []):
        if not p.get('draft') and p.get('id'):
            lastmod = p.get('date', today)[:10] if p.get('date') else today
            urls.append({
                'loc': APP_URL + f'/news/{p["id"]}',
                'priority': '0.6',
                'changefreq': 'never',
                'lastmod': lastmod,
            })

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for u in urls:
        xml.append('  <url>')
        xml.append(f'    <loc>{u["loc"]}</loc>')
        xml.append(f'    <lastmod>{u["lastmod"]}</lastmod>')
        xml.append(f'    <changefreq>{u["changefreq"]}</changefreq>')
        xml.append(f'    <priority>{u["priority"]}</priority>')
        xml.append('  </url>')
    xml.append('</urlset>')
    return '\n'.join(xml), 200, {'Content-Type': 'application/xml'}

@app.route('/')
def home():
    track_pageview('/')
    data = get_db()
    vacant        = [o for o in data['offices'] if o['status'] == 'Vacant']
    home_gallery  = data.get('homeGallery', [])
    site_amenities = data.get('siteAmenities') or [
        'Fully Furnished Offices',
        '24/7 Access',
        'Wired & Wireless Internet',
        'Sit/Stand Ergonomic Desks',
        '6 Hours Conference Room/Month per Office',
        'Unlimited Water & Coffee',
        'Free Document Scanning & Printing',
        'Free Janitorial Services & Document Destruction',
        'Mail Handling',
        'Free Parking',
    ]
    posts = sorted(
        [p for p in data.get('posts', []) if not p.get('draft', False)],
        key=lambda p: p.get('date', ''), reverse=True
    )[:3]
    # OG image — use first home gallery photo, else first vacant office photo
    og_image = ''
    if home_gallery:
        og_image = home_gallery[0].get('url','')
    elif vacant and vacant[0].get('photos'):
        og_image = vacant[0]['photos'][0].get('url','')
    return render_template('public/home.html',
        **get_site_settings('home'),
        vacant=vacant, site_amenities=site_amenities,
        home_gallery=home_gallery, posts=posts,
        canonical_url=APP_URL+'/',
        og_image=og_image,
        ga_id=GA_MEASUREMENT_ID,
        page_content=data.get('pageContent', {}),
        attraction_photos=data.get('attractionPhotos', []),
        attraction_tiles=data.get('attractionTiles', []))

@app.route('/offices')
def offices_page():
    return redirect('/#offices')

@app.route('/offices/<office_id>')
def office_detail(office_id):
    track_pageview(f'/offices/{office_id}')
    data = get_db()
    office = next((o for o in data['offices'] if o['id'] == office_id), None)
    if not office:
        abort(404)
    # Only show vacant offices publicly; redirect occupied ones back to offices list
    if office.get('status') != 'Vacant':
        return redirect('/#offices')
    # Get next/prev vacant offices for navigation
    vacant = [o for o in data['offices'] if o['status'] == 'Vacant']
    idx = next((i for i, o in enumerate(vacant) if o['id'] == office_id), 0)
    prev_office = vacant[idx - 1] if idx > 0 else None
    next_office = vacant[idx + 1] if idx + 1 < len(vacant) else None
    og_image = office['photos'][0]['url'] if office.get('photos') else ''
    og_title = f'Office {office["num"]} — {office.get("sqft","")} sq ft | Qbix Centre Macon GA'
    og_desc  = office.get('description') or f'Private furnished office {office["num"]} at Qbix Centre, north Macon GA. {office.get("sqft","")} sq ft, ${office.get("listDues","")}/mo.'
    return render_template('public/office_detail.html', office=office,
                           prev_office=prev_office, next_office=next_office,
                           canonical_url=APP_URL+f'/offices/{office_id}',
                           og_image=og_image, og_title=og_title, og_desc=og_desc,
                           ga_id=GA_MEASUREMENT_ID)

@app.route('/amenities')
def amenities():
    return redirect('/#amenities')

@app.route('/contact')
def contact():
    track_pageview('/contact')
    return render_template('public/contact.html', **get_site_settings('contact'),
                           canonical_url=APP_URL+'/contact', ga_id=GA_MEASUREMENT_ID)

@app.route('/privacy')
def privacy():
    track_pageview('/privacy')
    data = get_db()
    privacy_sms_html = data.get('bookingSettings', {}).get('privacySmsHtml', '')
    return render_template('public/privacy.html',
                           privacy_sms_html=privacy_sms_html,
                           ga_id=GA_MEASUREMENT_ID)

@app.route('/sms-optin')
def sms_optin():
    track_pageview('/sms-optin')
    return render_template('public/sms_optin.html', ga_id=GA_MEASUREMENT_ID)

@app.route('/contact', methods=['POST'])
def contact_submit():
    name    = request.form.get('name', '')
    email   = request.form.get('email', '')
    phone   = request.form.get('phone', '')
    subject = request.form.get('subject', '')
    message = request.form.get('message', '')

    # Store message in database
    data = get_db()
    data.setdefault('contact_messages', [])
    import secrets as _sec
    msg_entry = {
        'id': '_' + _sec.token_hex(4),
        'name': name,
        'email': email,
        'phone': phone,
        'subject': subject,
        'message': message,
        'date': datetime.now().isoformat(),
        'read': False,
    }
    data['contact_messages'].insert(0, msg_entry)
    save_data(data)

    # SMS notification to admin
    sms_text = f'New Qbix website message from {name}: {message[:100]}'
    send_sms(ADMIN_PHONE, sms_text)

    flash('Thank you! We will be in touch shortly.', 'success')
    return redirect(url_for('contact'))

@app.route('/admin/api/site-settings', methods=['GET', 'POST'])
@login_required
def api_site_settings():
    db = get_db()
    if request.method == 'GET':
        return jsonify(db.get('siteSettings', {}))
    data = request.get_json(force=True)
    db['siteSettings'] = data
    save_data(db)
    return jsonify({'ok': True})

@app.route('/admin/api/home-gallery', methods=['GET', 'POST'])
@login_required
def api_home_gallery():
    db = get_db()
    if request.method == 'GET':
        return jsonify({'ok': True, 'photos': db.get('homeGallery', [])})
    data = request.get_json(force=True)
    db['homeGallery'] = data.get('photos', [])
    save_data(db)
    return jsonify({'ok': True})

@app.route('/admin/api/site-amenities', methods=['GET', 'POST'])
@login_required
def api_site_amenities():
    db = get_db()
    if request.method == 'GET':
        return jsonify({'ok': True, 'amenities': db.get('siteAmenities', [])})
    data = request.get_json(force=True)
    db['siteAmenities'] = data.get('amenities', [])
    save_data(db)
    return jsonify({'ok': True})


@app.route('/admin/api/contact-messages')
@login_required
def get_contact_messages():
    data = get_db()
    return jsonify({'ok': True, 'messages': data.get('contact_messages', [])})

@app.route('/admin/api/contact-messages/<msg_id>/read', methods=['POST'])
@login_required
def mark_message_read(msg_id):
    data = get_db()
    msg = next((m for m in data.get('contact_messages', []) if m['id'] == msg_id), None)
    if msg:
        msg['read'] = True
        save_data(data)
    return jsonify({'ok': True})

@app.route('/admin/api/contact-messages/<msg_id>/delete', methods=['POST'])
@login_required
def delete_contact_message(msg_id):
    data = get_db()
    data['contact_messages'] = [m for m in data.get('contact_messages', []) if m['id'] != msg_id]
    save_data(data)
    return jsonify({'ok': True})

@app.route('/news')
def news():
    track_pageview('/news')
    data = get_db()
    posts = sorted(data.get('newsletter', []), key=lambda x: x.get('date',''), reverse=True)
    # Only show published (not draft) posts
    posts = [p for p in posts if not p.get('draft')]
    cat_filter = request.args.get('category', '')
    categories = sorted(set(p.get('category','') for p in posts if p.get('category')))
    if cat_filter:
        posts = [p for p in posts if p.get('category') == cat_filter]
    return render_template('public/news.html', **get_site_settings('news'),
                           posts=posts, categories=categories,
                           cat_filter=cat_filter,
                           canonical_url=APP_URL+'/news',
                           ga_id=GA_MEASUREMENT_ID)

@app.route('/news/<post_id>')
def news_post(post_id):
    data = get_db()
    posts = sorted(data.get('newsletter', []), key=lambda x: x.get('date',''), reverse=True)
    post = next((p for p in posts if p['id'] == post_id), None)
    if not post:
        abort(404)
    idx = posts.index(post)
    prev_post = posts[idx + 1] if idx + 1 < len(posts) else None
    next_post = posts[idx - 1] if idx > 0 else None
    og_image = post.get('heroPhoto',{}).get('url','') if post.get('heroPhoto') else ''
    og_desc  = post.get('body','')[:160].replace('<','').replace('>','') if post.get('body') else ''
    return render_template('public/news_post.html', post=post,
                           prev_post=prev_post, next_post=next_post,
                           canonical_url=APP_URL+f'/news/{post_id}',
                           og_image=og_image, og_title=post.get('subject',''),
                           og_desc=og_desc,
                           ga_id=GA_MEASUREMENT_ID)


# ══════════════════════════════════════════════════════════════════════════════
# ONBOARDING FLOW (public — for prospective members)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/onboard')
def onboard_home():
    track_pageview('/onboard')
    """Landing page linked from website — general interest form."""
    return render_template('public/onboard_home.html', ga_id=GA_MEASUREMENT_ID)

@app.route('/onboard/<token>')
def onboard(token):
    """Personalized onboarding link sent by admin."""
    data = get_db()
    info = _ot_get(data, token)   # filters expired
    if not info:
        return render_template('public/onboard_expired.html')
    return render_template('public/onboard.html', token=token, info=info, ga_id=GA_MEASUREMENT_ID)

@app.route('/onboard/<token>/submit', methods=['POST'])
def onboard_submit(token):
    data = get_db()
    if not _ot_get(data, token):
        return jsonify({'ok': False, 'error': 'Link expired'}), 400

    form = request.form

    # Create pending member
    member_id = '_' + secrets.token_hex(4)
    member = {
        'id':        member_id,
        'name':      form.get('company') or form.get('firstName') + ' ' + form.get('lastName'),
        'status':    'Pending',
        'start':     form.get('startDate', ''),
        'end':       '',
        'dues':      0,
        'discount':  0,
        'deposit':   0,
        'notes':     form.get('notes', ''),
        'email':     form.get('email', ''),
        'phone':     form.get('phone', ''),
        'address':   form.get('address', ''),
        'city':      form.get('city', ''),
        'state':     form.get('state', ''),
        'zip':       form.get('zip', ''),
        'website':   form.get('website', ''),
        'attachments':      [],
        'agreementSent':    '',
        'agreementSigned':  '',
        'emergencyName':    form.get('emergencyName', ''),
        'emergencyPhone':   form.get('emergencyPhone', ''),
        'emergencyRel':     form.get('emergencyRel', ''),
        'onboardedAt':      datetime.now().isoformat(),
    }
    data['members'].append(member)

    # Create pending occupant
    occ_id = '_' + secrets.token_hex(4)
    occupant = {
        'id':          occ_id,
        'name':        form.get('firstName', '') + ' ' + form.get('lastName', ''),
        'company':     member['name'],
        'phone':       form.get('phone', ''),
        'email':       form.get('email', ''),
        'office':      '',
        'endDate':     '',
        'status':      'Pending',
        'dlAttachment': None,
    }
    data['occupants'].append(occupant)
    save_data(data)

    # Notify admin
    send_email(
        ADMIN_EMAIL, 'Qbix Centre Admin',
        f'New onboarding submission: {member["name"]}',
        f'<p>A new prospect has completed the onboarding form.</p>'
        f'<p><b>Name:</b> {occupant["name"]}<br>'
        f'<b>Company:</b> {member["name"]}<br>'
        f'<b>Email:</b> {member["email"]}<br>'
        f'<b>Phone:</b> {member["phone"]}</p>'
        f'<p>Log in to your Qbix Centre dashboard to review and activate.</p>'
    )

    _ot_del(data, token)
    save_data(data)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
# CONFERENCE ROOM BOOKING (member-facing)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/book')
def book_home():
    data = get_db()
    bs   = data.get('bookingSettings', {})
    return render_template('public/book_home.html',
        opt_in_disclosure=bs.get('optInDisclosure', ''),
        ga_id=GA_MEASUREMENT_ID)

@app.route('/book/request-code', methods=['POST'])
def book_request_code():
    """Phone-number entry point. We look up the phone in OCCUPANTS only
    (members are companies, not booking entities — see 'Occupants vs members'
    in QBIX_HANDOFF.md). If found, we text a 6-digit code, stash a server-side
    pending entry, and return a session id. The client then POSTs that id +
    the code to /book/verify to receive a booking token."""
    raw_phone = request.json.get('phone', '')
    phone     = _normalize_phone(raw_phone)
    if not phone:
        return jsonify({'ok': False, 'error': 'Please enter a valid 10-digit phone number.'})

    data     = get_db()
    occupant = _member_by_phone(data, phone)
    if not occupant:
        # Vague on purpose — don't confirm or deny that a phone is on file.
        return jsonify({'ok': False, 'error':
            'We couldn\'t find that phone number on file. Contact the admin '
            'if you believe this is an error.'})

    # Issue a session id, stash code + occupant identity in the pending2fa
    # store so /book/verify can finalize.
    code = generate_code()
    sid  = secrets.token_urlsafe(16)
    _p2fa_set(data, sid, {
        'code':         code,
        'expires':      datetime.now() + timedelta(minutes=10),
        'purpose':      'book',
        'phone':        phone,
        'email':        occupant.get('_phone_member_email', ''),
        'name':         occupant.get('_phone_member_name', ''),
        'parentMember': occupant.get('_phone_parent_member', ''),
    })
    save_data(data)

    send_sms(phone, f'Qbix Centre booking code: {code}. Expires in 10 minutes.')
    return jsonify({'ok': True, 'sid': sid, 'phoneTail': phone[-4:]})

@app.route('/book/verify', methods=['POST'])
def book_verify():
    # Accept either {sid, code} (new flow) or the legacy {token, code} payload
    # so older client builds during deploy don't 500.
    sid  = request.json.get('sid') or request.json.get('token', '')
    code = (request.json.get('code') or '').strip()

    data  = get_db()
    entry = _p2fa_get(data, sid)   # already filters out expired entries
    if not entry:
        _p2fa_del(data, sid); save_data(data)
        return jsonify({'ok': False, 'error': 'Invalid or expired session.'})
    if entry['code'] != code:
        return jsonify({'ok': False, 'error': 'Incorrect code.'})

    # Issue booking session token. Carry parentMember so /book/slots etc. can
    # roll hours up to the right member account without re-deriving from the
    # occupant on every call.
    booking_token = secrets.token_urlsafe(32)
    _bt_set(data, booking_token, {
        'email':        entry.get('email', ''),
        'name':         entry.get('name', ''),
        'parentMember': entry.get('parentMember', ''),
        'expires':      datetime.now() + timedelta(hours=2),
    })
    _p2fa_del(data, sid)
    save_data(data)
    return jsonify({'ok': True, 'bookingToken': booking_token})

@app.route('/book/calendar')
def book_calendar():
    bt = request.args.get('token', '')
    data = get_db()
    entry = _bt_get(data, bt)   # filters expired
    if not entry:
        return redirect(url_for('book_home'))
    # Sessions are now created by occupant lookup, so the parent member name
    # is stamped on the token. We look up the member record by that name to
    # compute included hours; occupants without a valid parent member can't
    # book (their hours bucket would be undefined).
    parent_name = (entry.get('parentMember') or '').strip()
    member = next((m for m in data['members']
                   if m.get('name') == parent_name), None) if parent_name else None
    if not member:
        return redirect(url_for('book_home'))

    included  = hours_included(data, member['name'])
    resources = get_bookable_resources(data)
    bs        = data.get('bookingSettings', {})
    # Two-month window: this month and next month. The UI caps prev/next nav.
    now      = datetime.now()
    next_dt  = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
    return render_template('public/book_calendar.html',
        token=bt,
        member_name=entry['name'],
        member_email=entry.get('email', ''),
        included_hours=included,
        resources=resources,
        min_year=now.year,   min_month=now.month,
        max_year=next_dt.year, max_month=next_dt.month,
        opt_in_disclosure=bs.get('optInDisclosure', ''),
        ga_id=GA_MEASUREMENT_ID)

@app.route('/book/slots')
def book_slots():
    """Return booked slots for a given month plus the signed-in member's
    per-month hours usage. Optional ?resource_id=... filters slots to a single
    resource (used by phase-2 UI). Hours-used totals span ALL resources since
    conference room and office bookings share one monthly bucket."""
    bt = request.args.get('token', '')
    data  = get_db()
    entry = _bt_get(data, bt)
    if not entry:
        return jsonify({'ok': False}), 401

    year  = int(request.args.get('year',  datetime.now().year))
    month = int(request.args.get('month', datetime.now().month))
    resource_filter = request.args.get('resource_id', '').strip()

    slots = [b for b in data.get('bookings', [])
             if b.get('year') == year and b.get('month') == month
             and b.get('status') != 'cancelled'
             and (not resource_filter or b.get('resourceId') == resource_filter)]

    # Hours/limits live on the parent member account (the company). All
    # occupants share that bucket; the token carries `parentMember` so we
    # don't have to re-derive it from the occupant record on every call.
    parent_member  = (entry.get('parentMember') or '').strip()
    hours_used     = get_member_hours_used(data, parent_member, year, month) if parent_member else 0.0
    hours_inc      = hours_included(data, parent_member) if parent_member else 0
    bs             = data.get('bookingSettings', {})
    overage_rate   = bs.get('overageRatePerHour', 25)

    return jsonify({
        'ok':            True,
        'slots':         slots,
        'hoursUsed':     round(hours_used, 2),
        'hoursIncluded': hours_inc,
        'overageRate':   overage_rate,
    })

@app.route('/book/my-bookings')
def book_my_bookings():
    """Return ALL of the signed-in member's upcoming (today-and-later)
    bookings across every resource in a single round trip. Used by the
    member calendar's "My Upcoming Bookings" panel so it doesn't need to
    pull two months of all-resource data and filter client-side."""
    bt = request.args.get('token', '')
    data  = get_db()
    entry = _bt_get(data, bt)
    if not entry:
        return jsonify({'ok': False}), 401

    member_name  = (entry.get('name', '') or '').strip().lower()
    member_email = (entry.get('email', '') or '').strip().lower()
    parent_member = (entry.get('parentMember', '') or '').strip().lower()
    today_iso  = datetime.now().date().isoformat()

    def _is_mine(b):
        if not b: return False
        bn = (b.get('memberName',  '') or '').strip().lower()
        be = (b.get('memberEmail', '') or '').strip().lower()
        bp = (b.get('parentMember', '') or '').strip().lower()
        # Match by occupant identity first; also match legacy / member-level
        # bookings that share this occupant's parent member account so old
        # admin-created bookings (memberName=company) still show up.
        if parent_member and (bp == parent_member or bn == parent_member): return True
        if member_name  and bn == member_name:  return True
        if member_email and be == member_email: return True
        return False

    mine = [b for b in data.get('bookings', [])
            if b.get('status') != 'cancelled'
            and (b.get('date') or '') >= today_iso
            and _is_mine(b)]
    mine.sort(key=lambda b: (b.get('date',''), b.get('start','')))
    # Cap at a reasonable number for display.
    return jsonify({'ok': True, 'bookings': mine[:20]})

@app.route('/book/create', methods=['POST'])
def book_create():
    bt = request.json.get('token', '')
    data = get_db()
    entry = _bt_get(data, bt)
    if not entry:
        return jsonify({'ok': False, 'error': 'Session expired'}), 401
    # Sessions are issued only to occupants now. The parent member account
    # (occupant.company) is stamped on the token; we look it up here so we
    # can credit hours, enforce limits, and build the confirmation SMS.
    parent_name = (entry.get('parentMember') or '').strip()
    member = next((m for m in data['members']
                   if m.get('name') == parent_name), None) if parent_name else None
    if not member:
        return jsonify({'ok': False, 'error': 'Member account not found'}), 400

    occupant_name  = entry.get('name', '')   # who actually booked

    date_str       = request.json.get('date', '')    # YYYY-MM-DD
    start_time     = request.json.get('start', '')   # HH:MM
    end_time       = request.json.get('end', '')     # HH:MM
    title          = request.json.get('title', 'Meeting')
    # Resource defaults to conference room when caller omits it (preserves
    # phase-1 UI behavior). Validated against the bookable list below.
    resource_id    = (request.json.get('resourceId') or CONFERENCE_ROOM_ID).strip()
    accept_overage = bool(request.json.get('acceptOverage', False))

    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date'}), 400

    # Validate resource exists and is currently bookable.
    bookable = get_bookable_resources(data)
    chosen   = next((r for r in bookable if r['id'] == resource_id), None)
    if not chosen:
        return jsonify({'ok': False, 'error': 'That space is not available for booking.'}), 400

    # Check for conflicts — only against bookings on the SAME resource.
    for b in data.get('bookings', []):
        if (b.get('date') == date_str
                and b.get('status') != 'cancelled'
                and b.get('resourceId') == resource_id
                and not (end_time <= b.get('start','') or start_time >= b.get('end',''))):
            return jsonify({'ok': False, 'error': 'That time slot is already booked.'})

    # Overage gate — month-scoped accounting. Conference room + office hours
    # share one bucket. If this booking pushes the member over their monthly
    # included hours and the caller hasn't accepted overage, return an
    # overage payload so the UI can show the warning modal.
    booking_hours = _booking_duration_hours({'start': start_time, 'end': end_time})
    hours_used    = get_member_hours_used(data, member['name'], dt.year, dt.month)
    hours_inc     = hours_included(data, member['name'])
    bs            = data.get('bookingSettings', {})
    overage_rate  = float(bs.get('overageRatePerHour', 25))
    hours_after   = hours_used + booking_hours
    hours_over    = max(0.0, hours_after - hours_inc)
    overage_charge = round(hours_over * overage_rate, 2)

    if hours_over > 0 and not accept_overage:
        return jsonify({
            'ok':                    False,
            'overage':               True,
            'hoursThisBooking':      round(booking_hours, 2),
            'hoursUsed':             round(hours_used, 2),
            'hoursIncluded':         hours_inc,
            'hoursOver':             round(hours_over, 2),
            'overageRate':           overage_rate,
            'overageCharge':         overage_charge,
            'overageWarningMessage': bs.get('overageWarningMessage', ''),
        })

    booking_id = '_' + secrets.token_hex(4)
    booking = {
        'id':           booking_id,
        # Booker (occupant) is recorded under memberName/memberEmail so the
        # member calendar's "My Upcoming Bookings" filter still works for them.
        'memberName':   occupant_name,
        'memberEmail':  entry.get('email', ''),
        # parentMember = the member account this booking's hours roll up to.
        'parentMember': member['name'],
        'resourceType': chosen['type'],
        'resourceId':   chosen['id'],
        'date':         date_str,
        'year':         dt.year,
        'month':        dt.month,
        'start':        start_time,
        'end':          end_time,
        'title':        title,
        'status':       'confirmed',
        'createdAt':    datetime.now().isoformat(),
    }
    # Stamp overage detail when this booking caused (any part of) the overage,
    # so admin can see what was charged.
    if hours_over > 0:
        booking['overageHours']  = round(hours_over, 2)
        booking['overageRate']   = overage_rate
        booking['overageCharge'] = overage_charge
    data.setdefault('bookings', []).append(booking)
    save_data(data)

    res_label = chosen['label']

    # Confirmation SMS goes to the occupant who actually made the booking,
    # not the parent member. _member_phone falls back across both, but with
    # an occupant email match first this resolves to the occupant's phone.
    sms_phone = _member_phone(data, entry.get('email',''), occupant_name)
    sms_ctx   = _booking_sms_ctx(data, booking, override_label=res_label)
    if sms_phone:
        send_sms(sms_phone, render_sms_template(
            bs.get('smsConfirmationTemplate', ''), sms_ctx))

    # Schedule reminder (24h before) in background thread
    def send_reminder():
        try:
            booking_dt = datetime.strptime(f'{date_str} {start_time}', '%Y-%m-%d %H:%M')
            reminder_dt = booking_dt - timedelta(hours=24)
            wait = (reminder_dt - datetime.now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
            if sms_phone:
                # Re-read settings at fire time so a template edit between book
                # time and reminder time takes effect.
                latest    = get_db()
                latest_bs = latest.get('bookingSettings', {})
                send_sms(sms_phone, render_sms_template(
                    latest_bs.get('smsReminderTemplate', ''), sms_ctx))
        except Exception as e:
            print(f'Reminder error: {e}')

    threading.Thread(target=send_reminder, daemon=True).start()

    return jsonify({'ok': True, 'booking': booking})

@app.route('/book/cancel', methods=['POST'])
def book_cancel():
    bt = request.json.get('token', '')
    data = get_db()
    entry = _bt_get(data, bt)
    if not entry:
        return jsonify({'ok': False}), 401

    booking_id = request.json.get('bookingId', '')
    booking = next((b for b in data.get('bookings', [])
                    if b['id'] == booking_id
                    and b['memberEmail'].lower() == entry['email'].lower()), None)
    if not booking:
        return jsonify({'ok': False, 'error': 'Booking not found'}), 404

    booking['status'] = 'cancelled'
    save_data(data)
    return jsonify({'ok': True})

@app.route('/book/edit', methods=['POST'])
def book_edit():
    """Member-initiated edit of a future booking. Same conflict + overage gate
    as /book/create, but excludes the booking being edited from the checks."""
    bt = request.json.get('token', '')
    data = get_db()
    entry = _bt_get(data, bt)
    if not entry:
        return jsonify({'ok': False, 'error': 'Session expired'}), 401

    booking_id = request.json.get('bookingId', '')
    booking = next((b for b in data.get('bookings', [])
                    if b['id'] == booking_id
                    and b.get('memberEmail','').lower() == entry['email'].lower()
                    and b.get('status') != 'cancelled'), None)
    if not booking:
        return jsonify({'ok': False, 'error': 'Booking not found'}), 404

    # Only future bookings can be edited.
    try:
        existing_dt = datetime.strptime(f"{booking['date']} {booking['start']}", '%Y-%m-%d %H:%M')
        if existing_dt < datetime.now():
            return jsonify({'ok': False, 'error': 'Cannot edit past bookings'}), 400
    except (ValueError, KeyError):
        pass

    new_date  = request.json.get('date',  booking['date'])
    new_start = request.json.get('start', booking['start'])
    new_end   = request.json.get('end',   booking['end'])
    new_title = request.json.get('title', booking.get('title', 'Meeting'))
    new_res   = (request.json.get('resourceId') or booking.get('resourceId') or CONFERENCE_ROOM_ID).strip()
    accept_overage = bool(request.json.get('acceptOverage', False))

    return _apply_booking_edit(data, booking, new_date, new_start, new_end,
                               new_title, new_res, accept_overage,
                               sms_kind='member-edit')

def _apply_booking_edit(data, booking, new_date, new_start, new_end, new_title,
                        new_res, accept_overage, sms_kind):
    """Shared edit logic for both /book/edit (member) and /admin/api/booking-edit
    (admin). Returns a Flask response. Also called by admin-edit so behavior is
    identical except for sms_kind which controls the message template."""
    try:
        dt = datetime.strptime(new_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date'}), 400

    # Validate resource exists and is currently bookable.
    bookable = get_bookable_resources(data)
    chosen   = next((r for r in bookable if r['id'] == new_res), None)
    if not chosen:
        return jsonify({'ok': False, 'error': 'That space is not available for booking.'}), 400

    # Conflict check, excluding the booking being edited.
    for b in data.get('bookings', []):
        if (b['id'] != booking['id']
                and b.get('date') == new_date
                and b.get('status') != 'cancelled'
                and b.get('resourceId') == new_res
                and not (new_end <= b.get('start','') or new_start >= b.get('end',''))):
            return jsonify({'ok': False, 'error': 'That time slot is already booked.'})

    # Overage check — exclude this booking from "hours used" since we're replacing it.
    # Hours roll up to the parent member account; occupants without parentMember
    # (legacy bookings) fall back to their own memberName so totals stay correct.
    billed_to     = _booking_billed_to(booking)
    booking_hours = _booking_duration_hours({'start': new_start, 'end': new_end})
    other_used = sum(
        _booking_duration_hours(b) for b in data.get('bookings', [])
        if _booking_billed_to(b) == billed_to
        and b.get('year')   == dt.year
        and b.get('month')  == dt.month
        and b.get('status') != 'cancelled'
        and b['id'] != booking['id']
    )
    hours_inc      = hours_included(data, billed_to)
    bs             = data.get('bookingSettings', {})
    overage_rate   = float(bs.get('overageRatePerHour', 25))
    hours_after    = other_used + booking_hours
    hours_over     = max(0.0, hours_after - hours_inc)
    overage_charge = round(hours_over * overage_rate, 2)

    if hours_over > 0 and not accept_overage:
        return jsonify({
            'ok':                    False,
            'overage':               True,
            'hoursThisBooking':      round(booking_hours, 2),
            'hoursUsed':             round(other_used, 2),
            'hoursIncluded':         hours_inc,
            'hoursOver':             round(hours_over, 2),
            'overageRate':           overage_rate,
            'overageCharge':         overage_charge,
            'overageWarningMessage': bs.get('overageWarningMessage', ''),
        })

    # Apply the edit.
    booking['resourceType'] = chosen['type']
    booking['resourceId']   = chosen['id']
    booking['date']         = new_date
    booking['year']         = dt.year
    booking['month']        = dt.month
    booking['start']        = new_start
    booking['end']          = new_end
    booking['title']        = new_title
    booking['updatedAt']    = datetime.now().isoformat()
    if hours_over > 0:
        booking['overageHours']  = round(hours_over, 2)
        booking['overageRate']   = overage_rate
        booking['overageCharge'] = overage_charge
    else:
        booking.pop('overageHours',  None)
        booking.pop('overageRate',   None)
        booking.pop('overageCharge', None)
    save_data(data)

    # Send "your booking has been updated" SMS to the member, using the
    # admin-editable edit template against the *new* values (not stale).
    phone = _member_phone(data, booking.get('memberEmail',''), booking.get('memberName',''))
    if phone:
        ctx = _booking_sms_ctx(data, booking,
                               override_label=chosen['label'],
                               override_date=new_date,
                               override_start=new_start,
                               override_end=new_end)
        send_sms(phone, render_sms_template(
            bs.get('smsEditTemplate', ''), ctx))
    return jsonify({'ok': True, 'booking': booking})

def render_sms_template(tmpl, ctx):
    """Substitute {var} placeholders in an admin-editable SMS template. Falls
    back to the literal template string if a variable is missing so a typo'd
    template still sends *something* rather than nothing."""
    if not tmpl:
        return ''
    try:
        return tmpl.format(**ctx)
    except (KeyError, IndexError, ValueError):
        return tmpl

def _booking_sms_ctx(data, booking, override_label=None, override_date=None,
                     override_start=None, override_end=None):
    """Build the substitution dict for a booking SMS. Accepts overrides so the
    edit-confirmation can use the *new* values rather than the stale stamped
    ones on the booking row."""
    label = override_label or resource_label(data, booking.get('resourceId', CONFERENCE_ROOM_ID))
    return {
        'space':  label,
        'date':   override_date  or booking.get('date',  ''),
        'start':  override_start or booking.get('start', ''),
        'end':    override_end   or booking.get('end',   ''),
        'member': booking.get('memberName', ''),
    }

def _normalize_phone(p):
    """Reduce any phone string (with dashes, parens, +1 country code, etc.)
    down to a 10-digit string for comparison. Returns '' if fewer than 10
    digits are present."""
    digits = ''.join(filter(str.isdigit, str(p or '')))
    if len(digits) >= 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits if len(digits) == 10 else ''

def _member_by_phone(data, phone):
    """Look up an active OCCUPANT by phone number. Members (companies) are
    intentionally NOT matched here — bookings always flow through occupants
    so that monthly hour limits stay attached to the parent member account.
    Returns the occupant record dict augmented with:
      _phone_member_name   — occupant's own name (booked under)
      _phone_member_email  — occupant's email
      _phone_parent_member — the member account name to credit hours against
    None if no match."""
    digits = _normalize_phone(phone)
    if not digits:
        return None
    for o in data.get('occupants', []):
        if o.get('status') == 'Active' and _normalize_phone(o.get('phone','')) == digits:
            rec = dict(o)
            rec['_phone_member_name']   = o.get('name','')
            rec['_phone_member_email']  = o.get('email','')
            rec['_phone_parent_member'] = o.get('company','')
            return rec
    return None

def _user_by_phone(data, phone):
    """Look up an Active admin user by phone number. Returns the user dict
    (or None). Used by /admin/login to authenticate by phone + SMS 2FA. Any
    Active user's phone is a valid login — that's how the backup-phone setup
    works (Rocky adds a second user record with his Google Voice number, etc.)."""
    digits = _normalize_phone(phone)
    if not digits:
        return None
    for u in data.get('users', []):
        if u.get('status') == 'Active' and _normalize_phone(u.get('phone', '')) == digits:
            return u
    return None

def _member_phone(data, email, name):
    """Find the SMS-deliverable phone for a member or occupant. Email is checked
    first, then a name match. Returns '' if nothing usable is on file."""
    email = (email or '').lower()
    if email:
        m = next((m for m in data.get('members', [])
                  if m.get('email','').lower() == email), None)
        if m and m.get('phone'):
            return m['phone']
        o = next((o for o in data.get('occupants', [])
                  if o.get('email','').lower() == email), None)
        if o and o.get('phone'):
            return o['phone']
    if name:
        m = next((m for m in data.get('members', []) if m.get('name') == name), None)
        if m and m.get('phone'):
            return m['phone']
        o = next((o for o in data.get('occupants', []) if o.get('name') == name), None)
        if o and o.get('phone'):
            return o['phone']
    return ''


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN LOGIN (2-factor)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Phone-based admin login. Any Active record in DB.users can log in:
    enter phone → we send a 6-digit SMS code → /admin/2fa verifies it.
    Backup phone = just add a second user with that phone in the Admin tab."""
    if session.get('admin_authenticated'):
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        raw_phone = request.form.get('phone', '')
        phone     = _normalize_phone(raw_phone)
        if not phone:
            flash('Please enter a valid 10-digit phone number.', 'error')
            return render_template('admin/login.html')

        data = get_db()
        user = _user_by_phone(data, phone)
        if not user:
            # Vague error on purpose — don't leak which phones are admin users.
            flash('That phone number is not authorized for admin access.', 'error')
            return render_template('admin/login.html')

        # Send a 6-digit SMS code, stash a server-side pending entry, and
        # remember the sid + display tail in the Flask session so /admin/2fa
        # can pick up where we left off.
        code = generate_code()
        sid  = secrets.token_urlsafe(16)
        _p2fa_set(data, sid, {
            'code':    code,
            'expires': datetime.now() + timedelta(minutes=10),
            'purpose': 'admin',
            'phone':   phone,
            'userId':  user.get('id', ''),
            'name':    user.get('name', ''),
        })
        save_data(data)
        session['admin_2fa_sid']  = sid
        session['admin_2fa_tail'] = phone[-4:]    # for "code sent to ###-####" UX

        send_sms(phone, f'Qbix Centre admin login code: {code}. Expires in 10 minutes.')
        return redirect(url_for('admin_2fa'))

    return render_template('admin/login.html')

@app.route('/admin/2fa', methods=['GET', 'POST'])
def admin_2fa():
    sid  = session.get('admin_2fa_sid')
    data = get_db()
    if not sid or not _p2fa_get(data, sid):
        return redirect(url_for('admin_login'))

    tail = session.get('admin_2fa_tail', '')

    if request.method == 'POST':
        code  = request.form.get('code', '').strip()
        entry = _p2fa_get(data, sid)   # already filters expired

        if not entry:
            flash('Code expired. Please log in again.', 'error')
            return redirect(url_for('admin_login'))

        if entry['code'] != code:
            flash('Incorrect code. Please try again.', 'error')
            return render_template('admin/2fa.html', phone_tail=tail)

        # Success — fully authenticated. Stash who logged in for audit/UX.
        _p2fa_del(data, sid); save_data(data)
        session.pop('admin_2fa_sid',  None)
        session.pop('admin_2fa_tail', None)
        session['admin_authenticated'] = True
        session['admin_login_time']    = datetime.now().isoformat()
        session['admin_user_id']       = entry.get('userId', '')
        session['admin_user_name']     = entry.get('name', '')
        session.permanent = True

        return redirect(url_for('admin_dashboard'))

    return render_template('admin/2fa.html', phone_tail=tail)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('home'))


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — MANAGEMENT APP
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin')
@login_required
def admin_dashboard():
    data = get_db()
    active  = [m for m in data['members'] if m['status'] == 'Active']
    pending = [m for m in data['members'] if m['status'] == 'Pending']
    occ     = len([o for o in data['offices'] if o['status'] == 'Occupied'])
    vac     = len([o for o in data['offices'] if o['status'] == 'Vacant'])
    gross   = sum((m.get('dues') or 0) for m in active)
    net_rev = sum(net_dues(m) for m in active)
    dep     = sum((m.get('deposit') or 0) for m in active)
    endings = sorted([m for m in data['members'] if m.get('end')],
                     key=lambda m: m['end'])[:5]
    vacant  = [o for o in data['offices'] if o['status'] == 'Vacant']

    # Upcoming bookings (next 7 days)
    today = datetime.now().date()
    upcoming = sorted(
        [b for b in data.get('bookings', [])
         if b.get('status') == 'confirmed'
         and datetime.strptime(b['date'], '%Y-%m-%d').date() >= today],
        key=lambda b: (b['date'], b['start'])
    )[:10]

    return render_template('admin/dashboard.html',
        active=active, pending=pending,
        occ=occ, vac=vac, gross=gross, net_rev=net_rev, dep=dep,
        endings=endings, vacant=vacant, upcoming_bookings=upcoming,
        total=len(data['offices']),
        data=data)

# ── Data API (used by admin JS frontend) ─────────────────────────────────────

@app.route('/admin/api/data')
@login_required
def api_data():
    return jsonify(get_db())

@app.route('/admin/api/save', methods=['POST'])
@login_required
def api_save():
    try:
        data = request.json
        save_data(data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/admin/api/backup')
@login_required
def api_backup():
    try:
        data = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        data['lastBackup'] = today
        save_data(data)
        # Return the data as a downloadable JSON file
        import io
        buf = io.BytesIO(json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8'))
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                        download_name=f'qbix-backup-{today}.json',
                        mimetype='application/json')
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/admin/api/import-data', methods=['POST'])
@login_required
def import_data():
    """Import JSON data into PostgreSQL — use once to migrate existing data."""
    try:
        data = request.json
        if not data or 'offices' not in data:
            return jsonify({'ok': False, 'error': 'Invalid data format'}), 400
        save_data(data)
        return jsonify({'ok': True, 'message': 'Data imported successfully'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/admin/api/marketing-settings', methods=['GET'])
@login_required
def get_marketing_settings():
    data = get_db()
    return jsonify({'ok': True, 'settings': data.get('marketingSettings', {})})

@app.route('/admin/api/marketing-settings', methods=['POST'])
@login_required
def save_marketing_settings():
    data = get_db()
    incoming = request.json or {}
    ms = data.setdefault('marketingSettings', {})
    # Merge top-level keys
    for k, v in incoming.items():
        ms[k] = v
    save_data(data)
    return jsonify({'ok': True})


@app.route('/admin/api/import-photos', methods=['POST'])
@login_required
def import_photos():
    """Import photos from WordPress export into posts and offices."""
    try:
        payload = request.json
        post_updates   = payload.get('post_updates', {})
        office_updates = payload.get('office_updates', {})

        data = get_db()

        post_count = 0
        for title, updates in post_updates.items():
            post = next((p for p in data.get('newsletter', []) if p.get('subject') == title), None)
            if post:
                if 'heroPhoto' in updates:
                    post['heroPhoto'] = updates['heroPhoto']
                if 'galleryPhotos' in updates:
                    post['galleryPhotos'] = updates['galleryPhotos']
                post_count += 1

        office_count = 0
        for num, updates in office_updates.items():
            office = next((o for o in data.get('offices', []) if o.get('num') == num), None)
            if office:
                if 'photos' in updates:
                    office['photos'] = updates['photos']
                if 'heroPhoto' in updates:
                    office['heroPhoto'] = updates['heroPhoto']
                office_count += 1

        save_data(data)
        return jsonify({'ok': True, 'posts': post_count, 'offices': office_count})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/admin/api/test-sms')
@login_required
def test_sms():
    """Send a test SMS to verify Twilio configuration."""
    cfg = {
        'method':               'Twilio SMS',
        'TWILIO_ACCOUNT_SID':   TWILIO_ACCOUNT_SID[:8]+'...' if TWILIO_ACCOUNT_SID else '(not set)',
        'TWILIO_PHONE_NUMBER':  TWILIO_PHONE_NUMBER or '(not set)',
        'ADMIN_PHONE':          ADMIN_PHONE or '(not set)',
    }
    ok = send_sms(ADMIN_PHONE, 'Qbix Centre test SMS — Twilio is working!')
    return jsonify({
        'ok': ok,
        'config': cfg,
        'message': f'Test SMS sent to {ADMIN_PHONE}! Check your phone.' if ok else 'FAILED: Check Railway Twilio variables.'
    })



@app.route('/admin/api/onboard-link', methods=['POST'])
@login_required
def generate_onboard_link():
    name  = request.json.get('name', '')
    email = request.json.get('email', '')
    token = secrets.token_urlsafe(16)
    data  = get_db()
    _ot_set(data, token, {
        'name':    name,
        'email':   email,
        'expires': datetime.now() + timedelta(days=7),
    })
    save_data(data)
    link = f'{APP_URL}/onboard/{token}'

    # Email the link to the prospect
    if email:
        send_email(
            email, name,
            'Welcome to Qbix Centre — Complete Your Application',
            f'<p>Hi {name},</p>'
            f'<p>Thank you for your interest in Qbix Centre! Please click the link below to complete your membership application:</p>'
            f'<p><a href="{link}" style="background:#2563eb;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;display:inline-block">Complete My Application</a></p>'
            f'<p>This link expires in 7 days.</p>'
            f'<p>If you have any questions, reply to this email or call (478) 216-2876.</p>'
            f'<p>We look forward to welcoming you!</p>'
            f'<p>— The Qbix Centre Team</p>',
        )

    return jsonify({'ok': True, 'link': link, 'token': token})

# ── Agreement status update ───────────────────────────────────────────────────

@app.route('/admin/api/update-agreement-status/<member_id>', methods=['POST'])
@login_required
def update_agreement_status(member_id):
    """Update a member's agreement status (Received, Sent, Pending, N/A)."""
    data = get_db()
    member = next((m for m in data['members'] if m['id'] == member_id), None)
    if not member:
        return jsonify({'ok': False, 'error': 'Member not found'}), 404
    payload = request.json or {}
    status = payload.get('agreementStatus', '')
    allowed = ['Pending', 'Sent', 'Received', 'N/A']
    if status not in allowed:
        return jsonify({'ok': False, 'error': f'Invalid status. Must be one of: {allowed}'}), 400
    member['agreementStatus'] = status
    save_data(data)
    return jsonify({'ok': True, 'agreementStatus': status})

# ── Agreement generator ───────────────────────────────────────────────────────

@app.route('/admin/api/generate-agreement/<member_id>')
@login_required
def generate_agreement(member_id):
    """Render membership agreement as styled HTML in a new browser tab."""
    waive_setup_fee = request.args.get('waive_setup_fee', 'false').lower() == 'true'
    data   = get_db()
    member = next((m for m in data['members'] if m['id'] == member_id), None)
    if not member:
        abort(404)

    member_offices = [o for o in data['offices'] if o.get('member') == member['name']]
    offices        = [o['num'] for o in member_offices]
    office_str     = ', '.join(f'Office {n}' for n in offices) if offices else 'TBD'

    gross_amt     = sum(float(o.get('listDues') or 0) for o in member_offices)
    disc_amt      = sum(float(o.get('discount') or 0) for o in member_offices)
    net_amt       = max(0, gross_amt - disc_amt)
    deposit_amt   = member.get('deposit', 0) or 0
    dues_str      = f'${net_amt:,.0f}/month'
    deposit_str   = f'${deposit_amt:,}'
    proration_amt = float(member.get('proration') or 0)

    from calendar import monthrange
    raw_start = member.get('start', '')
    today_str = datetime.now().strftime('%B %d, %Y')
    pro_str   = ''
    if raw_start:
        try:
            sd = datetime.strptime(raw_start, '%Y-%m-%d')
            if sd.day == 1:
                term_start = sd
            else:
                last_day_of_start = monthrange(sd.year, sd.month)[1]
                pro_end  = sd.replace(day=last_day_of_start)
                pro_str  = sd.strftime('%B %d') + ' \u2013 ' + pro_end.strftime('%B %d, %Y')
                term_start = sd.replace(month=sd.month+1, day=1) if sd.month < 12 else sd.replace(year=sd.year+1, month=1, day=1)
            end_month = term_start.month + 5
            end_year  = term_start.year + (end_month - 1) // 12
            end_month = ((end_month - 1) % 12) + 1
            last_day  = monthrange(end_year, end_month)[1]
            term_end  = datetime(end_year, end_month, last_day)
            term_start_str = term_start.strftime('%B %d, %Y')
            term_end_str   = term_end.strftime('%B %d, %Y')
            start_str      = sd.strftime('%B %d, %Y')
        except Exception:
            term_start_str = '_______________'
            term_end_str   = '_______________'
            start_str      = raw_start
    else:
        term_start_str = '_______________'
        term_end_str   = '_______________'
        start_str      = '_______________'

    conf_hours = len(offices) * 6

    def fld(label, value):
        return f'<tr><td class="fl">{label}</td><td class="fv">{value}</td></tr>'

    def blt(text):
        return f'<li>{text}</li>'

    def sec(num, title):
        return f'<h2 class="sec-head"><span class="sec-num">{num}.</span> {title.upper()}</h2>'

    pro_field  = fld(f'Prorated First Payment ({pro_str})' if pro_str else 'Prorated First Payment', f'${proration_amt:,.0f}') if proration_amt > 0 else ''
    pro_bullet = blt(f'Prorated first payment ({pro_str}): <strong>${proration_amt:,.0f}</strong>, due at signing.') if proration_amt > 0 else ''
    setup_blt  = '' if waive_setup_fee else blt('A one-time setup fee of <strong>$100</strong> is due at signing.')
    pro_sig    = fld('Prorated First Payment', f'${proration_amt:,.0f}') if proration_amt > 0 else ''
    pro_ach    = fld('Prorated First Draft', f'<strong>${proration_amt:,.0f}</strong>') if proration_amt > 0 else ''
    mname      = member.get('name', '')
    memail     = member.get('email', '')
    mphone     = member.get('phone', '')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Qbix Centre Membership Agreement</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',Arial,sans-serif;font-size:10.5pt;color:#222;background:#e8e8e8;padding:30px 20px}}
.page{{background:#fff;max-width:800px;margin:0 auto;padding:60px 70px;box-shadow:0 2px 20px rgba(0,0,0,.15)}}
.hdr{{text-align:center;border-bottom:3px solid #1a2744;padding-bottom:20px;margin-bottom:24px}}
.hdr-title{{font-family:'Playfair Display',serif;font-size:26pt;color:#1a2744}}
.hdr-sub{{font-size:11pt;color:#555;margin-top:4px}}
.hdr-addr{{font-size:9pt;color:#888;margin-top:6px}}
.hdr-date{{font-size:9pt;color:#888;margin-top:4px;font-style:italic}}
.summary{{background:#f7f9fc;border:1px solid #d0d8e8;border-radius:6px;padding:18px 22px;margin-bottom:28px}}
.summary-title{{font-size:8pt;text-transform:uppercase;letter-spacing:.1em;color:#1a2744;font-weight:700;margin-bottom:12px}}
table.fields{{width:100%;border-collapse:collapse}}
.fl{{padding:5px 0;color:#555;font-size:9.5pt;width:55%}}
.fv{{padding:5px 0;color:#222;font-size:9.5pt;text-align:right;font-weight:500}}
.sec-head{{font-size:9pt;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#1a2744;border-bottom:1.5px solid #1a2744;padding-bottom:4px;margin:24px 0 10px}}
.sec-num{{color:#d4a843}}
ul.clauses{{padding-left:18px;margin:0}}
ul.clauses li{{margin-bottom:7px;line-height:1.55;font-size:9.5pt}}
.page-break{{border-top:2px solid #1a2744;margin-top:48px;padding-top:32px}}
.sec-title{{text-align:center;font-size:14pt;font-weight:700;color:#1a2744;margin-bottom:6px}}
.sec-sub{{text-align:center;font-size:9pt;color:#888;margin-bottom:24px;font-style:italic}}
.sig-row{{display:flex;gap:40px;margin-top:20px}}
.sig-field{{flex:1;border-bottom:1px solid #aaa;min-height:26px;padding-bottom:2px}}
.sig-label{{font-size:8pt;color:#888;margin-top:4px}}
.prefilled{{color:#333;font-size:10pt}}
@media print{{
  body{{background:#fff;padding:0}}
  .page{{box-shadow:none;padding:40px 50px;max-width:100%}}
  .no-print{{display:none}}
}}
</style>
</head>
<body>
<div class="no-print" style="max-width:800px;margin:0 auto 16px;display:flex;gap:12px;align-items:center">
  <button onclick="window.print()" style="background:#1a2744;color:#fff;border:none;border-radius:6px;padding:10px 24px;font-size:13px;font-weight:600;cursor:pointer;font-family:Inter,sans-serif">&#128438;&nbsp; Print / Save as PDF</button>
  <span style="color:#555;font-size:12px">Print dialog &rarr; select &ldquo;Save as PDF&rdquo; as the printer</span>
</div>
<div class="page">
  <div class="hdr">
    <div class="hdr-title">QBIX CENTRE</div>
    <div class="hdr-sub">Membership Agreement</div>
    <div class="hdr-addr">500A Northside Crossing, Macon, GA 31210 &bull; (478) 787-0532 &bull; qbixcentre.com</div>
    <div class="hdr-date">Agreement Date: {today_str}</div>
  </div>
  <div class="summary">
    <div class="summary-title">Member Summary</div>
    <table class="fields">
      {fld('Member / Company', f'<strong>{mname}</strong>')}
      {fld('Office(s) Assigned', f'<strong>{office_str}</strong>')}
      {fld('Monthly Membership Fee', f'<strong>{dues_str}</strong>')}
      {pro_field}
      {fld('Refundable Deposit', deposit_str)}
      {fld('Term Start Date', term_start_str)}
      {fld('Term End Date', term_end_str)}
      {fld('Conference Room Hours Included', f'{conf_hours} hours/month')}
      {fld('Contact Email', memail)}
      {fld('Contact Phone', mphone)}
    </table>
  </div>
  {sec(1,'Membership & Fees')}
  <ul class="clauses">
    {blt(f'Monthly membership fee of <strong>{dues_str}</strong> is due on the 1st of each month via auto-draft through Bill.com. Payments received after the 5th are considered late.')}
    {pro_bullet}
    {blt(f'A refundable deposit of <strong>{deposit_str}</strong> is required prior to move-in and will be returned, less normal wear and tear and cost of unreturned keys, upon conclusion of the membership.')}
    {setup_blt}
    {blt(f'The initial term of this Agreement is six (6) full calendar months, from <strong>{term_start_str}</strong> through <strong>{term_end_str}</strong>. This Agreement automatically renews for successive six (6) month periods at the then-current rate unless the Member provides written notice of non-renewal at least thirty (30) days prior to the end of the then-current term. Failure to provide timely notice results in automatic renewal and the Member\'s obligation for the full succeeding term.')}
    {blt('A non-refundable background check fee of $35 per cardholder is required prior to access being granted.')}
    {blt('Additional key/fob holders: $150/month plus $35 background check fee. Both keyholders must be from the same company.')}
  </ul>
  {sec(2,'Access & Use')}
  <ul class="clauses">
    {blt('Members receive 24/7 access via card key/fob and a personal access code. Access codes are strictly confidential and must not be shared. Card replacement fee: $35.')}
    {blt(f'The conference room may be reserved online at qbixcentre.com at least 24 hours in advance. Each membership includes <strong>{conf_hours} hours/month</strong> at no charge; additional time is billed at $25/hour.')}
    {blt('The workspace is for lawful, professional business purposes only. Sleeping, cooking meals, or personal activities on the premises are not permitted.')}
    {blt('Members are responsible for safeguarding their own confidential information and must respect the privacy and confidentiality of fellow members.')}
  </ul>
  {sec(3,'Amenities & Overage Charges')}
  <ul class="clauses">
    {blt('Included: High-speed Wi-Fi/Ethernet (AT&T Gigabit Fiber), furnished workstations, kitchenette with Starbucks coffee and beverages, full-color laser printer/scanner, conference room, free parking, and janitorial service.')}
    {blt('Monthly printing allowances: 200 black &amp; white pages; 100 color pages. Overages: $0.10/page B&amp;W; $0.20/page color.')}
    {blt('Conference room overages: $25/hour, billed monthly.')}
    {blt('Mail handling is included with all private office memberships.')}
  </ul>
  {sec(4,'Conduct & Responsibilities')}
  <ul class="clauses">
    {blt('Members shall conduct themselves in a professional, courteous, and cooperative manner at all times. Disruptive behavior or harassment may result in immediate termination.')}
    {blt('Noise: Use headphones for audio; avoid speakerphone calls or amplified sound in common areas.')}
    {blt('Cleanliness: Wash dishes immediately after use. Label food and beverages; unlabeled items discarded weekly. Clear personal items from common areas daily.')}
    {blt('Safety: No hazardous materials, open flames, smoking, or pets anywhere on the premises.')}
    {blt('Members are fully responsible for their own conduct and that of any guests. Damages must be reported immediately and paid in full.')}
    {blt('Prohibited: pyramid schemes, harassment, unauthorized use of others\' information, theft, or display of inappropriate content.')}
  </ul>
  {sec(5,'Insurance & Liability')}
  <ul class="clauses">
    {blt('Qbix Centre does not provide insurance for members\' personal property or business assets. Members are strongly encouraged to obtain appropriate coverage.')}
    {blt('RoseAn Properties, LLC and affiliates shall not be liable for theft, loss, damage, or injury to persons or property on the premises, to the maximum extent permitted by law.')}
    {blt('Member agrees to indemnify and hold harmless RoseAn Properties, LLC and Qbix Centre from all claims, damages, or expenses (including attorneys\' fees) arising from Member\'s use of the premises.')}
  </ul>
  {sec(6,'Termination')}
  <ul class="clauses">
    {blt('By Member: Written notice of non-renewal must be provided at least thirty (30) days prior to the end of the then-current six (6) month term. Notice after this deadline will not prevent automatic renewal; Member remains responsible for dues for the full succeeding term.')}
    {blt('By Management: Qbix Centre may terminate any membership immediately and without refund for violation of this Agreement or the House Guidelines.')}
    {blt('Upon termination, Member must return all keys, remove all personal property within 48 hours, and leave their office in clean condition.')}
  </ul>
  {sec(7,'General Provisions')}
  <ul class="clauses">
    {blt('Not a Lease: This Agreement grants a revocable license to use shared workspace and does not create a tenancy, leasehold interest, or any real property right.')}
    {blt('Force Majeure: Services may be suspended without liability for events beyond management\'s reasonable control.')}
    {blt('Modifications: House Guidelines and policies may be updated at any time. Members will be notified of material changes by email.')}
    {blt('Authority: The person signing represents that they have full authority to bind themselves and/or their company.')}
    {blt('Promotional Use: Member consents to Qbix Centre publishing their business name in directories. Written consent required for photos identifying individual members.')}
    {blt('Governing Law: This Agreement is governed by the laws of the State of Georgia.')}
    {blt('Entire Agreement: This Agreement, together with the House Guidelines, constitutes the entire agreement between the parties.')}
  </ul>
  {sec(8,'House Guidelines')}
  <p style="font-size:9.5pt;line-height:1.6;margin-top:8px">Qbix Centre maintains a separate House Guidelines document governing day-to-day conduct, use of common areas, equipment, noise, and cleanliness. By signing this Agreement, Member acknowledges that they have received, read, and agree to abide by the House Guidelines in their current form. Member acknowledges that the House Guidelines are subject to change at the sole discretion of the Manager, and that continued use of the premises constitutes acceptance of any updated guidelines.</p>

  <!-- Signature Page -->
  <div class="page-break">
    <div class="sec-title">SIGNATURE PAGE</div>
    <div class="sec-sub">Qbix Centre Membership Agreement</div>
    <p style="font-size:9.5pt;line-height:1.6;margin-bottom:20px">By signing below, Member acknowledges that they have read, understand, and agree to all terms and conditions of this Membership Agreement, and acknowledges receipt of and agreement to abide by the Qbix Centre House Guidelines (as may be updated from time to time). Member represents that they have authority to enter into this Agreement on behalf of themselves and/or their company.</p>
    <table class="fields" style="margin-bottom:24px">
      {fld('Office(s) Assigned', office_str)}
      {fld('Monthly Membership Fee', dues_str)}
      {pro_sig}
      {fld('Deposit Collected', deposit_str)}
      {fld('Term', f'{term_start_str} &ndash; {term_end_str}')}
    </table>
    <h2 class="sec-head">Member</h2>
    <div class="sig-row"><div style="flex:2"><div class="sig-field"></div><div class="sig-label">Member Signature</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Date</div></div></div>
    <div class="sig-row"><div style="flex:2"><div class="sig-field prefilled">{mname}</div><div class="sig-label">Print Name</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Title / Position</div></div></div>
    <h2 class="sec-head" style="margin-top:36px">Qbix Centre Acceptance</h2>
    <div class="sig-row"><div style="flex:2"><div class="sig-field"></div><div class="sig-label">Authorized Signature</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Date</div></div></div>
    <div class="sig-row"><div style="flex:2"><div class="sig-field"></div><div class="sig-label">Print Name</div></div><div style="flex:1"><div class="sig-field prefilled">Manager, RoseAn Properties, LLC</div><div class="sig-label">Title</div></div></div>
  </div>

  <!-- Background Check -->
  <div class="page-break">
    <div class="sec-title">CONFIDENTIAL BACKGROUND CHECK AUTHORIZATION</div>
    <div class="sec-sub">Non-refundable fee: $35 per cardholder &bull; Required prior to access</div>
    <h2 class="sec-head">Applicant Information</h2>
    <div class="sig-row"><div style="flex:1"><div class="sig-field prefilled">{mname}</div><div class="sig-label">Print Full Legal Name</div></div></div>
    <div class="sig-row"><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Former Name(s) &amp; Dates Used</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Social Security Number</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Date of Birth</div></div></div>
    <div class="sig-row"><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Current Address (include move-in month/year)</div></div></div>
    <div class="sig-row"><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Previous Address #1</div></div></div>
    <div class="sig-row"><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Previous Address #2</div></div></div>
    <h2 class="sec-head" style="margin-top:20px">Identification Required</h2>
    <p style="font-size:9.5pt;line-height:1.6;margin-top:6px">A legible copy of your current driver\'s license or government-issued photo ID must be attached.</p>
    <h2 class="sec-head" style="margin-top:20px">Authorization Statement</h2>
    <p style="font-size:9.5pt;line-height:1.6;margin-top:6px">I hereby authorize RoseAn Properties, LLC and its designated agents to conduct a comprehensive background investigation including verification of credit history, residential history, employment history, educational background, criminal and civil court records, driving records, and any other public records deemed relevant. A photocopy of this authorization shall be as valid as the original.</p>
    <div class="sig-row"><div style="flex:2"><div class="sig-field"></div><div class="sig-label">Applicant Signature</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Date</div></div></div>
  </div>

  <!-- Auto-Draft -->
  <div class="page-break">
    <div class="sec-title">AUTO-DRAFT AUTHORIZATION</div>
    <div class="sec-sub">Bill.com, Inc. on behalf of RoseAn Properties, LLC &bull; Required for all memberships</div>
    <h2 class="sec-head">Banking Information</h2>
    <div class="sig-row"><div style="flex:2"><div class="sig-field prefilled">{mname}</div><div class="sig-label">Account Holder Name</div></div><div style="flex:2"><div class="sig-field"></div><div class="sig-label">Bank Name</div></div></div>
    <div class="sig-row"><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Account Number</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Routing Number</div></div></div>
    <p style="font-size:9pt;color:#666;margin-top:10px;font-style:italic">(Attach a voided check if preferred.)</p>
    <h2 class="sec-head" style="margin-top:20px">Monthly Draft Amount</h2>
    <table class="fields" style="margin-top:8px">
      {fld('Authorized Monthly Amount', f'<strong>{dues_str}</strong>')}
      {pro_ach}
    </table>
    <p style="font-size:9pt;color:#555;margin-top:8px">Draft date: 1st of each month. If the 1st falls on a weekend or holiday, draft processes the next business day.</p>
    <h2 class="sec-head" style="margin-top:20px">Authorization</h2>
    <p style="font-size:9.5pt;line-height:1.6;margin-top:6px">I/We authorize Bill.com, Inc., on behalf of RoseAn Properties, LLC (Qbix Centre), to initiate recurring ACH debit entries to the bank account above in the amount of <strong>{dues_str}</strong>, beginning <strong>{term_start_str}</strong>. This authorization remains in effect until canceled in writing at least ten (10) business days prior to the desired cancellation date.</p>
    <div class="sig-row"><div style="flex:2"><div class="sig-field"></div><div class="sig-label">Account Holder Signature</div></div><div style="flex:1"><div class="sig-field"></div><div class="sig-label">Date</div></div></div>
    <div class="sig-row"><div style="flex:2"><div class="sig-field prefilled">{mname}</div><div class="sig-label">Print Name</div></div></div>
  </div>

</div>
</body>
</html>"""

    member['agreementSent'] = datetime.now().strftime('%Y-%m-%d')
    save_data(data)
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}



@app.route('/admin/api/generate-newsletter', methods=['POST'])
@login_required
def generate_newsletter():
    if not ANTHROPIC_API_KEY:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400

    data   = get_db()
    active = [m for m in data['members'] if m['status'] == 'Active']
    occ    = len([o for o in data['offices'] if o['status'] == 'Occupied'])
    vac    = len([o for o in data['offices'] if o['status'] == 'Vacant'])
    month  = datetime.now().strftime('%B %Y')

    # SEO keywords — weave 2-3 naturally
    seo_kws = data.get('marketingSettings', {}).get('seoKeywords', [])
    kw_note = ''
    if seo_kws:
        sample = seo_kws[:5]
        kw_note = f' Naturally weave in 2-3 of these SEO keywords where they fit organically (do not force them): {", ".join(sample)}.'

    context = (
        f"Qbix Centre is a professional coworking space in Macon, Georgia at 500A Northside Crossing. "
        f"Current stats: {occ} offices occupied, {vac} vacant, {len(active)} active members. "
        f"Month: {month}. "
        f"Amenities: 24/7 access, AT&T Fiber, Starbucks coffee, furnished offices, conference room, free parking."
        + kw_note
    )

    req_body     = request.json or {}
    custom_notes = req_body.get('notes', '')
    nl_type      = req_body.get('nlType', 'Monthly Update')
    spotlight    = req_body.get('spotlight', {})  # {name, profession, tenure, personalNote}

    try:
        import urllib.request
        import json as json_mod

        base = 'Format as clean HTML for email using <p>, <strong>, <ul>/<li> tags only. Do not use markdown. Do not invent details not provided.'

        if nl_type == 'Member Spotlight':
            name        = spotlight.get('name', 'our member')
            profession  = spotlight.get('profession', '')
            tenure      = spotlight.get('tenure', '')
            personal    = spotlight.get('personalNote', '')
            prompt = (
                f'Write a warm, engaging Member Spotlight feature article for the Qbix Centre newsletter. '
                f'This is a profile of one of our members. '
                f'Member name: {name}. '
                + (f'Profession/business: {profession}. ' if profession else '')
                + (f'How long at Qbix: {tenure}. ' if tenure else '')
                + (f'Personal note from manager: {personal}. ' if personal else '')
                + f'Structure: warm intro paragraph welcoming them to the spotlight, 2-3 paragraphs about their work and presence in the Qbix community, a friendly closing encouraging members to connect with them. '
                + f'Tone: warm, personal, community-focused. Do not invent details beyond what is provided. '
                + f'Background context (weave in lightly): {context}. {base}'
            )

        elif nl_type == 'Community':
            if custom_notes.strip():
                prompt = (
                    f'Write a warm community-focused newsletter for Qbix Centre about local north Macon business news and happenings relevant to our professional tenant community. '
                    f'Manager notes on what to cover: {custom_notes}. '
                    f'3-4 paragraphs. Conversational, neighbor-to-neighbor tone — like a community insider sharing news. '
                    f'Context: {context}. {base}'
                )
            else:
                prompt = (
                    f'Write a warm community-focused newsletter for Qbix Centre about the north Macon professional community. '
                    f'3-4 paragraphs touching on themes like local business, networking, professional growth, and community. '
                    f'Tone: conversational, warm, community insider. '
                    f'Context: {context}. {base}'
                )

        elif nl_type == 'Availability':
            if custom_notes.strip():
                prompt = (
                    f'Write a professional but friendly availability/promotional newsletter for Qbix Centre. '
                    f'Manager notes: {custom_notes}. '
                    f'Highlight available office space and membership options. Include a clear call to action to schedule a tour. '
                    f'Tone: welcoming and professional, not pushy. 2-3 paragraphs. '
                    f'Context: {context}. {base}'
                )
            else:
                prompt = (
                    f'Write a professional but friendly availability newsletter for Qbix Centre. '
                    f'{vac} office(s) currently available. Mention flexible membership options (private office, flex membership, virtual address). '
                    f'Include a call to action to schedule a tour or reach out. Tone: welcoming, not pushy. 2-3 paragraphs. '
                    f'Context: {context}. {base}'
                )

        else:  # Monthly Update (default)
            if custom_notes.strip():
                intro  = 'Write a warm, friendly, professional monthly newsletter for Qbix Centre. '
                focus  = 'The manager has provided specific content to cover — this is the main focus of the newsletter. Cover it fully: ' + custom_notes + ' '
                outro  = 'Add a brief welcoming opener and a friendly closing. Background context (weave in naturally): ' + context + '. ' + base
                prompt = intro + focus + outro
            else:
                prompt = (
                    'Write a warm, friendly, professional monthly newsletter for Qbix Centre. '
                    '3-4 short paragraphs: welcoming opener, community snapshot, friendly closing. '
                    'Context: ' + context + '. ' + base
                )

        payload = {
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 1000,
            'messages': [{'role': 'user', 'content': prompt}]
        }

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json_mod.dumps(payload).encode(),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json_mod.loads(resp.read())
            draft = result['content'][0]['text']
            draft = draft.strip()
            if draft.startswith('```'):
                draft = draft.split('\n', 1)[-1]
            if draft.endswith('```'):
                draft = draft.rsplit('```', 1)[0]
            draft = draft.strip()
            return jsonify({'ok': True, 'draft': draft})

    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return jsonify({'ok': False, 'error': f'Anthropic HTTP {e.code}: {body[:400]}'}), 500
    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error': str(e), 'detail': traceback.format_exc()[-600:]}), 500

@app.route('/admin/api/generate-social-posts', methods=['POST'])
@login_required
def generate_social_posts():
    """Generate Google, Facebook, and Nextdoor posts from newsletter content."""
    if not ANTHROPIC_API_KEY:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400

    body_text = request.json.get('body', '')  # newsletter body (HTML or plain)
    subject   = request.json.get('subject', '')

    # Strip HTML tags for cleaner AI input
    import re
    clean_body = re.sub(r'<[^>]+>', ' ', body_text)
    clean_body = re.sub(r'\s+', ' ', clean_body).strip()[:2000]

    prompt = f"""You are a social media writer for Qbix Centre, a professional coworking space at 500A Northside Crossing in north Macon, GA.

Given this newsletter content:
Subject: {subject}
Body: {clean_body}

Write THREE platform-specific posts. Return ONLY valid JSON with this exact structure (no markdown fences):
{{
  "google": "...",
  "facebook": "...",
  "nextdoor": "..."
}}

Rules:
- google: 150-300 words. Professional tone. Include north Macon / Northside Crossing keywords naturally. End with a call to action (e.g. "Contact us to schedule a tour"). No hashtags.
- facebook: 40-80 words. Warm, conversational. Include a referral hook like "Know someone who needs office space in north Macon?" Add 2-3 hashtags at the end (#QbixCentre #MaconGA #NorthMacon).
- nextdoor: 40-70 words. Neighbor-to-neighbor tone. Community framing. No hashtags. Never feel like an ad. Something a real neighbor would write.

Do not invent facts not in the newsletter. Return ONLY the JSON object."""

    try:
        import urllib.request
        import json as json_mod

        payload = {
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 1000,
            'messages': [{'role': 'user', 'content': prompt}]
        }
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json_mod.dumps(payload).encode(),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json_mod.loads(resp.read())
            raw = result['content'][0]['text'].strip()
            # Strip fences if present
            if raw.startswith('```'):
                raw = raw.split('\n', 1)[-1]
            if raw.endswith('```'):
                raw = raw.rsplit('```', 1)[0]
            raw = raw.strip()
            posts = json_mod.loads(raw)
            return jsonify({'ok': True, 'posts': posts})

    except urllib.error.HTTPError as e:
        body_err = e.read().decode('utf-8', errors='replace')
        return jsonify({'ok': False, 'error': f'Anthropic HTTP {e.code}: {body_err[:400]}'}), 500
    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error': str(e), 'detail': traceback.format_exc()[-600:]}), 500


@app.route('/admin/api/publish-newsletter', methods=['POST'])
@login_required
def publish_newsletter():
    """Save newsletter and optionally email to all active members."""
    data       = get_db()
    subject    = request.json.get('subject', f'Qbix Centre Newsletter — {datetime.now().strftime("%B %Y")}')
    body       = request.json.get('body', '')
    send       = request.json.get('send', False)
    category   = request.json.get('category', 'Monthly Update')
    scheduled  = request.json.get('scheduledFor', None)  # ISO datetime string or None
    hero_photo      = request.json.get('heroPhoto', None)
    gallery_photos  = request.json.get('galleryPhotos', [])

    post_id = '_' + secrets.token_hex(4)

    if scheduled:
        # Save as a scheduled draft — will auto-publish when time arrives
        post = {
            'id':          post_id,
            'subject':     subject,
            'body':        body,
            'category':    category,
            'date':        datetime.now().isoformat(),
            'scheduledFor': scheduled,
            'draft':       True,
            'sent':        False,
        }
        if hero_photo:    post['heroPhoto']     = hero_photo
        if gallery_photos: post['galleryPhotos'] = gallery_photos
        data.setdefault('newsletter', []).append(post)
        save_data(data)
        return jsonify({'ok': True, 'sent': 0, 'postId': post_id, 'scheduled': True})

    post = {
        'id':       post_id,
        'subject':  subject,
        'body':     body,
        'category': category,
        'date':     datetime.now().isoformat(),
        'sent':     send,
        'draft':    False,
    }
    if hero_photo:    post['heroPhoto']     = hero_photo
    if gallery_photos: post['galleryPhotos'] = gallery_photos
    data.setdefault('newsletter', []).append(post)

    # Add marketing action alert: newsletter published → pending GBP post
    ms = data.setdefault('marketingSettings', {})
    alerts = ms.setdefault('marketingAlerts', [])
    alerts = [a for a in alerts if a.get('type') != 'gbp_post_pending']
    alerts.append({
        'id': 'gbp_' + secrets.token_hex(3),
        'type': 'gbp_post_pending',
        'message': f'Newsletter "{subject}" was published — post it to Google Business Profile to keep your profile active.',
        'dismissed': False
    })
    ms['marketingAlerts'] = alerts
    save_data(data)

    if send:
        active_with_email = [m for m in data['members']
                             if m['status'] == 'Active' and m.get('email')]
        sent_count = 0
        for m in active_with_email:
            ok = send_email(m['email'], m['name'], subject, body)
            if ok:
                sent_count += 1
        return jsonify({'ok': True, 'sent': sent_count, 'postId': post_id})

    return jsonify({'ok': True, 'sent': 0, 'postId': post_id})

@app.route('/admin/api/reschedule-post', methods=['POST'])
@login_required
def reschedule_post():
    """Update or remove the scheduled publish time on an existing draft post."""
    data        = get_db()
    post_id     = request.json.get('postId')
    scheduled   = request.json.get('scheduledFor')  # ISO string or None to unschedule
    post = next((p for p in data.get('newsletter', []) if p['id'] == post_id), None)
    if not post:
        return jsonify({'ok': False, 'error': 'Post not found'}), 404
    if scheduled:
        post['scheduledFor'] = scheduled
        post['draft'] = True
    else:
        # Cancel schedule — revert to plain draft
        post.pop('scheduledFor', None)
    save_data(data)
    return jsonify({'ok': True})

# ── Booking management (admin view) ──────────────────────────────────────────

@app.route('/admin/bookings')
@login_required
def admin_bookings():
    return redirect(url_for('admin_dashboard'))

# ── Booking Settings (admin-editable rate, warning, SMS templates) ───────────

# Whitelist of editable keys + their type (str|float). Anything else posted is
# silently dropped so a stray field can't sneak into the JSON blob.
_BOOKING_SETTINGS_FIELDS = {
    'overageRatePerHour':      'float',
    'overageWarningMessage':   'str',
    'smsConfirmationTemplate': 'str',
    'smsReminderTemplate':     'str',
    'smsEditTemplate':         'str',
    'smsCancelTemplate':       'str',
    'optInDisclosure':         'str',
    'privacySmsHtml':          'str',
}

@app.route('/admin/api/booking-settings', methods=['GET'])
@login_required
def admin_get_booking_settings():
    data = get_db()
    bs   = data.get('bookingSettings', {})
    out  = {k: bs.get(k, '') for k in _BOOKING_SETTINGS_FIELDS}
    return jsonify({'ok': True, 'settings': out})

@app.route('/admin/api/booking-settings', methods=['POST'])
@login_required
def admin_save_booking_settings():
    payload = request.json or {}
    data = get_db()
    bs   = data.setdefault('bookingSettings', {})
    for key, kind in _BOOKING_SETTINGS_FIELDS.items():
        if key not in payload:
            continue
        val = payload[key]
        if kind == 'float':
            try:
                val = float(val)
                if val < 0:
                    return jsonify({'ok': False, 'error': f'{key} cannot be negative'}), 400
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': f'{key} must be a number'}), 400
            bs[key] = val
        else:
            bs[key] = (val or '').strip() if isinstance(val, str) else val
    save_data(data)
    return jsonify({'ok': True})

@app.route('/admin/api/booking-cancel', methods=['POST'])
@login_required
def admin_cancel_booking():
    booking_id = request.json.get('bookingId', '')
    data = get_db()
    booking = next((b for b in data.get('bookings', []) if b['id'] == booking_id), None)
    if not booking:
        return jsonify({'ok': False}), 404
    booking['status'] = 'cancelled'
    save_data(data)
    # Notify the member their booking was cancelled — admin-editable template.
    phone = _member_phone(data, booking.get('memberEmail',''), booking.get('memberName',''))
    if phone:
        bs  = data.get('bookingSettings', {})
        ctx = _booking_sms_ctx(data, booking)
        send_sms(phone, render_sms_template(
            bs.get('smsCancelTemplate', ''), ctx))
    return jsonify({'ok': True})

@app.route('/admin/api/bookable-resources')
@login_required
def admin_bookable_resources():
    """Used by the admin Bookings tab to populate the resource picker in the
    Add/Edit Booking modals. Returns the active occupant list (the only people
    bookings can be made for) — bookings always credit hours to the occupant's
    parent member account, so member-direct selection is intentionally absent."""
    data = get_db()
    occupants = sorted([o['name'] for o in data.get('occupants', [])
                        if o.get('status') == 'Active' and o.get('name')])
    return jsonify({
        'ok':        True,
        'resources': get_bookable_resources(data),
        'occupants': occupants,
    })

@app.route('/admin/api/booking-create', methods=['POST'])
@login_required
def admin_create_booking():
    """Admin creates a booking on behalf of an active occupant. Same conflict
    + overage gate as /book/create, but no auth-token check (relies on
    @login_required) and the occupant is chosen from the picker. Hours roll
    up to the occupant's parent member account."""
    occupant_name = request.json.get('memberName', '').strip()
    if not occupant_name:
        return jsonify({'ok': False, 'error': 'Occupant is required'}), 400

    data = get_db()
    # Look up active occupant by name. Members (companies) are not bookable
    # directly — the parent member is derived from occupant.company.
    occ = next((o for o in data.get('occupants', [])
                if o.get('name') == occupant_name and o.get('status') == 'Active'), None)
    if not occ:
        return jsonify({'ok': False, 'error': 'Occupant not found or not active'}), 404

    parent_member_name = (occ.get('company') or '').strip()
    parent_member = next((m for m in data.get('members', [])
                          if m.get('name') == parent_member_name), None) if parent_member_name else None
    if not parent_member:
        return jsonify({'ok': False, 'error':
            f'Occupant "{occupant_name}" is not linked to an active member account.'}), 400

    occupant_email = occ.get('email', '')

    date_str       = request.json.get('date', '')
    start_time     = request.json.get('start', '')
    end_time       = request.json.get('end', '')
    title          = request.json.get('title', 'Meeting')
    resource_id    = (request.json.get('resourceId') or CONFERENCE_ROOM_ID).strip()
    accept_overage = bool(request.json.get('acceptOverage', False))

    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date'}), 400

    bookable = get_bookable_resources(data)
    chosen   = next((r for r in bookable if r['id'] == resource_id), None)
    if not chosen:
        return jsonify({'ok': False, 'error': 'That space is not available for booking.'}), 400

    # Conflict check on the same resource.
    for b in data.get('bookings', []):
        if (b.get('date') == date_str
                and b.get('status') != 'cancelled'
                and b.get('resourceId') == resource_id
                and not (end_time <= b.get('start','') or start_time >= b.get('end',''))):
            return jsonify({'ok': False, 'error': 'That time slot is already booked.'})

    # Overage check — accounted at the parent-member level (all occupants
    # under one member share the same monthly hour bucket).
    booking_hours  = _booking_duration_hours({'start': start_time, 'end': end_time})
    hours_used     = get_member_hours_used(data, parent_member_name, dt.year, dt.month)
    hours_inc      = hours_included(data, parent_member_name)
    bs             = data.get('bookingSettings', {})
    overage_rate   = float(bs.get('overageRatePerHour', 25))
    hours_after    = hours_used + booking_hours
    hours_over     = max(0.0, hours_after - hours_inc)
    overage_charge = round(hours_over * overage_rate, 2)

    if hours_over > 0 and not accept_overage:
        return jsonify({
            'ok':                    False,
            'overage':               True,
            'hoursThisBooking':      round(booking_hours, 2),
            'hoursUsed':             round(hours_used, 2),
            'hoursIncluded':         hours_inc,
            'hoursOver':             round(hours_over, 2),
            'overageRate':           overage_rate,
            'overageCharge':         overage_charge,
            'overageWarningMessage': bs.get('overageWarningMessage', ''),
        })

    booking_id = '_' + secrets.token_hex(4)
    booking = {
        'id':           booking_id,
        # Booker = occupant. Hours roll up to parentMember.
        'memberName':   occupant_name,
        'memberEmail':  occupant_email,
        'parentMember': parent_member_name,
        'resourceType': chosen['type'],
        'resourceId':   chosen['id'],
        'date':         date_str,
        'year':         dt.year,
        'month':        dt.month,
        'start':        start_time,
        'end':          end_time,
        'title':        title,
        'status':       'confirmed',
        'createdAt':    datetime.now().isoformat(),
        'createdBy':    'admin',
    }
    if hours_over > 0:
        booking['overageHours']  = round(hours_over, 2)
        booking['overageRate']   = overage_rate
        booking['overageCharge'] = overage_charge
    data.setdefault('bookings', []).append(booking)
    save_data(data)

    # Confirmation SMS to the occupant who's actually using the space.
    phone = _member_phone(data, occupant_email, occupant_name)
    if phone:
        ctx = _booking_sms_ctx(data, booking, override_label=chosen['label'])
        send_sms(phone, render_sms_template(
            bs.get('smsConfirmationTemplate', ''), ctx))
    return jsonify({'ok': True, 'booking': booking})

@app.route('/admin/api/booking-edit', methods=['POST'])
@login_required
def admin_edit_booking():
    """Admin edits any booking. Reuses the shared edit logic; identical to
    /book/edit except no member-auth filter on the lookup."""
    booking_id = request.json.get('bookingId', '')
    data = get_db()
    booking = next((b for b in data.get('bookings', [])
                    if b['id'] == booking_id and b.get('status') != 'cancelled'), None)
    if not booking:
        return jsonify({'ok': False, 'error': 'Booking not found'}), 404

    new_date  = request.json.get('date',  booking['date'])
    new_start = request.json.get('start', booking['start'])
    new_end   = request.json.get('end',   booking['end'])
    new_title = request.json.get('title', booking.get('title', 'Meeting'))
    new_res   = (request.json.get('resourceId') or booking.get('resourceId') or CONFERENCE_ROOM_ID).strip()
    accept_overage = bool(request.json.get('acceptOverage', False))

    return _apply_booking_edit(data, booking, new_date, new_start, new_end,
                               new_title, new_res, accept_overage,
                               sms_kind='admin-edit')

# ── Monthly usage email (called by scheduler or manually) ────────────────────

@app.route('/admin/api/send-monthly-usage', methods=['POST'])
@login_required
def send_monthly_usage():
    """Compute per-member usage for a given month and return the rows so the
    admin UI can open Outlook drafts for each. App-side email is disabled
    (Notify → Outlook workflow), so this endpoint no longer sends — it just
    hands back the data the dashboard needs to compose drafts."""
    data  = get_db()
    month = int(request.json.get('month', datetime.now().month))
    year  = int(request.json.get('year',  datetime.now().year))
    month_name = datetime(year, month, 1).strftime('%B %Y')

    rows = []
    for member in data.get('members', []):
        if member.get('status') != 'Active':
            continue
        # Bookings billed to this member account, including any made by occupants
        # under it (those stamp `parentMember`; legacy bookings fall back to
        # `memberName`).
        member_bookings = [
            b for b in data.get('bookings', [])
            if _booking_billed_to(b) == member['name']
            and b.get('year') == year
            and b.get('month') == month
            and b.get('status') != 'cancelled'
        ]
        if not member_bookings:
            continue   # skip members with no usage that month

        total_minutes = 0
        for b in member_bookings:
            try:
                s = datetime.strptime(b['start'], '%H:%M')
                e = datetime.strptime(b['end'],   '%H:%M')
                total_minutes += int((e - s).total_seconds() / 60)
            except Exception:
                pass
        hours_used      = round(total_minutes / 60, 2)
        included        = hours_included(data, member['name'])
        hours_remaining = max(0, included - hours_used)
        hours_over      = max(0, hours_used - included)

        rows.append({
            'memberName':     member['name'],
            'memberEmail':    member.get('email', ''),
            'hoursUsed':      hours_used,
            'hoursIncluded':  included,
            'hoursRemaining': hours_remaining,
            'hoursOver':      hours_over,
            'bookings': [
                {
                    'date':  b['date'],
                    'start': b['start'],
                    'end':   b['end'],
                    'title': b.get('title', 'Meeting'),
                    'space': resource_label(data, b.get('resourceId', 'conference_room')),
                    'bookedBy': b.get('memberName', ''),
                }
                for b in sorted(member_bookings, key=lambda x: (x['date'], x['start']))
            ],
        })

    rows.sort(key=lambda r: r['memberName'].lower())
    return jsonify({
        'ok':        True,
        'month':     month,
        'year':      year,
        'monthName': month_name,
        'rows':      rows,
    })


# ── Analytics API ────────────────────────────────────────────────────────────

@app.route('/admin/api/analytics')
@login_required
def get_analytics():
    """Return GA4 website visit stats. Accepts ?days=7|30|90 and ?channel=X for drill-down."""
    ga_json = os.environ.get('GA_SERVICE_ACCOUNT_JSON', '')
    ga_property = os.environ.get('GA_PROPERTY_ID', '')

    if not ga_json or not ga_property:
        return get_analytics_builtin()

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Metric, Dimension, OrderBy,
            FilterExpression, Filter
        )
        from google.oauth2 import service_account

        days    = request.args.get('days', '30')
        channel = request.args.get('channel', '')  # drill-down channel name
        try:
            days_int = int(days)
            if days_int not in (7, 30, 90):
                days_int = 30
        except ValueError:
            days_int = 30

        date_start = f'{days_int}daysAgo'

        creds_dict = json.loads(ga_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/analytics.readonly']
        )
        client = BetaAnalyticsDataClient(credentials=creds)
        property_id = f"properties/{ga_property}"

        # Today's sessions (always fixed)
        today_resp = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[DateRange(start_date='today', end_date='today')],
            metrics=[Metric(name='sessions')],
        ))
        today = int(today_resp.rows[0].metric_values[0].value) if today_resp.rows else 0

        # Last 7 days
        week_resp = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[DateRange(start_date='7daysAgo', end_date='today')],
            metrics=[Metric(name='sessions')],
        ))
        week = int(week_resp.rows[0].metric_values[0].value) if week_resp.rows else 0

        # Selected range total
        range_resp = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[DateRange(start_date=date_start, end_date='today')],
            metrics=[Metric(name='sessions')],
        ))
        range_total = int(range_resp.rows[0].metric_values[0].value) if range_resp.rows else 0

        # Daily sparkline for selected range
        daily_resp = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[DateRange(start_date=date_start, end_date='today')],
            dimensions=[Dimension(name='date')],
            metrics=[Metric(name='sessions')],
            order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name='date'))],
        ))
        daily = []
        for row in daily_resp.rows:
            raw = row.dimension_values[0].value
            day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            daily.append({'day': day, 'views': int(row.metric_values[0].value)})

        # Channel groups OR drill-down sources
        if not channel:
            # Top-level: sessions by channel group
            ch_resp = client.run_report(RunReportRequest(
                property=property_id,
                date_ranges=[DateRange(start_date=date_start, end_date='today')],
                dimensions=[Dimension(name='sessionDefaultChannelGroup')],
                metrics=[Metric(name='sessions')],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name='sessions'), desc=True)],
            ))
            channels = [
                {'label': row.dimension_values[0].value,
                 'sessions': int(row.metric_values[0].value)}
                for row in ch_resp.rows
            ]
            sources = []
        else:
            # Drill-down: sessions by source within the selected channel
            channels = []
            # Direct has no meaningful source breakdown
            if channel.lower() == 'direct':
                sources = []
            else:
                dim = 'sessionSource'
                src_resp = client.run_report(RunReportRequest(
                    property=property_id,
                    date_ranges=[DateRange(start_date=date_start, end_date='today')],
                    dimensions=[Dimension(name=dim)],
                    metrics=[Metric(name='sessions')],
                    dimension_filter=FilterExpression(
                        filter=Filter(
                            field_name='sessionDefaultChannelGroup',
                            string_filter=Filter.StringFilter(value=channel)
                        )
                    ),
                    order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name='sessions'), desc=True)],
                    limit=10,
                ))
                sources = [
                    {'label': row.dimension_values[0].value or '(direct)',
                     'sessions': int(row.metric_values[0].value)}
                    for row in src_resp.rows
                ]

        return jsonify({
            'ok': True,
            'source': 'ga4',
            'today': today,
            'week': week,
            'range_total': range_total,
            'days': days_int,
            'daily': daily,
            'channels': channels,
            'sources': sources,
            'channel': channel,
        })

    except Exception as e:
        print(f"[GA4 ERROR] {e}")
        return get_analytics_builtin()


def get_analytics_builtin():
    """Fall back to built-in page tracking."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                now = datetime.now()
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                week_start  = today_start - timedelta(days=7)
                month_start = today_start - timedelta(days=30)

                cur.execute("SELECT COUNT(*) FROM qbix_pageviews WHERE visited_at >= %s", (today_start,))
                today = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM qbix_pageviews WHERE visited_at >= %s", (week_start,))
                week = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM qbix_pageviews WHERE visited_at >= %s", (month_start,))
                month = cur.fetchone()[0]
                cur.execute("""
                    SELECT DATE(visited_at) as day, COUNT(*) as views
                    FROM qbix_pageviews WHERE visited_at >= %s
                    GROUP BY DATE(visited_at) ORDER BY day
                """, (month_start,))
                daily = [{'day': str(r[0]), 'views': r[1]} for r in cur.fetchall()]
                cur.execute("""
                    SELECT path, COUNT(*) as views FROM qbix_pageviews
                    WHERE visited_at >= %s GROUP BY path ORDER BY views DESC LIMIT 5
                """, (month_start,))
                top_pages = [{'path': r[0], 'views': r[1]} for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) FROM qbix_pageviews")
                total = cur.fetchone()[0]

        return jsonify({'ok': True, 'source': 'builtin', 'today': today, 'week': week,
                       'month': month, 'total': total, 'daily': daily, 'top_pages': top_pages})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Setup route (first run only) ─────────────────────────────────────────────

@app.context_processor
def inject_now():
    return {'now': datetime.now()}


# ══════════════════════════════════════════════════════════════════════════════
# CLOUDINARY — Media Library
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/api/media', methods=['GET'])
@login_required
def media_list():
    """Return all assets in the qbix folder from Cloudinary."""
    if not _cloudinary_available:
        return jsonify({'ok': False, 'error': 'Cloudinary not installed'}), 500
    try:
        result = cloudinary.api.resources(
            type='upload',
            prefix='qbix/',
            max_results=200,
            context=True,
            tags=True
        )
        assets = []
        for r in result.get('resources', []):
            ctx = r.get('context', {}).get('custom', {})
            parts = r['public_id'].split('/')
            folder = parts[1] if len(parts) > 1 else 'general'
            assets.append({
                'public_id': r['public_id'],
                'url':       r['secure_url'],
                'width':     r.get('width', 0),
                'height':    r.get('height', 0),
                'bytes':     r.get('bytes', 0),
                'format':    r.get('format', ''),
                'created':   r.get('created_at', ''),
                'alt':       ctx.get('alt', ''),
                'caption':   ctx.get('caption', ''),
                'folder':    folder,
            })
        assets.sort(key=lambda x: x['created'], reverse=True)
        return jsonify({'ok': True, 'assets': assets})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/api/media/upload', methods=['POST'])
@login_required
def media_upload():
    """Upload an image to Cloudinary. Accepts multipart file or base64 data URI."""
    if not _cloudinary_available:
        return jsonify({'ok': False, 'error': 'Cloudinary not installed'}), 500
    try:
        folder  = request.form.get('folder', 'general')
        alt     = request.form.get('alt', '')
        caption = request.form.get('caption', '')

        ctx_parts = []
        if alt:     ctx_parts.append(f'alt={alt}')
        if caption: ctx_parts.append(f'caption={caption}')
        ctx_str = '|'.join(ctx_parts) if ctx_parts else None

        upload_opts = {
            'folder':          f'qbix/{folder}',
            'use_filename':    True,
            'unique_filename': True,
            'overwrite':       False,
            'resource_type':   'image',
            'format':          'webp',
            'transformation':  [{'quality': 'auto', 'fetch_format': 'webp'}],
        }
        if ctx_str:
            upload_opts['context'] = ctx_str

        if 'file' in request.files:
            f = request.files['file']
            result = cloudinary.uploader.upload(f, **upload_opts)
        elif request.is_json and request.json.get('data'):
            j = request.json
            alt     = j.get('alt', alt)
            caption = j.get('caption', caption)
            folder  = j.get('folder', folder)
            upload_opts['folder'] = f'qbix/{folder}'
            ctx_parts = []
            if alt:     ctx_parts.append(f'alt={alt}')
            if caption: ctx_parts.append(f'caption={caption}')
            if ctx_parts:
                upload_opts['context'] = '|'.join(ctx_parts)
            result = cloudinary.uploader.upload(j['data'], **upload_opts)
        else:
            return jsonify({'ok': False, 'error': 'No file provided'}), 400

        ctx = result.get('context', {}).get('custom', {})
        return jsonify({
            'ok':        True,
            'public_id': result['public_id'],
            'url':       result['secure_url'],
            'width':     result.get('width', 0),
            'height':    result.get('height', 0),
            'bytes':     result.get('bytes', 0),
            'format':    result.get('format', 'webp'),
            'alt':       ctx.get('alt', alt),
            'caption':   ctx.get('caption', caption),
            'folder':    folder,
        })
    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error': str(e), 'detail': traceback.format_exc()[-400:]}), 500


@app.route('/admin/api/media/update', methods=['POST'])
@login_required
def media_update():
    """Update alt text and caption for a Cloudinary asset."""
    if not _cloudinary_available:
        return jsonify({'ok': False, 'error': 'Cloudinary not installed'}), 500
    try:
        public_id = request.json.get('public_id', '')
        alt       = request.json.get('alt', '')
        caption   = request.json.get('caption', '')
        ctx_parts = []
        if alt:     ctx_parts.append(f'alt={alt}')
        if caption: ctx_parts.append(f'caption={caption}')
        ctx_str = '|'.join(ctx_parts) if ctx_parts else 'alt='
        cloudinary.uploader.explicit(public_id, type='upload', context=ctx_str)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/api/media/delete', methods=['POST'])
@login_required
def media_delete():
    """Permanently delete a Cloudinary asset."""
    if not _cloudinary_available:
        return jsonify({'ok': False, 'error': 'Cloudinary not installed'}), 500
    try:
        public_id = request.json.get('public_id', '')
        if not public_id:
            return jsonify({'ok': False, 'error': 'public_id required'}), 400
        cloudinary.uploader.destroy(public_id)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/api/media/suggest-alt', methods=['POST'])
@login_required
def media_suggest_alt():
    """
    Use Claude to suggest alt text for a newly uploaded photo.
    Accepts: { url, public_id, context_type ('office'|'news'), office_info{}, draft_subject, draft_body, seo_keywords[] }
    Returns: { ok, suggested_alt, suggested_caption }
    """
    if not ANTHROPIC_API_KEY:
        return jsonify({'ok': False, 'error': 'Anthropic API key not configured'}), 400
    try:
        import urllib.request as _ureq
        import json as _json
        import re as _re

        body        = request.json or {}
        ctx_type    = body.get('context_type', 'office')   # 'office' or 'news'
        office_info = body.get('office_info', {})           # {num, sqft, dormer, description, status}
        subject     = body.get('draft_subject', '')
        draft_body  = body.get('draft_body', '')
        seo_kws     = body.get('seo_keywords', [])

        kw_str = ', '.join(seo_kws[:5]) if seo_kws else 'north Macon office space, Northside Crossing, private office Macon GA'

        if ctx_type == 'office':
            num   = office_info.get('num', '')
            sqft  = office_info.get('sqft', '')
            dormer = office_info.get('dormer', '')
            desc  = office_info.get('description', '')
            status = office_info.get('status', 'Vacant')
            size_str = f'{sqft} sq ft' if sqft else ''
            if dormer: size_str += f' + {dormer} sq ft dormer'
            prompt = (
                f'Write a concise, SEO-optimised alt text (max 125 characters) for a photo of Office {num} '
                f'at Qbix Centre, a professional coworking space at 500A Northside Crossing in north Macon, GA. '
                + (f'Office size: {size_str}. ' if size_str else '')
                + (f'Description: {desc}. ' if desc else '')
                + f'Status: {status}. '
                f'Naturally include 1-2 of these SEO keywords where they fit: {kw_str}. '
                f'Also write a short caption (max 80 characters) for use under the photo. '
                f'Return ONLY valid JSON: {{"alt": "...", "caption": "..."}} — no markdown, no extra text.'
            )
        else:  # news
            clean = _re.sub(r'<[^>]+>', ' ', draft_body)
            clean = _re.sub(r'\s+', ' ', clean).strip()[:800]
            prompt = (
                f'Write a concise, SEO-optimised alt text (max 125 characters) for a photo used in a '
                f'Qbix Centre newsletter post. '
                f'Post subject: "{subject}". '
                + (f'Post content summary: {clean[:400]}. ' if clean else '')
                + f'Qbix Centre is a professional coworking space in north Macon GA at Northside Crossing. '
                f'Naturally include 1-2 of these SEO keywords: {kw_str}. '
                f'Also write a short caption (max 80 characters). '
                f'Return ONLY valid JSON: {{"alt": "...", "caption": "..."}} — no markdown, no extra text.'
            )

        payload = {
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 200,
            'messages': [{'role': 'user', 'content': prompt}]
        }
        req = _ureq.Request(
            'https://api.anthropic.com/v1/messages',
            data=_json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'}
        )
        with _ureq.urlopen(req, timeout=20) as resp:
            result = _json.loads(resp.read())
            raw = result['content'][0]['text'].strip()
            raw = raw.replace('```json','').replace('```','').strip()
            parsed = _json.loads(raw)
            return jsonify({'ok': True, 'suggested_alt': parsed.get('alt',''), 'suggested_caption': parsed.get('caption','')})

    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error': str(e), 'detail': traceback.format_exc()[-300:]}), 500


@app.route('/admin/api/media/update-news-alt', methods=['POST'])
@login_required
def update_news_alt():
    """
    One-shot: update alt text + caption on all news post photos in Cloudinary.
    Reads posts from DB, matches to the provided alt_map, updates Cloudinary metadata.
    """
    if not _cloudinary_available:
        return jsonify({'ok': False, 'error': 'Cloudinary not installed'}), 500
    try:
        alt_map = request.json.get('alt_map', {})
        data    = get_db()
        updated = 0
        errors  = []

        for post in data.get('newsletter', []):
            subject = post.get('subject', '')
            mapping = alt_map.get(subject)
            if not mapping:
                continue

            hero_alt   = mapping.get('heroAlt', '')
            caption    = mapping.get('caption', '')
            gallery_alts = mapping.get('galleryAlts', [])

            # Hero photo
            hero = post.get('heroPhoto')
            if hero and isinstance(hero, dict) and hero.get('public_id'):
                try:
                    ctx = f'alt={hero_alt}'
                    if caption:
                        ctx += f'|caption={caption}'
                    cloudinary.uploader.explicit(hero['public_id'], type='upload', context=ctx)
                    hero['alt'] = hero_alt
                    updated += 1
                except Exception as e:
                    errors.append(f'{subject} hero: {str(e)[:100]}')

            # Gallery photos
            gallery = post.get('galleryPhotos', [])
            for i, photo in enumerate(gallery):
                if not (isinstance(photo, dict) and photo.get('public_id')):
                    continue
                alt = gallery_alts[i] if i < len(gallery_alts) else hero_alt
                try:
                    ctx = f'alt={alt}'
                    if caption:
                        ctx += f'|caption={caption}'
                    cloudinary.uploader.explicit(photo['public_id'], type='upload', context=ctx)
                    photo['alt'] = alt
                    updated += 1
                except Exception as e:
                    errors.append(f'{subject} gallery[{i}]: {str(e)[:100]}')

        save_data(data)
        return jsonify({
            'ok': True,
            'updated': updated,
            'errors': errors,
            'message': f'{updated} news photos updated in Cloudinary. {len(errors)} errors.'
        })
    except Exception as e:
        import traceback
        return jsonify({'ok': False, 'error': str(e), 'detail': traceback.format_exc()[-400:]}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8765))
    app.run(host='0.0.0.0', port=port, debug=False)