"""Small, single-admin inquiry service. Standard library only; see OPERATIONS.md."""
import hashlib
import base64
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', ROOT.parent / 'beneficial-to-family-private')).resolve()
HOST = os.environ.get('BIND_HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '8766'))
BASE = os.environ.get('PUBLIC_BASE_URL', os.environ.get('RENDER_EXTERNAL_URL', f'http://127.0.0.1:{PORT}')).rstrip('/')
LIVE = os.environ.get('LAUNCH_READY') == 'yes'
SECURE = BASE.startswith('https://')
STATUSES = ('new', 'reviewing', 'waiting_customer', 'closed')
SERVICES = ('specialist', 'records', 'travel', 'partner')
FILES = {'/': 'index.html', **{'/' + n: n for n in ('index.html', 'styles.css', 'reading.css', 'app.js', 'skyline.svg', 'workflow.js', 'portal.html', 'portal.js', 'portal.css', 'admin.html', 'admin.js')}}
MIMES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}
FILES.update({'/' + n: n for n in ('en.html', 'zh.html', 'international.js', 'favicon.svg', 'robots.txt')})
MIMES['.txt'] = 'text/plain'
FILES['/sitemap.xml'] = 'sitemap.xml'
MIMES['.xml'] = 'application/xml'
MIMES['.jpg'] = 'image/jpeg'
FILES.update({'/' + n: n for n in ('hero-family.jpg', 'hero-family-small.jpg', 'family-shanghai.jpg', 'family-shanghai-small.jpg')})
# Public review documents only; private/internal material is not served.
FILES.update({'/' + str(p.relative_to(ROOT)): str(p.relative_to(ROOT))
              for p in (ROOT / 'privacy').glob('*')
              if p.is_file() and p.suffix in ('.html', '.txt', '.css')})
