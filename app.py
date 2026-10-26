"""Velvet & Vivid Creations: self-hosted booking and salon operations.
Python 3.10+; no external runtime dependencies. Single-process SQLite backend.
See README.md before exposing to the public internet.
"""
import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', str(ROOT / 'data'))).resolve()
MEDIA = DATA / 'uploads'
DB = DATA / 'salon.sqlite3'
SITE = os.getenv('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
TZ = ZoneInfo('America/New_York')
BUSINESS = 'Velvet & Vivid Creations'
PORT = int(os.getenv('PORT', '8000'))
SECURE_COOKIES = SITE.startswith('https://')
JSON_LIMIT = 9 * 1024 * 1024
STATUSES = ('PENDING', 'APPROVED', 'DECLINED', 'CANCELLED', 'COMPLETED', 'NO_SHOW')
METHODS = ('cash', 'card_external', 'zelle', 'venmo', 'gift_card', 'other')

DATA.mkdir(parents=True, exist_ok=True)
MEDIA.mkdir(parents=True, exist_ok=True)


@contextmanager
def conn():
    c = sqlite3.connect(DB, timeout=15)
    try:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=15000')
        with c:
            yield c
    finally:
        c.close()


def asdict(row):
    return dict(row) if row is not None else None


def init_db():
    with conn() as d:
        d.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL, phone TEXT DEFAULT '', password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('ADMIN','CLIENT')), created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS hours (
          weekday INTEGER PRIMARY KEY, enabled INTEGER NOT NULL, open_min INTEGER NOT NULL,
          close_min INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS services (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT 'Hair artistry', price_cents INTEGER NOT NULL,
          duration INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS clients (
          id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
          name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
          preferences TEXT NOT NULL DEFAULT '', allergies TEXT NOT NULL DEFAULT '',
          formula TEXT NOT NULL DEFAULT '', contact_consent INTEGER NOT NULL DEFAULT 0,
          photo_consent INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS appointments (
          id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
          service_id INTEGER NOT NULL REFERENCES services(id), start_ts INTEGER NOT NULL,
          end_ts INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
          message TEXT NOT NULL DEFAULT '', price_cents INTEGER NOT NULL, created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_appt_start ON appointments(start_ts,end_ts,status);
        CREATE TABLE IF NOT EXISTS blocks (
          id INTEGER PRIMARY KEY, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
          reason TEXT NOT NULL DEFAULT 'Unavailable');
        CREATE TABLE IF NOT EXISTS client_notes (
          id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
          appointment_id INTEGER REFERENCES appointments(id), note TEXT NOT NULL,
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS photos (
          id INTEGER PRIMARY KEY, client_id INTEGER REFERENCES clients(id),
          filename TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '',
          public INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices (
          id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
          appointment_id INTEGER REFERENCES appointments(id),
          description TEXT NOT NULL, subtotal_cents INTEGER NOT NULL,
          tax_cents INTEGER NOT NULL DEFAULT 0, discount_cents INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS payments (
          id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
          amount_cents INTEGER NOT NULL, tip_cents INTEGER NOT NULL DEFAULT 0,
          method TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '',
          gift_card_id INTEGER REFERENCES gift_cards(id), stripe_id TEXT UNIQUE,
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS coupons (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE,
          percent INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS gift_cards (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE,
          balance_cents INTEGER NOT NULL, issued_cents INTEGER NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS inventory (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 0,
          reorder_level INTEGER NOT NULL DEFAULT 0, unit_cost_cents INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS expenses (
          id INTEGER PRIMARY KEY, description TEXT NOT NULL, category TEXT NOT NULL,
          amount_cents INTEGER NOT NULL, incurred_ts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY, appointment_id INTEGER NOT NULL REFERENCES appointments(id),
          type TEXT NOT NULL, channel TEXT NOT NULL, sent_at INTEGER NOT NULL,
          UNIQUE(appointment_id,type,channel));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, actor_id INTEGER, event TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS password_resets (
          hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS inquiries (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
          message TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS throttles (
          key TEXT PRIMARY KEY, count INTEGER NOT NULL, reset_at INTEGER NOT NULL);
        ''')
        if d.execute('SELECT COUNT(*) FROM hours').fetchone()[0] == 0:
            d.executemany('INSERT INTO hours(weekday,enabled,open_min,close_min) VALUES(?,?,?,?)',
                          [(day,int(day in (1,2,3,4,5)),540,1080) for day in range(7)])
        if d.execute('SELECT COUNT(*) FROM services').fetchone()[0] == 0:
            d.executemany('INSERT INTO services(name,description,category,price_cents,duration) VALUES(?,?,?,?,?)', [
              ('Signature haircut & style', 'A tailored haircut, finish and consultation.', 'Cuts & styling', 8500, 60),
              ('Custom hair artistry', 'A personalized color transformation. Final price at consultation.', 'Color artistry', 18500, 180),
              ('Dimensional balayage', 'Soft lived-in dimension and blended luminosity.', 'Color artistry', 23500, 210),
              ('Gloss & conditioning', 'Tone, shine and restorative hair care.', 'Treatments', 9500, 75),
              ('Root touch-up', 'Color refresh and polished finish.', 'Color artistry', 11500, 120),
              ('Consultation', 'Plan your hair goals and upcoming appointment.', 'Consultations', 0, 30),
            ])


def now():
    return int(time.time())


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()


def password_ok(password, stored):
    try:
        salt_hex, digest_hex = stored.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def admin_totp_ok(code):
    secret = os.getenv('ADMIN_TOTP_SECRET', '').strip().replace(' ', '').upper()
    if not secret:
        return not (os.getenv('REQUIRE_ADMIN_2FA', '0') == '1' or SECURE_COOKIES)
    try:
        key = base64.b32decode(secret + '=' * (-len(secret) % 8))
        for interval in (-1, 0, 1):
            counter = int(time.time() // 30) + interval
            mac = hmac.new(key, counter.to_bytes(8, 'big'), hashlib.sha1).digest()
            offset = mac[-1] & 15
            otp = (int.from_bytes(mac[offset:offset+4], 'big') & 0x7fffffff) % 1000000
            if hmac.compare_digest(f'{otp:06}', str(code)):
                return True
        return False
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def clean(value, limit=300):
    if not isinstance(value, str):
        raise ApiError(400, 'Invalid text field')
    s = value.strip()
    if len(s) > limit:
        raise ApiError(400, f'Text exceeds {limit} characters')
    return s


def email_address(v):
    x = clean(v, 254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', x):
        raise ApiError(400, 'Enter a valid email address')
    return x


def integer(v, minimum=None, maximum=None):
    try:
        if isinstance(v, bool):
            raise ValueError()
        i = int(v)
    except (ValueError, TypeError):
        raise ApiError(400, 'Enter a valid number')
    if (minimum is not None and i < minimum) or (maximum is not None and i > maximum):
        raise ApiError(400, 'Number is out of range')
    return i


def timestamp(v):
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            # UI submits times with local offset; never interpret an ambiguous naive timestamp.
            raise ValueError()
        return int(dt.timestamp())
    except (ValueError, TypeError, AttributeError):
        raise ApiError(400, 'Invalid appointment date/time (timezone required)')


def localiso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec='minutes')


def slot_allowed(start, duration, d=None):
    dt = datetime.fromtimestamp(start, TZ)
    end = dt + timedelta(minutes=duration)
    if dt.minute not in (0, 30) or dt.second != 0 or dt.date() != end.date():
        return False
    if start < now() + 60 * 60 * 2 or start > now() + 90 * 86400:
        return False
    if d is None:
        with conn() as temp:
            h = temp.execute('SELECT * FROM hours WHERE weekday=?',(dt.weekday(),)).fetchone()
    else:
        h = d.execute('SELECT * FROM hours WHERE weekday=?',(dt.weekday(),)).fetchone()
    if not h or not h['enabled']:
        return False
    start_min=dt.hour*60+dt.minute
    end_min=end.hour*60+end.minute
    return h['open_min'] <= start_min and end_min <= h['close_min']


def overlaps(d, start, end, exclude_id=None):
    args = [end, start]
    clause = ''
    if exclude_id:
        clause = ' AND id != ?'
        args.append(exclude_id)
    booked = d.execute('SELECT 1 FROM appointments WHERE status IN (\'PENDING\',\'APPROVED\') AND start_ts < ? AND end_ts > ?' + clause + ' LIMIT 1', args).fetchone()
    blocked = d.execute('SELECT 1 FROM blocks WHERE start_ts < ? AND end_ts > ? LIMIT 1', (end, start)).fetchone()
    return bool(booked or blocked)


def audit(d, actor, event, detail=''):
    d.execute('INSERT INTO audit(actor_id,event,detail,created_at) VALUES(?,?,?,?)', (actor, event, detail[:300], now()))


def throttle(key, limit, window):
    t = now()
    with conn() as d:
        d.execute('BEGIN IMMEDIATE')
        row = d.execute('SELECT count,reset_at FROM throttles WHERE key=?', (key,)).fetchone()
        if row and row['reset_at'] > t:
            if row['count'] >= limit:
                raise ApiError(429, 'Too many requests. Please try again later.')
            d.execute('UPDATE throttles SET count=count+1 WHERE key=?', (key,))
        else:
            d.execute('INSERT INTO throttles(key,count,reset_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET count=1,reset_at=excluded.reset_at', (key,1,t+window))


def send_email(to, subject, body):
    if not (os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM')):
        return False
    msg = EmailMessage()
    msg['From'] = os.environ['SMTP_FROM']
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT', '587')), timeout=15) as smtp:
        if os.getenv('SMTP_STARTTLS', '1') == '1':
            smtp.starttls()
        if os.getenv('SMTP_USER'):
            smtp.login(os.environ['SMTP_USER'], os.getenv('SMTP_PASSWORD', ''))
        smtp.send_message(msg)
    return True


def send_sms(to, body):
    sid = os.getenv('TWILIO_ACCOUNT_SID')
    token = os.getenv('TWILIO_AUTH_TOKEN')
    from_phone = os.getenv('TWILIO_FROM')
    if not all((sid, token, from_phone)) or not to:
        return False
    digits=re.sub(r'\D','',to)
    if len(digits)==10: to='+1'+digits
    elif len(digits)==11 and digits.startswith('1'): to='+'+digits
    elif not re.fullmatch(r'\+[1-9][0-9]{7,14}',to): return False
    form = urllib.parse.urlencode({'To':to,'From':from_phone,'Body':body}).encode()
    req = urllib.request.Request(f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json', data=form)
    req.add_header('Authorization', 'Basic ' + base64.b64encode(f'{sid}:{token}'.encode()).decode())
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status in (200, 201)


def notify_appointment(appt_id, kind):
    with conn() as d:
        a = d.execute('''SELECT a.*,c.name,c.email,c.phone,c.contact_consent,s.name AS service_name
          FROM appointments a JOIN clients c ON c.id=a.client_id
          JOIN services s ON s.id=a.service_id WHERE a.id=?''', (appt_id,)).fetchone()
        if not a:
            return
    date = datetime.fromtimestamp(a['start_ts'],TZ).strftime('%A, %B %d at %I:%M %p')
    body = f'{BUSINESS}\n\n{a["name"]}, your {a["service_name"]} appointment on {date} is {kind}.\nMelbourne, Florida.\n\nFor changes, contact the salon.'
    if kind == 'requested':
        body = f'{BUSINESS}\n\nYour request for {a["service_name"]} on {date} was received. It is NOT confirmed until approved.\n\nFor changes, contact the salon.'
    if a['email']:
        try:
            send_email(a['email'], f'Appointment {kind} | {BUSINESS}', body)
        except Exception as e:
            print('Email delivery failed:', type(e).__name__, file=sys.stderr)
    if a['contact_consent'] and a['phone']:
        try:
            send_sms(a['phone'], body[:500])
        except Exception as e:
            print('SMS delivery failed:', type(e).__name__, file=sys.stderr)
    if kind == 'requested' and os.getenv('ADMIN_NOTIFY_EMAIL'):
        try:
            send_email(os.environ['ADMIN_NOTIFY_EMAIL'], 'New appointment request', f'New request #{appt_id}\n{date}\nReview in admin dashboard.')
        except Exception:
            pass


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def invoice_summary(d, invoice_id):
    r = d.execute('''SELECT i.*,c.name AS client_name,c.email,
      COALESCE((SELECT SUM(amount_cents) FROM payments p WHERE p.invoice_id=i.id),0) AS paid_cents,
      COALESCE((SELECT SUM(tip_cents) FROM payments p WHERE p.invoice_id=i.id),0) AS tips_cents
      FROM invoices i JOIN clients c ON c.id=i.client_id WHERE i.id=?''',(invoice_id,)).fetchone()
    if not r:
        raise ApiError(404, 'Invoice not found')
    obj = dict(r)
    obj['total_cents'] = max(0,obj['subtotal_cents']+obj['tax_cents']-obj['discount_cents'])
    obj['due_cents'] = max(0,obj['total_cents']-obj['paid_cents'])
    return obj


class SiteHandler(BaseHTTPRequestHandler):
    server_version = 'VelvetVivid/1.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Avoid logging full URLs containing possible personally identifiable data.
        print(f'{self.address_string()} - {fmt % args}',file=sys.stderr)

    def respond_headers(self, status=200, kind='application/json; charset=utf-8', length=None, cache=False, extra=None):
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy',"default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'")
        self.send_header('Cache-Control','public, max-age=86400' if cache else 'no-store')
        if SECURE_COOKIES:
            self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
        if length is not None:
            self.send_header('Content-Length',str(length))
        if extra:
            for h,v in extra:
                self.send_header(h,v)
        self.end_headers()

    def json(self, payload, status=200, cookies=None):
        data = json.dumps(payload, separators=(',',':'),ensure_ascii=False).encode()
        self.respond_headers(status, length=len(data), extra=cookies)
        self.wfile.write(data)

    def error_json(self, e):
        self.json({'error': e.message}, e.status)

    def cookies(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get('Cookie',''))
        except Exception:
            pass
        return c

    def client_ip(self):
        if os.getenv('TRUST_PROXY_IP_HEADERS')=='1':
            forwarded=self.headers.get('X-Forwarded-For','').split(',')[0].strip()
            if re.fullmatch(r'[A-Fa-f0-9:.]{3,45}',forwarded):
                return forwarded
        return self.client_address[0]

    def cookie(self, name):
        return self.cookies().get(name).value if self.cookies().get(name) else None

    def setcookie(self, name, value, http_only=False, max_age=86400):
        attrs = f'{name}={value}; Path=/; SameSite=Lax; Max-Age={max_age}'
        if http_only:
            attrs += '; HttpOnly'
        if SECURE_COOKIES:
            attrs += '; Secure'
        return ('Set-Cookie',attrs)

    def csrf(self):
        x = self.headers.get('X-CSRF-Token','')
        y = self.cookie('vv_csrf')
        if not y or not x or not hmac.compare_digest(x,y):
            raise ApiError(403,'Security token expired. Refresh the page.')
        origin = self.headers.get('Origin')
        if origin and origin.rstrip('/') != SITE:
            # For local testing only the configured PUBLIC_BASE_URL is accepted.
            raise ApiError(403,'Request origin is not allowed')
        if not origin and self.headers.get('Sec-Fetch-Site','same-origin') not in ('same-origin','none'):
            raise ApiError(403,'Cross-origin request is not allowed')

    def user(self):
        token = self.cookie('vv_session')
        if not token or not re.fullmatch('[a-f0-9]{64}', token):
            return None
        with conn() as d:
            r = d.execute('''SELECT u.id,u.name,u.email,u.role FROM sessions s
                JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?''',
                (hashlib.sha256(token.encode()).hexdigest(),now())).fetchone()
            return asdict(r)

    def admin(self):
        u = self.user()
        if not u or u['role'] != 'ADMIN':
            raise ApiError(403,'Stylist account required')
        return u

    def signed_in(self):
        u = self.user()
        if not u:
            raise ApiError(401,'Sign in required')
        return u

    def request_json(self):
        try:
            length = int(self.headers.get('Content-Length','0'))
        except ValueError:
            raise ApiError(400,'Invalid request')
        if length < 0 or length > JSON_LIMIT:
            raise ApiError(413,'Request is too large')
        if 'application/json' not in self.headers.get('Content-Type',''):
            raise ApiError(415,'JSON request required')
        try:
            v = json.loads(self.rfile.read(length))
            if not isinstance(v,dict):
                raise ValueError()
            return v
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400,'Malformed JSON')

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path.startswith('/api/'):
                self.api_get(path)
            elif path.startswith('/media/'):
                self.media_get(path)
            else:
                self.static_get(path)
        except ApiError as e:
            self.error_json(e)
        except Exception as e:
            print('GET ERROR',repr(e),file=sys.stderr)
            self.json({'error':'Internal server error'},500)

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == '/api/stripe/webhook':
                self.stripe_webhook()
                return
            if not path.startswith('/api/'):
                raise ApiError(404,'Not found')
            self.csrf()
            v = self.request_json()
            self.api_post(path, v)
        except ApiError as e:
            self.error_json(e)
        except sqlite3.IntegrityError:
            self.json({'error':'That entry already exists or conflicts with another record'},409)
        except Exception as e:
            print('POST ERROR',repr(e),file=sys.stderr)
            self.json({'error':'Internal server error'},500)

    def static_get(self,path):
        allowed = {'/':'index.html','/index.html':'index.html','/styles.css':'styles.css',
                   '/site.js':'site.js','/velvet-vivid-hero.webp':'public/velvet-vivid-hero.webp',
                   '/velvet-texture.webp':'public/velvet-texture.webp','/favicon.svg':'public/favicon.svg'}
        filename=allowed.get(path)
        if not filename:
            raise ApiError(404,'Not found')
        p=ROOT/filename
        if not p.exists():
            raise ApiError(404,'Not found')
        typ=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        if typ=='text/javascript': typ='application/javascript'
        raw=p.read_bytes()
        self.respond_headers(200,typ,len(raw),cache=path not in ('/','/index.html'))
        self.wfile.write(raw)

    def media_get(self,path):
        m=re.fullmatch(r'/media/(\d+)',path)
        if not m:
            raise ApiError(404,'Not found')
        u=self.user()
        with conn() as d:
            row=d.execute('SELECT * FROM photos WHERE id=?',(int(m.group(1)),)).fetchone()
        if not row or not (row['public'] or (u and u['role']=='ADMIN')):
            raise ApiError(404,'Not found')
        fn=row['filename']
        if not re.fullmatch(r'[a-f0-9]{32}\.webp',fn):
            raise ApiError(404,'Not found')
        p=MEDIA/fn
        if not p.exists():
            raise ApiError(404,'Not found')
        raw=p.read_bytes()
        self.respond_headers(200,'image/webp',len(raw),cache=False)
        self.wfile.write(raw)

    def api_get(self,path):
        q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path=='/api/bootstrap':
            csrf=self.cookie('vv_csrf')
            newcookie=[]
            if not csrf or not re.fullmatch('[a-f0-9]{48}',csrf):
                csrf=secrets.token_hex(24)
                newcookie=[self.setcookie('vv_csrf',csrf)]
            u=self.user()
            self.json({'csrf':csrf,'user':u,'business':BUSINESS,'timezone':'America/New_York',
                       'stripe_enabled':bool(os.getenv('STRIPE_SECRET_KEY'))},cookies=newcookie)
            return
        if path=='/api/services':
            with conn() as d:
                rows=d.execute('SELECT * FROM services WHERE active=1 ORDER BY category,name').fetchall()
            self.json({'services':[dict(r) for r in rows]});return
        if path=='/api/gallery':
            with conn() as d:
                rows=d.execute('SELECT id,caption,created_at FROM photos WHERE public=1 ORDER BY id DESC LIMIT 100').fetchall()
            self.json({'photos':[dict(r) for r in rows]});return
        if path=='/api/availability':
            service_id=integer(q.get('service_id',['0'])[0],1)
            day=clean(q.get('date',[''])[0],10)
            try:
                date=datetime.strptime(day,'%Y-%m-%d').date()
            except ValueError:
                raise ApiError(400,'Invalid date')
            with conn() as d:
                s=d.execute('SELECT * FROM services WHERE id=? AND active=1',(service_id,)).fetchone()
                if not s:
                    raise ApiError(404,'Service unavailable')
                slots=[]
                for h in range(9,18):
                    for minute in (0,30):
                        local=datetime(date.year,date.month,date.day,h,minute,tzinfo=TZ)
                        t=int(local.timestamp())
                        if slot_allowed(t,s['duration'],d) and not overlaps(d,t,t+s['duration']*60):
                            slots.append({'start':local.isoformat(timespec='minutes'),'time':local.strftime('%I:%M %p').lstrip('0')})
            self.json({'slots':slots});return
        if path=='/api/my/profile':
            u=self.signed_in()
            with conn() as d:
                r=d.execute('SELECT name,email,phone FROM users WHERE id=?',(u['id'],)).fetchone()
            self.json({'profile':dict(r)});return
        if path=='/api/my/appointments':
            u=self.signed_in()
            with conn() as d:
                rows=d.execute('''SELECT a.id,a.status,a.start_ts,a.end_ts,s.name AS service_name,a.price_cents
                   FROM appointments a JOIN clients c ON c.id=a.client_id JOIN services s ON s.id=a.service_id
                   WHERE c.user_id=? ORDER BY a.start_ts DESC LIMIT 100''',(u['id'],)).fetchall()
            self.json({'appointments':[dict(r) for r in rows]});return
        if path=='/api/admin/overview':
            self.admin()
            with conn() as d:
                t=now()
                midnight=datetime.now(TZ).replace(hour=0,minute=0,second=0,microsecond=0)
                a=int(midnight.timestamp());b=int((midnight+timedelta(days=1)).timestamp())
                bookings=d.execute('SELECT COUNT(*) FROM appointments WHERE start_ts>=? AND start_ts<? AND status NOT IN (\'DECLINED\',\'CANCELLED\')',(a,b)).fetchone()[0]
                pending=d.execute('SELECT COUNT(*) FROM appointments WHERE status=\'PENDING\'').fetchone()[0]
                clients=d.execute('SELECT COUNT(*) FROM clients').fetchone()[0]
                thismonth=int(midnight.replace(day=1).timestamp())
                revenue=d.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE created_at>=?',(thismonth,)).fetchone()[0]
                upcoming=d.execute('''SELECT a.*,c.name AS client_name,s.name AS service_name FROM appointments a
                  JOIN clients c ON c.id=a.client_id JOIN services s ON s.id=a.service_id
                  WHERE a.start_ts>=? AND a.status IN ('PENDING','APPROVED') ORDER BY a.start_ts LIMIT 12''',(t,)).fetchall()
            self.json({'today':bookings,'pending':pending,'clients':clients,'revenue_cents':revenue,'upcoming':[dict(r) for r in upcoming]});return
        if path=='/api/admin/appointments':
            self.admin()
            with conn() as d:
                rows=d.execute('''SELECT a.*,c.name AS client_name,c.email,c.phone,s.name AS service_name
                  FROM appointments a JOIN clients c ON c.id=a.client_id JOIN services s ON s.id=a.service_id
                  ORDER BY a.start_ts DESC LIMIT 600''').fetchall()
            self.json({'appointments':[dict(r) for r in rows]});return
        if path=='/api/admin/clients':
            self.admin()
            with conn() as d:
                rows=d.execute('''SELECT c.*, (SELECT COUNT(*) FROM appointments a WHERE a.client_id=c.id) AS visits
                  FROM clients c ORDER BY c.created_at DESC LIMIT 1000''').fetchall()
            self.json({'clients':[dict(r) for r in rows]});return
        if path=='/api/admin/client':
            self.admin()
            cid=integer(q.get('id',['0'])[0],1)
            with conn() as d:
                c=d.execute('SELECT * FROM clients WHERE id=?',(cid,)).fetchone()
                if not c:raise ApiError(404,'Client not found')
                history=d.execute('''SELECT a.*,s.name AS service_name FROM appointments a JOIN services s ON s.id=a.service_id
                      WHERE a.client_id=? ORDER BY a.start_ts DESC''',(cid,)).fetchall()
                notes=d.execute('SELECT * FROM client_notes WHERE client_id=? ORDER BY created_at DESC',(cid,)).fetchall()
                photos=d.execute('SELECT id,caption,public,created_at FROM photos WHERE client_id=? ORDER BY id DESC',(cid,)).fetchall()
                inv=d.execute('SELECT id FROM invoices WHERE client_id=? ORDER BY id DESC',(cid,)).fetchall()
                invoices=[invoice_summary(d,r['id']) for r in inv]
            self.json({'client':dict(c),'history':[dict(x) for x in history],'notes':[dict(x) for x in notes],
                       'photos':[dict(x) for x in photos],'invoices':invoices});return
        if path=='/api/admin/invoices':
            self.admin()
            with conn() as d:
                ids=d.execute('SELECT id FROM invoices ORDER BY id DESC LIMIT 500').fetchall()
                invoices=[invoice_summary(d,i['id']) for i in ids]
                pays=d.execute('SELECT * FROM payments ORDER BY created_at DESC LIMIT 500').fetchall()
            self.json({'invoices':invoices,'payments':[dict(x) for x in pays]});return
        if path=='/api/admin/hours':
            self.admin()
            with conn() as d:
                rows=d.execute('SELECT * FROM hours ORDER BY weekday').fetchall()
            self.json({'hours':[dict(r) for r in rows]});return
        if path=='/api/admin/messages':
            self.admin()
            with conn() as d:
                rows=d.execute('SELECT * FROM inquiries ORDER BY id DESC LIMIT 400').fetchall()
            self.json({'messages':[dict(r) for r in rows]});return
        if path=='/api/admin/services':
            self.admin()
            with conn() as d:
                rows=d.execute('SELECT * FROM services ORDER BY id').fetchall()
            self.json({'services':[dict(x) for x in rows]});return
        if path=='/api/admin/extras':
            self.admin()
            with conn() as d:
                blocks=d.execute('SELECT * FROM blocks ORDER BY start_ts DESC LIMIT 200').fetchall()
                gifts=d.execute('SELECT * FROM gift_cards ORDER BY id DESC LIMIT 200').fetchall()
                coupons=d.execute('SELECT * FROM coupons ORDER BY id DESC LIMIT 200').fetchall()
                inventory=d.execute('SELECT * FROM inventory ORDER BY name LIMIT 300').fetchall()
                expenses=d.execute('SELECT * FROM expenses ORDER BY incurred_ts DESC LIMIT 300').fetchall()
            self.json({'blocks':[dict(x) for x in blocks],'gifts':[dict(x) for x in gifts],
                       'coupons':[dict(x) for x in coupons],'inventory':[dict(x) for x in inventory],
                       'expenses':[dict(x) for x in expenses]});return
        if path=='/api/admin/export':
            self.admin()
            name=q.get('type',[''])[0]
            tables={'clients':['id','name','email','phone','preferences','allergies','formula','contact_consent','photo_consent'],
                    'appointments':['id','client_id','service_id','start_ts','end_ts','status','price_cents'],
                    'invoices':['id','client_id','appointment_id','description','subtotal_cents','tax_cents','discount_cents'],
                    'payments':['id','invoice_id','amount_cents','tip_cents','method','created_at'],
                    'expenses':['id','description','category','amount_cents','incurred_ts']}
            cols=tables.get(name)
            if not cols:raise ApiError(404,'Unknown report')
            with conn() as d:
                records=d.execute('SELECT '+','.join(cols)+' FROM '+name+' ORDER BY id DESC').fetchall()
            file=io.StringIO();writer=csv.writer(file);writer.writerow(cols)
            for r in records:
                cells=[]
                for column in cols:
                    raw=str(r[column]).replace('\r',' ').replace('\n',' ') if r[column] is not None else ''
                    # Prevent spreadsheet formula injection from untrusted client text.
                    if raw.lstrip().startswith(('=','+','-','@','\t')):
                        raw="'"+raw
                    cells.append(raw)
                writer.writerow(cells)
            body=file.getvalue().encode('utf-8-sig')
            self.respond_headers(200,'text/csv; charset=utf-8',len(body),extra=[('Content-Disposition',f'attachment; filename="velvet-vivid-{name}.csv"')]);self.wfile.write(body);return
        raise ApiError(404,'Not found')

    def api_post(self,path,v):
        if path=='/api/contact':
            throttle('contact:'+self.client_ip(),6,3600)
            name=clean(v.get('name',''),90);email=email_address(v.get('email',''))
            phone=clean(v.get('phone',''),25);message=clean(v.get('message',''),2000)
            if not name or not message:raise ApiError(400,'Name and message are required')
            with conn() as d:
                d.execute('INSERT INTO inquiries(name,email,phone,message,created_at) VALUES(?,?,?,?,?)',
                          (name,email,phone,message,now()))
            if os.getenv('ADMIN_NOTIFY_EMAIL'):
                try:send_email(os.environ['ADMIN_NOTIFY_EMAIL'],'Website inquiry | '+BUSINESS,
                               f'New inquiry from {name} ({email}). Open stylist dashboard to review.')
                except Exception:pass
            self.json({'ok':True},201);return
        if path=='/api/auth/register':
            throttle('register:'+self.client_ip(),5,3600)
            name=clean(v.get('name',''),90);email=email_address(v.get('email',''))
            phone=clean(v.get('phone',''),25);password=v.get('password','')
            if not name or not phone or not isinstance(password,str) or len(password)<12 or len(password)>128:
                raise ApiError(400,'Name and password of at least 12 characters required')
            with conn() as d:
                d.execute('INSERT INTO users(email,name,phone,password_hash,role,created_at) VALUES(?,?,?,?,?,?)',
                          (email,name,phone,password_hash(password),'CLIENT',now()))
            self.json({'ok':True,'message':'Account created. Sign in to manage future bookings.'},201);return
        if path=='/api/auth/login':
            throttle('login:'+self.client_ip(),10,900)
            email=email_address(v.get('email',''))
            password=v.get('password','')
            if not isinstance(password,str):password=''
            with conn() as d:
                u=d.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
                valid=password_ok(password,u['password_hash']) if u else password_ok(password,password_hash('dummy-password-not-matched'))
                if not u or not valid or (u['role']=='ADMIN' and not admin_totp_ok(v.get('code',''))):
                    raise ApiError(401,'Email, password, or verification code is incorrect')
                token=secrets.token_hex(32)
                d.execute('INSERT INTO sessions(hash,user_id,expires) VALUES(?,?,?)',
                          (hashlib.sha256(token.encode()).hexdigest(),u['id'],now()+7*86400))
                audit(d,u['id'],'login')
            self.json({'ok':True,'user':{'id':u['id'],'name':u['name'],'email':u['email'],'role':u['role']}},
                      cookies=[self.setcookie('vv_session',token,True,7*86400)]);return
        if path=='/api/auth/request-reset':
            throttle('reset:'+self.client_ip(),5,3600)
            email=email_address(v.get('email',''))
            with conn() as d:
                u=d.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
                if u and os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM'):
                    token=secrets.token_hex(32)
                    d.execute('DELETE FROM password_resets WHERE user_id=?',(u['id'],))
                    d.execute('INSERT INTO password_resets(hash,user_id,expires) VALUES(?,?,?)',
                              (hashlib.sha256(token.encode()).hexdigest(),u['id'],now()+1800))
                    link=SITE+'/#/reset?token='+token
                    try:
                        send_email(email,'Reset your Velvet & Vivid password',
                            'Use this one-time link within 30 minutes to reset your password:\n'+link)
                    except Exception:
                        print('Reset email failed',file=sys.stderr)
            self.json({'ok':True,'message':'If an account exists and email is configured, a reset link has been sent.'});return
        if path=='/api/auth/reset-password':
            token=clean(v.get('token',''),128)
            pwd=v.get('password')
            if not re.fullmatch('[a-f0-9]{64}',token) or not isinstance(pwd,str) or not 12<=len(pwd)<=128:
                raise ApiError(400,'Invalid reset link or password')
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                r=d.execute('SELECT * FROM password_resets WHERE hash=? AND expires>?',
                            (hashlib.sha256(token.encode()).hexdigest(),now())).fetchone()
                if not r:raise ApiError(400,'Reset link is invalid or expired')
                d.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(pwd),r['user_id']))
                d.execute('DELETE FROM password_resets WHERE user_id=?',(r['user_id'],))
                d.execute('DELETE FROM sessions WHERE user_id=?',(r['user_id'],))
                audit(d,r['user_id'],'password-reset')
            self.json({'ok':True});return
        if path=='/api/my/profile/update':
            u=self.signed_in()
            if u['role']!='CLIENT':raise ApiError(403,'Client account required')
            name=clean(v.get('name',''),90);phone=clean(v.get('phone',''),25)
            if not name or not phone:raise ApiError(400,'Name and phone required')
            with conn() as d:
                d.execute('UPDATE users SET name=?,phone=? WHERE id=?',(name,phone,u['id']))
                d.execute('UPDATE clients SET name=?,phone=? WHERE user_id=?',(name,phone,u['id']))
                audit(d,u['id'],'profile-edited')
            self.json({'ok':True});return
        if path=='/api/auth/logout':
            token=self.cookie('vv_session')
            if token:
                with conn() as d:d.execute('DELETE FROM sessions WHERE hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            self.json({'ok':True},cookies=[self.setcookie('vv_session','',True,0)]);return
        if path=='/api/my/appointments/cancel':
            u=self.signed_in()
            if u['role']!='CLIENT':raise ApiError(403,'Client account required')
            id=integer(v.get('id'),1)
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                a=d.execute('''SELECT a.* FROM appointments a JOIN clients c ON c.id=a.client_id
                   WHERE a.id=? AND c.user_id=?''',(id,u['id'])).fetchone()
                if not a:raise ApiError(404,'Appointment not found')
                if a['status'] not in ('PENDING','APPROVED') or a['start_ts']-now()<24*3600:
                    raise ApiError(400,'Online cancellation is available at least 24 hours before an active appointment. Contact the salon directly for other changes.')
                d.execute("UPDATE appointments SET status='CANCELLED' WHERE id=?",(id,))
                audit(d,u['id'],'customer-cancel',str(id))
            notify_appointment(id,'cancelled')
            self.json({'ok':True});return
        if path=='/api/appointments/request':
            throttle('book:'+self.client_ip(),10,3600)
            u=self.user();service_id=integer(v.get('service_id'),1);start=timestamp(v.get('start'))
            message=clean(v.get('message',''),800)
            if u and u['role']=='CLIENT':
                with conn() as d:
                    profile=d.execute('SELECT name,email,phone FROM users WHERE id=?',(u['id'],)).fetchone()
                name=profile['name'];email=profile['email'];phone=profile['phone'];user_id=u['id']
            else:
                name=clean(v.get('name',''),90);email=email_address(v.get('email',''))
                phone=clean(v.get('phone',''),25);user_id=None
            if not name or not phone:
                raise ApiError(400,'Name and phone are required')
            consent=int(bool(v.get('contact_consent',False)))
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                s=d.execute('SELECT * FROM services WHERE id=? AND active=1',(service_id,)).fetchone()
                if not s:raise ApiError(404,'Service unavailable')
                if not slot_allowed(start,s['duration'],d):raise ApiError(400,'Time is outside available hours')
                end=start+s['duration']*60
                if overlaps(d,start,end):raise ApiError(409,'That time has just been requested. Choose another.')
                client=None
                if user_id:
                    client=d.execute('SELECT id FROM clients WHERE user_id=?',(user_id,)).fetchone()
                if not client:
                    c=d.execute('INSERT INTO clients(user_id,name,email,phone,contact_consent,created_at) VALUES(?,?,?,?,?,?)',
                                (user_id,name,email,phone,consent,now()))
                    client_id=c.lastrowid
                else:
                    client_id=client['id']
                    d.execute('UPDATE clients SET contact_consent=? WHERE id=?',(consent,client_id))
                id=d.execute('''INSERT INTO appointments(client_id,service_id,start_ts,end_ts,message,price_cents,created_at)
                    VALUES(?,?,?,?,?,?,?)''',(client_id,service_id,start,end,message,s['price_cents'],now())).lastrowid
                audit(d,user_id,'appointment-requested',f'appointment={id}')
            notify_appointment(id,'requested')
            self.json({'ok':True,'appointment_id':id,'message':'Your request was submitted. It is not confirmed until approved.'},201);return
        if path=='/api/admin/appointment/status':
            u=self.admin();id=integer(v.get('id'),1);status=clean(v.get('status',''),20)
            if status not in STATUSES:raise ApiError(400,'Invalid status')
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                a=d.execute('SELECT * FROM appointments WHERE id=?',(id,)).fetchone()
                if not a:raise ApiError(404,'Appointment not found')
                if status=='APPROVED' and (a['start_ts'] < now() or overlaps(d,a['start_ts'],a['end_ts'],id)):
                    raise ApiError(409,'Time is no longer available')
                d.execute('UPDATE appointments SET status=? WHERE id=?',(status,id))
                audit(d,u['id'],'appointment-status',f'{id}:{status}')
            if status in ('APPROVED','DECLINED','CANCELLED'):
                notify_appointment(id, {'APPROVED':'approved','DECLINED':'declined','CANCELLED':'cancelled'}[status])
            self.json({'ok':True});return
        if path=='/api/admin/appointment/reschedule':
            u=self.admin();id=integer(v.get('id'),1);start=timestamp(v.get('start'))
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                a=d.execute('SELECT * FROM appointments WHERE id=?',(id,)).fetchone()
                if not a:raise ApiError(404,'Appointment not found')
                dur=(a['end_ts']-a['start_ts'])//60
                if not slot_allowed(start,dur,d) or overlaps(d,start,start+dur*60,id):
                    raise ApiError(409,'That time is unavailable')
                d.execute('UPDATE appointments SET start_ts=?,end_ts=?,status=? WHERE id=?',(start,start+dur*60,'APPROVED',id))
                audit(d,u['id'],'rescheduled',str(id))
            notify_appointment(id,'rescheduled');self.json({'ok':True});return
        if path=='/api/admin/client/update':
            u=self.admin();cid=integer(v.get('id'),1)
            values=[clean(v.get(x,''),n) for x,n in (('name',90),('email',254),('phone',25),('preferences',3000),('allergies',3000),('formula',5000))]
            if not values[0]:raise ApiError(400,'Name required')
            values.extend([int(bool(v.get('contact_consent'))),int(bool(v.get('photo_consent'))),cid])
            with conn() as d:
                changed=d.execute('''UPDATE clients SET name=?,email=?,phone=?,preferences=?,allergies=?,formula=?,contact_consent=?,photo_consent=? WHERE id=?''',values)
                if not changed.rowcount:raise ApiError(404,'Client not found')
                if not values[-2]:
                    d.execute('UPDATE photos SET public=0 WHERE client_id=?',(cid,))
                audit(d,u['id'],'client-update',str(cid))
            self.json({'ok':True});return
        if path=='/api/admin/notes/add':
            u=self.admin();cid=integer(v.get('client_id'),1)
            note=clean(v.get('note',''),5000)
            if not note:raise ApiError(400,'Write a note first')
            with conn() as d:
                d.execute('INSERT INTO client_notes(client_id,note,created_at) VALUES(?,?,?)',(cid,note,now()))
                audit(d,u['id'],'note-added',str(cid))
            self.json({'ok':True});return
        if path=='/api/admin/photos/add':
            u=self.admin();cid=integer(v.get('client_id'),1);caption=clean(v.get('caption',''),150)
            is_public=bool(v.get('public',False));payload=v.get('image','')
            if not isinstance(payload,str) or not payload.startswith('data:image/webp;base64,'):
                raise ApiError(400,'Upload a WebP image')
            try:
                data=base64.b64decode(payload.split(',',1)[1],validate=True)
            except (ValueError,base64.binascii.Error):raise ApiError(400,'Invalid image')
            if len(data)>5_000_000 or len(data)<24 or not (data[:4]==b'RIFF' and data[8:12]==b'WEBP'):
                raise ApiError(400,'Invalid or oversized image')
            with conn() as d:
                c=d.execute('SELECT photo_consent FROM clients WHERE id=?',(cid,)).fetchone()
                if not c:raise ApiError(404,'Client not found')
                if is_public and not c['photo_consent']:
                    raise ApiError(400,'Client must consent to portfolio sharing first')
                fn=secrets.token_hex(16)+'.webp'
                (MEDIA/fn).write_bytes(data)
                id=d.execute('INSERT INTO photos(client_id,filename,caption,public,created_at) VALUES(?,?,?,?,?)',
                              (cid,fn,caption,int(is_public),now())).lastrowid
                audit(d,u['id'],'photo-added',f'{id}:public={is_public}')
            self.json({'ok':True,'id':id});return
        if path=='/api/admin/photos/public':
            u=self.admin();pid=integer(v.get('id'),1);flag=int(bool(v.get('public')))
            with conn() as d:
                p=d.execute('''SELECT p.id,c.photo_consent FROM photos p JOIN clients c ON c.id=p.client_id WHERE p.id=?''',(pid,)).fetchone()
                if not p:raise ApiError(404,'Photo not found')
                if flag and not p['photo_consent']:raise ApiError(400,'Obtain client permission first')
                d.execute('UPDATE photos SET public=? WHERE id=?',(flag,pid))
                audit(d,u['id'],'photo-visibility',f'{pid}:{flag}')
            self.json({'ok':True});return
        if path=='/api/admin/invoices/create':
            u=self.admin();cid=integer(v.get('client_id'),1)
            appt=v.get('appointment_id');appt=integer(appt,1) if appt else None
            desc=clean(v.get('description',''),220)
            subtotal=integer(v.get('subtotal_cents'),0,10_000_000)
            tax=integer(v.get('tax_cents',0),0,5_000_000)
            discount=integer(v.get('discount_cents',0),0,10_000_000)
            coupon=clean(v.get('coupon_code',''),30).upper()
            if coupon:
                with conn() as d:
                    r=d.execute('SELECT percent FROM coupons WHERE code=? AND active=1',(coupon,)).fetchone()
                if not r:raise ApiError(400,'Coupon code is not valid')
                discount+=subtotal*r['percent']//100
                desc=desc+' (coupon '+coupon+')'
            if discount>subtotal+tax:raise ApiError(400,'Discount cannot exceed total')
            if not desc:raise ApiError(400,'Description required')
            with conn() as d:
                c=d.execute('SELECT id FROM clients WHERE id=?',(cid,)).fetchone()
                if not c:raise ApiError(404,'Client not found')
                if appt and not d.execute('SELECT 1 FROM appointments WHERE id=? AND client_id=?',(appt,cid)).fetchone():
                    raise ApiError(400,'Appointment does not belong to client')
                id=d.execute('''INSERT INTO invoices(client_id,appointment_id,description,subtotal_cents,tax_cents,discount_cents,created_at)
                   VALUES(?,?,?,?,?,?,?)''',(cid,appt,desc,subtotal,tax,discount,now())).lastrowid
                audit(d,u['id'],'invoice-created',str(id))
            self.json({'ok':True,'id':id},201);return
        if path=='/api/admin/payments/add':
            u=self.admin();id=integer(v.get('invoice_id'),1)
            amount=integer(v.get('amount_cents'),1,10_000_000)
            tip=integer(v.get('tip_cents',0),0,1_000_000)
            method=clean(v.get('method',''),30);ref=clean(v.get('reference',''),150)
            if method not in METHODS:raise ApiError(400,'Unknown payment method')
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                inv=invoice_summary(d,id)
                if amount>inv['due_cents']:
                    raise ApiError(400,'Payment exceeds outstanding balance')
                gift_id=None
                if method=='gift_card':
                    if not ref:raise ApiError(400,'Enter gift card code as reference')
                    gift=d.execute('SELECT * FROM gift_cards WHERE code=?',(ref.upper(),)).fetchone()
                    if not gift or gift['balance_cents']<amount:raise ApiError(400,'Gift card has insufficient funds')
                    gift_id=gift['id'];d.execute('UPDATE gift_cards SET balance_cents=balance_cents-? WHERE id=?',(amount,gift_id))
                d.execute('''INSERT INTO payments(invoice_id,amount_cents,tip_cents,method,reference,gift_card_id,created_at)
                    VALUES(?,?,?,?,?,?,?)''',(id,amount,tip,method,ref,gift_id,now()))
                audit(d,u['id'],'payment-added',f'{id}:{amount}:{method}')
            self.json({'ok':True});return
        if path=='/api/invoices/checkout':
            u=self.admin();invoice_id=integer(v.get('invoice_id'),1)
            secret=os.getenv('STRIPE_SECRET_KEY')
            if not secret:raise ApiError(503,'Stripe is not connected')
            with conn() as d:inv=invoice_summary(d,invoice_id)
            if inv['due_cents']<50:raise ApiError(400,'Invoice must have at least $0.50 outstanding')
            # Hosted checkout redirects to Stripe. Use signed webhook to mark paid.
            form={'mode':'payment', 'success_url':SITE+'/#/payment-success',
                  'cancel_url':SITE+'/#/payment-cancelled',
                  'line_items[0][price_data][currency]':'usd',
                  'line_items[0][price_data][product_data][name]':f'{BUSINESS} - Invoice #{invoice_id}',
                  'line_items[0][price_data][unit_amount]':str(inv['due_cents']),
                  'line_items[0][quantity]':'1',
                  'metadata[invoice_id]':str(invoice_id),'client_reference_id':str(invoice_id)}
            req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',
                 data=urllib.parse.urlencode(form).encode(),headers={'Authorization':'Bearer '+secret})
            try:
                with urllib.request.urlopen(req,timeout=20) as res:result=json.loads(res.read())
            except Exception as e:
                print('Stripe checkout unavailable',type(e).__name__,file=sys.stderr)
                raise ApiError(502,'Payment provider unavailable')
            self.json({'url':result.get('url')});return
        if path=='/api/admin/hours/save':
            u=self.admin()
            rows=v.get('hours',[])
            if not isinstance(rows,list) or len(rows)!=7:
                raise ApiError(400,'Seven days of hours are required')
            validated=[]
            for i,row in enumerate(rows):
                if not isinstance(row,dict) or integer(row.get('weekday'),0,6)!=i:
                    raise ApiError(400,'Invalid weekday')
                enabled=int(bool(row.get('enabled')))
                open_min=integer(row.get('open_min'),0,1439)
                close_min=integer(row.get('close_min'),1,1440)
                if close_min-open_min<30 or open_min%30 or close_min%30:
                    raise ApiError(400,'Hours must use half-hour increments with at least 30 minutes open')
                validated.append((enabled,open_min,close_min,i))
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                d.executemany('UPDATE hours SET enabled=?,open_min=?,close_min=? WHERE weekday=?',validated)
                audit(d,u['id'],'hours-updated')
            self.json({'ok':True});return
        if path=='/api/admin/services/save':
            u=self.admin();id=v.get('id');id=integer(id,1) if id else None
            name=clean(v.get('name',''),90);desc=clean(v.get('description',''),500);cat=clean(v.get('category',''),90)
            price=integer(v.get('price_cents'),0,5000000);duration=integer(v.get('duration'),30,480)
            if duration%30 or not name or not cat:raise ApiError(400,'Service name, category and 30-minute duration required')
            active=int(bool(v.get('active',True)))
            with conn() as d:
                if id:
                    d.execute('UPDATE services SET name=?,description=?,category=?,price_cents=?,duration=?,active=? WHERE id=?',
                              (name,desc,cat,price,duration,active,id))
                else:
                    d.execute('INSERT INTO services(name,description,category,price_cents,duration,active) VALUES(?,?,?,?,?,?)',
                              (name,desc,cat,price,duration,active))
                audit(d,u['id'],'service-saved',str(id or 'new'))
            self.json({'ok':True});return
        if path=='/api/admin/blocks/add':
            u=self.admin();start=timestamp(v.get('start'));end=timestamp(v.get('end'))
            reason=clean(v.get('reason','Unavailable'),140)
            if end<=start or end-start>30*86400:raise ApiError(400,'Invalid time range')
            with conn() as d:
                d.execute('BEGIN IMMEDIATE')
                if d.execute("SELECT 1 FROM appointments WHERE status IN ('PENDING','APPROVED') AND start_ts < ? AND end_ts > ? LIMIT 1",(end,start)).fetchone():
                    raise ApiError(409,'This closure overlaps an existing appointment request. Reschedule or decline it first.')
                d.execute('INSERT INTO blocks(start_ts,end_ts,reason) VALUES(?,?,?)',(start,end,reason))
                audit(d,u['id'],'time-block',reason)
            self.json({'ok':True});return
        if path=='/api/admin/blocks/delete':
            u=self.admin();id=integer(v.get('id'),1)
            with conn() as d:
                d.execute('DELETE FROM blocks WHERE id=?',(id,));audit(d,u['id'],'block-delete',str(id))
            self.json({'ok':True});return
        if path=='/api/admin/gifts/add':
            u=self.admin();amount=integer(v.get('amount_cents'),100,1_000_000)
            code=secrets.token_hex(5).upper()
            with conn() as d:
                d.execute('INSERT INTO gift_cards(code,balance_cents,issued_cents,created_at) VALUES(?,?,?,?)',
                         (code,amount,amount,now()))
                audit(d,u['id'],'gift-created',f'amount={amount}')
            self.json({'ok':True,'code':code});return
        if path=='/api/admin/coupons/add':
            u=self.admin();code=clean(v.get('code',''),30).upper();percent=integer(v.get('percent'),1,100)
            if not re.fullmatch('[A-Z0-9_-]{3,30}',code):raise ApiError(400,'Invalid coupon code')
            with conn() as d:
                d.execute('INSERT INTO coupons(code,percent,active) VALUES(?,?,1)',(code,percent))
                audit(d,u['id'],'coupon-created',code)
            self.json({'ok':True});return
        if path=='/api/admin/inventory/save':
            u=self.admin();id=v.get('id');id=integer(id,1) if id else None
            name=clean(v.get('name',''),140);qty=integer(v.get('quantity'),0,100000)
            reorder=integer(v.get('reorder_level',0),0,100000);cost=integer(v.get('unit_cost_cents',0),0,10_000_000)
            if not name:raise ApiError(400,'Item name required')
            with conn() as d:
                if id:d.execute('UPDATE inventory SET name=?,quantity=?,reorder_level=?,unit_cost_cents=? WHERE id=?',(name,qty,reorder,cost,id))
                else:d.execute('INSERT INTO inventory(name,quantity,reorder_level,unit_cost_cents) VALUES(?,?,?,?)',(name,qty,reorder,cost))
                audit(d,u['id'],'inventory-saved',name)
            self.json({'ok':True});return
        if path=='/api/admin/expenses/add':
            u=self.admin();desc=clean(v.get('description',''),180);cat=clean(v.get('category','Other'),80)
            amount=integer(v.get('amount_cents'),1,10_000_000)
            if not desc:raise ApiError(400,'Expense description required')
            with conn() as d:
                d.execute('INSERT INTO expenses(description,category,amount_cents,incurred_ts) VALUES(?,?,?,?)',
                         (desc,cat,amount,now()))
                audit(d,u['id'],'expense-recorded',desc)
            self.json({'ok':True});return
        raise ApiError(404,'Not found')

    def stripe_webhook(self):
        secret=os.getenv('STRIPE_WEBHOOK_SECRET','')
        if not secret:raise ApiError(503,'Webhook not configured')
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise ApiError(400,'Invalid length')
        if length>262144 or length<2:raise ApiError(413,'Invalid payload')
        payload=self.rfile.read(length)
        signature=self.headers.get('Stripe-Signature','')
        opts=dict(item.split('=',1) for item in signature.split(',') if '=' in item)
        try:
            t=int(opts.get('t','0'))
            expected=hmac.new(secret.encode(),str(t).encode()+b'.'+payload,hashlib.sha256).hexdigest()
            good=abs(now()-t)<=300 and any(hmac.compare_digest(expected,item[3:])
                 for item in signature.split(',') if item.startswith('v1='))
        except ValueError:good=False
        if not good:raise ApiError(400,'Invalid webhook signature')
        event=json.loads(payload)
        if event.get('type')=='checkout.session.completed':
            session=event['data']['object']
            if session.get('payment_status')=='paid':
                invoice_id=integer(session.get('metadata',{}).get('invoice_id'),1)
                payment_id=str(session.get('payment_intent') or session.get('id',''))
                amount=integer(session.get('amount_total'),0)
                with conn() as d:
                    d.execute('BEGIN IMMEDIATE')
                    if not d.execute('SELECT 1 FROM payments WHERE stripe_id=?',(payment_id,)).fetchone():
                        inv=invoice_summary(d,invoice_id)
                        if amount>0 and amount<=inv['due_cents']:
                            d.execute('''INSERT INTO payments(invoice_id,amount_cents,tip_cents,method,reference,stripe_id,created_at)
                              VALUES(?,?,0,'stripe',?,?,?)''',(invoice_id,amount,payment_id,payment_id,now()))
                            audit(d,None,'stripe-payment',f'{invoice_id}:{amount}')
        self.json({'received':True})


def run_reminders():
    lower=now()+23*3600;upper=now()+25*3600
    sent=0
    with conn() as d:
        appointments=d.execute("SELECT id FROM appointments WHERE status='APPROVED' AND start_ts BETWEEN ? AND ?",(lower,upper)).fetchall()
    for x in appointments:
        with conn() as d:
            a=d.execute('''SELECT a.*,c.name,c.email,c.phone,c.contact_consent,s.name AS service_name
              FROM appointments a JOIN clients c ON c.id=a.client_id JOIN services s ON s.id=a.service_id WHERE a.id=?''',(x['id'],)).fetchone()
        date=datetime.fromtimestamp(a['start_ts'],TZ).strftime('%a %b %d at %I:%M %p')
        message=f'{BUSINESS}: Reminder, {a["service_name"]} on {date}. Contact the salon to make changes.'
        for channel in ('email','sms'):
            if channel=='sms' and (not a['contact_consent'] or not a['phone']):continue
            with conn() as d:
                exists=d.execute('SELECT 1 FROM notifications WHERE appointment_id=? AND type=? AND channel=?',
                                 (a['id'],'24h',channel)).fetchone()
            if exists:continue
            try:
                ok=send_email(a['email'],'Appointment reminder | '+BUSINESS,message) if channel=='email' else send_sms(a['phone'],message)
                if ok:
                    with conn() as d:d.execute('INSERT OR IGNORE INTO notifications(appointment_id,type,channel,sent_at) VALUES(?,?,?,?)',
                                           (a['id'],'24h',channel,now()))
                    sent+=1
            except Exception as e:
                print('Reminder failed',type(e).__name__,file=sys.stderr)
    print(f'Reminders delivered: {sent}')


def reminder_loop():
    """Single-instance in-process reminder scheduler; provider credentials required."""
    while True:
        try:
            run_reminders()
        except Exception as e:
            print('Reminder scheduler error:',type(e).__name__,file=sys.stderr)
        time.sleep(30*60)


def main():
    p=argparse.ArgumentParser()
    cmd=p.add_subparsers(dest='command')
    a=cmd.add_parser('create-admin');a.add_argument('--email',required=True);a.add_argument('--name',default='Stylist')
    cmd.add_parser('reminders')
    cmd.add_parser('backup')
    args=p.parse_args()
    init_db()
    if args.command=='create-admin':
        import getpass
        pwd=getpass.getpass('Admin password (at least 12 characters): ')
        if len(pwd)<12:raise SystemExit('Password too short')
        with conn() as d:
            d.execute('INSERT INTO users(email,name,phone,password_hash,role,created_at) VALUES(?,?,?,?,?,?)',
                      (email_address(args.email),clean(args.name,90),'',password_hash(pwd),'ADMIN',now()))
        print('Admin created. Configure ADMIN_TOTP_SECRET for additional protection.')
    elif args.command=='reminders':run_reminders()
    elif args.command=='backup':
        name=DATA/('backup-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.sqlite3')
        with conn() as source,sqlite3.connect(name) as target:source.backup(target)
        print('Database backup:',name,'(Also back up the uploads/ directory.)')
    else:
        print(f'Visit {SITE}. Local server port {PORT}.')
        if os.getenv('ENABLE_REMINDER_LOOP','1')=='1':
            Thread(target=reminder_loop,daemon=True,name='reminders').start()
        ThreadingHTTPServer(('0.0.0.0',PORT),SiteHandler).serve_forever()


if __name__=='__main__':main()