sessions = {}
rates = {}
lock = threading.Lock()

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def db():
    conn = sqlite3.connect(DATA / 'inquiries.sqlite3', timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init():
    global PASSWORD
    if DATA == ROOT or ROOT in DATA.parents:
        raise RuntimeError('DATA_DIR must be outside the website source directory.')
    if LIVE and (not SECURE or not os.environ.get('ADMIN_PASSWORD')):
        raise RuntimeError('Live mode requires HTTPS PUBLIC_BASE_URL and ADMIN_PASSWORD.')
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
    if not PASSWORD:
        secret_file = DATA / 'admin-password.txt'
        if not secret_file.exists():
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(24))
        PASSWORD = secret_file.read_text().strip()
    if len(PASSWORD) < 16:
        raise RuntimeError('ADMIN_PASSWORD must contain at least 16 characters.')
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tickets (
          id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, name TEXT NOT NULL,
          email TEXT NOT NULL, country TEXT NOT NULL, language TEXT NOT NULL,
          service TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
          created REAL NOT NULL, updated REAL NOT NULL, consent_version TEXT NOT NULL,
          admin_seen REAL NOT NULL DEFAULT 0, customer_seen REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL, role TEXT NOT NULL,
          body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL, audience TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, ticket_id TEXT, action TEXT NOT NULL, created REAL NOT NULL);
        ''')
    os.chmod(DATA / 'inquiries.sqlite3', 0o600)

def notify(ticket_id, audience, email):
    # No patient name, request content or access secret is included in email.
    required = ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'MAIL_FROM')
    target = os.environ.get('ADMIN_EMAIL', '') if audience == 'admin' else email
    state = 'not_configured'
    if all(os.environ.get(x) for x in required) and target:
        message = EmailMessage()
        message['From'] = os.environ['MAIL_FROM']
        message['To'] = target
        message['Subject'] = f'Beneficial to Family | Inquiry update {ticket_id}'
        url = BASE + ('/admin.html' if audience == 'admin' else '/portal.html')
        message.set_content(f'咨询有更新 / Your inquiry has an update.\n\nReference: {ticket_id}\n{url}\n\n请使用原有专属链接或访问密钥查看。请勿直接回复此邮件。\nUse your saved private link or access key. This mailbox is not monitored for replies.\nThis email does not contain medical information.')
        try:
            with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '587')), timeout=8) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
                smtp.send_message(message)
            state = 'accepted'  # SMTP acceptance is not proof of inbox delivery.
        except Exception:
            state = 'failed'
    with db() as c:
        c.execute('INSERT INTO notifications(ticket_id,audience,status,created) VALUES(?,?,?,?)', (ticket_id, audience, state, time.time()))
    return state

class RequestError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log request paths, bearer tokens, contacts or message contents.

    def respond(self, body, status=200, mime='application/json', cookie=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if mime == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        public_page = urlsplit(self.path).path in ('/', '/index.html', '/en.html', '/zh.html')
        self.send_header('X-Robots-Tag', 'index, follow' if public_page and BASE == 'https://linksinocare.com' else 'noindex, nofollow')
        hashes = ''
        if mime == 'text/html':
            for block in re.findall(rb'<script type="application/ld\+json">(.*?)</script>', raw, re.S):
                hashes += " 'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode() + "'"
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'" + hashes + "; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if SECURE:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)

    def limited(self, kind, count, seconds):
        now = time.time()
        key = (self.client_address[0], kind)
        with lock:
            for k in list(rates):
                if not rates[k] or now - rates[k][-1] > 3600:
                    del rates[k]
            recent = [v for v in rates.get(key, []) if v > now - seconds]
            if len(recent) >= count:
                raise RequestError('Too many requests. Please try later. / 操作频繁，请稍后再试。', 429)
            rates[key] = recent + [now]

    def admin(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get('Cookie', ''))
        value = cookie.get('bf_admin')
        key = digest(value.value) if value else ''
        with lock:
            if sessions.get(key, 0) < time.time():
                sessions.pop(key, None)
                raise RequestError('Please sign in. / 请先登录。', 401)
        return key

    def ticket(self, c, ticket_id, admin=False):
        row = c.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone()
        if admin:
            self.admin()
        elif not row or not hmac.compare_digest(row['token_hash'], digest(self.headers.get('Authorization', '').removeprefix('Bearer '))):
            raise RequestError('Invalid reference or access key. / 编号或访问密钥不正确。', 403)
        if not row:
            raise RequestError('Not found', 404)
        return row

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        try:
            path = urlsplit(self.path).path
            if not post and path in FILES:
                file = ROOT / FILES[path]
                return self.respond(file.read_bytes(), mime=MIMES[file.suffix])
            self.limited('api', 180, 60)
            payload = {}
            if post:
                if self.headers.get('Origin') != BASE:
                    raise RequestError('Origin not permitted', 403)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise RequestError('JSON required', 415)
                try:
                    length = int(self.headers.get('Content-Length', 0))
                except ValueError:
                    raise RequestError('Invalid length')
                if length < 1 or length > 12000:
                    raise RequestError('Request too large', 413)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise RequestError('Invalid request')
            self.route(path, post, payload)
        except RequestError as e:
            self.respond({'error': e.message}, e.status)
        except (ValueError, TypeError, KeyError):
            self.respond({'error': 'Invalid input / 输入格式不正确'}, 400)
        except Exception:
            self.respond({'error': 'Service unavailable; do not resubmit repeatedly. / 服务异常，请勿反复提交。'}, 500)

    def route(self, path, post, p):
        if path == '/api/config' and not post:
            return self.respond({'live': LIVE, 'email_configured': all(os.environ.get(k) for k in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'MAIL_FROM', 'ADMIN_EMAIL'))})
        if path == '/api/admin/login' and post:
            self.limited('login', 8, 900)
            if not hmac.compare_digest(str(p.get('password', '')), PASSWORD):
                raise RequestError('Incorrect password / 密码错误', 401)
            token = secrets.token_urlsafe(32)
            with lock:
                for key in list(sessions):
                    if sessions[key] < time.time():
                        del sessions[key]
                sessions[digest(token)] = time.time() + 8 * 3600
            return self.respond({'ok': True}, cookie=f'bf_admin={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800' + ('; Secure' if SECURE else ''))
        if path == '/api/admin/logout' and post:
            key = self.admin()
            with lock:
                sessions.pop(key, None)
            return self.respond({'ok': True}, cookie='bf_admin=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0' + ('; Secure' if SECURE else ''))
        if path == '/api/tickets' and post:
            self.limited('create', 5, 3600)
            values = {}
            for field, size in [('name', 80), ('email', 150), ('country', 80), ('language', 20), ('service', 30)]:
                value = p.get(field, '')
                if not isinstance(value, str) or not value.strip() or len(value) > size:
                    raise RequestError('Please complete required fields / 请完整填写必要信息')
                values[field] = value.strip()
            if not re.fullmatch(r'[^\s@\r\n]+@[^\s@\r\n]+\.[^\s@\r\n]+', values['email']) or values['service'] not in SERVICES or values['language'] not in ('zh', 'en') or p.get('consent') is not True:
                raise RequestError('Invalid fields or missing consent / 请检查信息并确认告知')
            ticket_id = 'BF-' + secrets.token_hex(5).upper()
            token = secrets.token_urlsafe(32)
            now = time.time()
            with db() as c:
                c.execute('INSERT INTO tickets(id,token_hash,name,email,country,language,service,created,updated,consent_version) VALUES(?,?,?,?,?,?,?,?,?,?)', (ticket_id, digest(token), values['name'], values['email'], values['country'], values['language'], values['service'], now, now, 'intake-v1-2026-09-25'))
                c.execute('INSERT INTO audit(ticket_id,action,created) VALUES(?,?,?)', (ticket_id, 'created', now))
            state = notify(ticket_id, 'admin', values['email'])
            return self.respond({'id': ticket_id, 'token': token, 'notification': state}, 201)
        if path == '/api/admin/tickets' and not post:
            self.admin()
            with db() as c:
                rows = c.execute('SELECT id,name,email,country,language,service,status,created,updated,admin_seen FROM tickets ORDER BY updated DESC LIMIT 200').fetchall()
            return self.respond({'tickets': [dict(r) for r in rows]})
        match = re.fullmatch(r'/api/(admin/)?tickets/(BF-[0-9A-F]{10})(/messages|/status|/notify)?', path)
        if not match:
            raise RequestError('Not found', 404)
        admin, ticket_id, action = bool(match[1]), match[2], match[3]
        with db() as c:
            row = self.ticket(c, ticket_id, admin)
            if not post and not action:
                result = {k: row[k] for k in ('id', 'name', 'email', 'country', 'language', 'service', 'status', 'created', 'updated')}
                result['messages'] = [dict(x) for x in c.execute('SELECT role,body,created FROM messages WHERE ticket_id=? ORDER BY id', (ticket_id,))]
                seen = 'admin_seen' if admin else 'customer_seen'
                c.execute(f'UPDATE tickets SET {seen}=? WHERE id=?', (time.time(), ticket_id))
                if admin:
                    result['notifications'] = [dict(x) for x in c.execute('SELECT audience,status,created FROM notifications WHERE ticket_id=? ORDER BY id DESC LIMIT 10', (ticket_id,))]
                return self.respond(result)
            if post and action == '/status' and admin:
                status = p.get('status')
                if status not in STATUSES:
                    raise RequestError('Invalid status')
                c.execute('UPDATE tickets SET status=?,updated=? WHERE id=?', (status, time.time(), ticket_id))
                c.execute('INSERT INTO audit(ticket_id,action,created) VALUES(?,?,?)', (ticket_id, 'status:' + status, time.time()))
            elif post and action == '/messages':
                self.limited('message', 30, 3600)
                body = p.get('body', '')
                if not isinstance(body, str) or not body.strip() or len(body) > 2000:
                    raise RequestError('Message must contain 1–2000 characters / 请填写1至2000字')
                if row['status'] == 'closed':
                    raise RequestError('Inquiry closed; contact the team to reopen / 咨询已结束，请联系团队重新开启', 409)
                now = time.time()
                c.execute('INSERT INTO messages(ticket_id,role,body,created) VALUES(?,?,?,?)', (ticket_id, 'admin' if admin else 'customer', body.strip(), now))
                c.execute('UPDATE tickets SET updated=?,status=? WHERE id=?', (now, 'waiting_customer' if admin else 'reviewing', ticket_id))
                c.execute('INSERT INTO audit(ticket_id,action,created) VALUES(?,?,?)', (ticket_id, 'admin_reply' if admin else 'customer_reply', now))
            elif not (post and action == '/notify' and admin):
                raise RequestError('Not found', 404)
        audience = 'customer' if admin else 'admin'
        if action == '/notify':
            self.limited('notify', 10, 3600)
            audience = p.get('audience', 'customer')
            if audience not in ('admin', 'customer'):
                raise RequestError('Invalid audience')
        state = notify(ticket_id, audience, row['email'])
        self.respond({'ok': True, 'notification': state})

if __name__ == '__main__':
    init()
    print(f'Service: {BASE}\nAdmin: {BASE}/admin.html\nLocal password file (if not configured): {DATA}/admin-password.txt', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
