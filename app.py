"""Private Lessons with Coach Dacia: stdlib HTTP app backed by SQLite."""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo
from database import connect

ROOT = Path(__file__).parent
TZ = ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "America/Los_Angeles"))
POLICY = [
    "At least 12 hours' notice is required to cancel or reschedule.",
    "Cancellations made less than 12 hours before the lesson and no-shows are charged the full lesson price.",
    "Gymnasts should arrive approximately 10 minutes before their lesson to warm up and be ready to begin.",
    "Lessons begin at the scheduled time. Arriving late may result in a shorter lesson.",
    "If my schedule allows, I may extend a lesson so the gymnast receives their full lesson duration after arriving late, but this is not guaranteed.",
    "Private lessons are supplemental to regular gymnastics practice and are not a replacement for mandatory practice.",
    "Gymnasts are expected to consistently put in effort and take regular practices seriously. Private lesson privileges may be paused if these expectations are not being met.",
]
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS parents(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gymnasts(id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parents(id), name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lesson_types(id INTEGER PRIMARY KEY, minutes INTEGER UNIQUE NOT NULL, price_cents INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS availability(id INTEGER PRIMARY KEY, weekday INTEGER, specific_date TEXT, start_time TEXT NOT NULL, end_time TEXT NOT NULL, CHECK ((weekday IS NULL) != (specific_date IS NULL)));
CREATE TABLE IF NOT EXISTS blocked_times(id INTEGER PRIMARY KEY, start_at TEXT NOT NULL, end_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_versions(id TEXT PRIMARY KEY, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parents(id), gymnast_id INTEGER NOT NULL REFERENCES gymnasts(id), lesson_type_id INTEGER NOT NULL REFERENCES lesson_types(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL, notes TEXT, policy_version TEXT NOT NULL, policy_accepted_at TEXT NOT NULL, manage_token_hash TEXT NOT NULL, hold_expires_at TEXT, stripe_session_id TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, booking_id TEXT NOT NULL REFERENCES bookings(id), stripe_payment_id TEXT UNIQUE, stripe_session_id TEXT UNIQUE, amount_cents INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders(booking_id TEXT PRIMARY KEY REFERENCES bookings(id), sent_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bookings_times ON bookings(start_at,end_at,status);
"""


def db():
    return connect()


def initialize():
    if os.environ.get("DATABASE_URL"):
        # Hosted schema is applied once using supabase_schema.sql.
        return
    with db() as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO lesson_types(minutes,price_cents,active) VALUES(30,4000,1),(40,5300,1)")
        if not c.execute("SELECT 1 FROM settings WHERE key='price_update_2026_10'").fetchone():
            c.execute("UPDATE lesson_types SET price_cents=CASE minutes WHEN 30 THEN 4000 WHEN 40 THEN 5300 END,active=1 WHERE minutes IN (30,40)")
            c.execute("INSERT INTO settings VALUES('price_update_2026_10','done')")
        c.execute("INSERT OR IGNORE INTO settings VALUES('buffer_minutes','0')")
        version = hashlib.sha256(json.dumps(POLICY).encode()).hexdigest()[:16]
        c.execute("INSERT OR IGNORE INTO policy_versions VALUES(?,?)", (version, json.dumps(POLICY)))
        c.execute("INSERT OR IGNORE INTO settings VALUES('policy_version',?)", (version,))


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_dt(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def manage_token(booking_id):
    secret = os.environ.get("MANAGE_LINK_SECRET")
    if not secret: raise ValueError("Manage link secret is not configured")
    return hmac.new(secret.encode(), booking_id.encode(), hashlib.sha256).hexdigest()


def payment_method():
    return os.environ.get("PAYMENT_METHOD", "stripe").strip().lower()


def venmo_url():
    username = os.environ.get("VENMO_USERNAME", "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", username):
        return ""
    return "https://venmo.com/u/" + username


def overlap(a, b, c, d):
    return a < d and c < b


def settings(c):
    return {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}


def current_policy(c):
    row = c.execute("SELECT p.id,p.content FROM policy_versions p JOIN settings s ON s.value=p.id WHERE s.key='policy_version'").fetchone()
    return row["id"], json.loads(row["content"])


def available(c, start, minutes, exclude=None):
    if start < utc_now() + timedelta(minutes=5):
        return False
    end = start + timedelta(minutes=minutes)
    local_start, local_end = start.astimezone(TZ), end.astimezone(TZ)
    if local_start.date() != local_end.date():
        return False
    day, clock_start, clock_end = local_start.date().isoformat(), local_start.strftime("%H:%M"), local_end.strftime("%H:%M")
    windows = c.execute("SELECT * FROM availability WHERE specific_date=? OR weekday=?", (day, local_start.weekday())).fetchall()
    if not any(w["start_time"] <= clock_start and clock_end <= w["end_time"] for w in windows):
        return False
    buffer = int(settings(c).get("buffer_minutes", "0"))
    for r in c.execute("SELECT start_at,end_at FROM blocked_times"):
        if overlap(start, end, parse_dt(r[0]), parse_dt(r[1])):
            return False
    for r in c.execute("SELECT id,start_at,end_at,status,hold_expires_at FROM bookings WHERE status IN ('confirmed','pending')"):
        if r["id"] == exclude or (r["status"] == "pending" and r["hold_expires_at"] and parse_dt(r["hold_expires_at"]) < utc_now()):
            continue
        if overlap(start, end, parse_dt(r["start_at"]) - timedelta(minutes=buffer), parse_dt(r["end_at"]) + timedelta(minutes=buffer)):
            return False
    return True


def stripe_post(path, data):
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise ValueError("Payments are not configured yet")
    req = urllib.request.Request("https://api.stripe.com/v1/" + path, urllib.parse.urlencode(data).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/x-www-form-urlencoded", "Idempotency-Key": data.get("metadata[booking_id]", secrets.token_hex(12))})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def send_confirmation(c, booking_id, token, reminder=False):
    row = c.execute("SELECT b.*,p.email,g.name gymnast,l.minutes,pm.amount_cents price_cents FROM bookings b JOIN parents p ON p.id=b.parent_id JOIN gymnasts g ON g.id=b.gymnast_id JOIN lesson_types l ON l.id=b.lesson_type_id JOIN payments pm ON pm.booking_id=b.id WHERE b.id=?", (booking_id,)).fetchone()
    if not row or not os.environ.get("SMTP_HOST"):
        return
    local = parse_dt(row["start_at"]).astimezone(TZ)
    url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/manage?token=" + token
    msg = EmailMessage()
    msg["Subject"] = "Reminder: lesson with Coach Dacia tomorrow" if reminder else "Your lesson with Coach Dacia is confirmed"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = row["email"]
    policies = json.loads(c.execute("SELECT content FROM policy_versions WHERE id=?", (row["policy_version"],)).fetchone()[0])
    msg.set_content(f"{row['gymnast']}'s {row['minutes']}-minute lesson is {'coming up' if reminder else 'confirmed'} for {local:%A, %B %d at %I:%M %p} {TZ.key}. Amount paid: ${row['price_cents']/100:.2f}.\n\nCancel or reschedule at least 12 hours ahead: {url}\n\n" + "\n".join(policies))
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value, headers=None):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536: raise ValueError("Request too large")
        return self.rfile.read(length)

    def json_body(self):
        return json.loads(self.body())

    def admin(self):
        secret = os.environ.get("ADMIN_PASSWORD")
        cookie = next((x.split("=", 1)[1] for x in self.headers.get("Cookie", "").split("; ") if x.startswith("admin_session=")), "")
        try:
            payload, sig = cookie.rsplit(".", 1)
            expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            return bool(secret and hmac.compare_digest(sig, expected) and int(payload) > time.time())
        except (ValueError, AttributeError): return False

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(path.query)
        try:
            if path.path == "/api/config":
                with db() as c:
                    types = [dict(r) for r in c.execute("SELECT id,minutes,price_cents FROM lesson_types WHERE active=1 AND price_cents>0 ORDER BY minutes")]
                    policy_version, policies = current_policy(c)
                method = payment_method()
                ready = bool(os.environ.get("MANAGE_LINK_SECRET") and (venmo_url() if method == "venmo" else (os.environ.get("STRIPE_SECRET_KEY") and os.environ.get("STRIPE_WEBHOOK_SECRET") and os.environ.get("PUBLIC_BASE_URL")) if method == "stripe" else False))
                return self.reply(200, {"lesson_types": types, "policies": policies, "policy_version": policy_version, "timezone": TZ.key, "contact_email": os.environ.get("CONTACT_EMAIL", ""), "payment_method": method, "venmo_url": venmo_url() if method == "venmo" else "", "payments_ready": ready})
            if path.path == "/api/slots":
                day = datetime.strptime(q.get("date", [""])[0], "%Y-%m-%d").date()
                today = utc_now().astimezone(TZ).date()
                if not (today <= day <= today + timedelta(days=90)): raise ValueError("Choose a date in the next 90 days")
                with db() as c:
                    types = c.execute("SELECT minutes FROM lesson_types WHERE active=1 AND price_cents>0").fetchall()
                    slots = []
                    for hour in range(6, 22):
                        for minute in (0, 10, 20, 30, 40, 50):
                            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
                            start = local.astimezone(timezone.utc)
                            durations = [r[0] for r in types if available(c, start, r[0])]
                            if durations: slots.append({"start_at": iso(start), "label": local.strftime("%I:%M %p").lstrip("0"), "durations": durations})
                return self.reply(200, {"slots": slots})
            if path.path == "/api/manage":
                token = q.get("token", [""])[0]
                with db() as c:
                    r = c.execute("SELECT b.id,b.start_at,b.end_at,b.status,b.notes,b.stripe_session_id,g.name gymnast,p.name parent,l.minutes,pm.amount_cents price_cents FROM bookings b JOIN gymnasts g ON g.id=b.gymnast_id JOIN parents p ON p.id=b.parent_id JOIN lesson_types l ON l.id=b.lesson_type_id JOIN payments pm ON pm.booking_id=b.id WHERE b.manage_token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                if not r: return self.reply(404, {"error": "Booking not found"})
                data = dict(r); data["can_change"] = r["status"] == "confirmed" and parse_dt(r["start_at"]) > utc_now() + timedelta(hours=12)
                data["payment_method"] = "stripe" if r["stripe_session_id"] else "venmo"
                data["venmo_url"] = venmo_url() if data["payment_method"] == "venmo" and r["status"] == "pending" else ""
                data.pop("stripe_session_id", None)
                return self.reply(200, data)
            if path.path == "/api/admin":
                if not self.admin(): return self.reply(401, {"error": "Sign in required"})
                with db() as c:
                    bookings = [dict(r) for r in c.execute("SELECT b.id,b.start_at,b.end_at,b.status,b.stripe_session_id,b.notes,p.name parent,p.email,g.name gymnast,l.minutes,pm.amount_cents price_cents,pm.status payment_status FROM bookings b JOIN parents p ON p.id=b.parent_id JOIN gymnasts g ON g.id=b.gymnast_id JOIN lesson_types l ON l.id=b.lesson_type_id LEFT JOIN payments pm ON pm.booking_id=b.id ORDER BY b.start_at DESC LIMIT 500")]
                    revenue = c.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE status='paid'").fetchone()[0]
                    data = {"bookings": bookings, "revenue_cents": revenue, "lesson_types": [dict(r) for r in c.execute("SELECT * FROM lesson_types")], "availability": [dict(r) for r in c.execute("SELECT * FROM availability")], "blocked": [dict(r) for r in c.execute("SELECT * FROM blocked_times")], "settings": settings(c), "policies": current_policy(c)[1]}
                return self.reply(200, data)
            if path.path in ("/", "/book", "/admin", "/manage", "/success"):
                content = (ROOT / "public" / "index.html").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content); return
            if path.path in ("/style.css", "/theme.css", "/app.js"):
                content = (ROOT / "public" / path.path[1:]).read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/css" if path.path.endswith("css") else "text/javascript"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content); return
            return self.reply(404, {"error": "Not found"})
        except (ValueError, sqlite3.Error) as e: return self.reply(400, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/stripe/webhook": return self.webhook()
            data = self.json_body()
            if path == "/api/checkout": return self.checkout(data)
            if path == "/api/admin/login":
                email = os.environ.get("ADMIN_EMAIL", "").strip().casefold()
                password = os.environ.get("ADMIN_PASSWORD", "")
                if not email or not password: return self.reply(503, {"error": "Coach login is not configured yet"})
                supplied_email = str(data.get("email", "")).strip().casefold()
                supplied_password = str(data.get("password", ""))
                if not (hmac.compare_digest(supplied_email, email) and hmac.compare_digest(supplied_password, password)):
                    return self.reply(401, {"error": "Invalid email or password"})
                expiry = str(int(time.time() + 8 * 3600)); sig = hmac.new(password.encode(), expiry.encode(), hashlib.sha256).hexdigest()
                return self.reply(200, {"ok": True}, {"Set-Cookie": f"admin_session={expiry}.{sig}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800" + ("; Secure" if os.environ.get("PUBLIC_BASE_URL", "").startswith("https:") else "")})
            if path.startswith("/api/admin/"):
                if not self.admin(): return self.reply(401, {"error": "Sign in required"})
                return self.admin_write(path, data)
            if path == "/api/manage/change": return self.manage_change(data)
            return self.reply(404, {"error": "Not found"})
        except (ValueError, KeyError, sqlite3.Error) as e: return self.reply(400, {"error": str(e)})

    def checkout(self, d):
        method = payment_method()
        if method == "venmo":
            if not venmo_url() or not os.environ.get("MANAGE_LINK_SECRET"):
                raise ValueError("Venmo booking is being set up. Please check back soon")
        elif method == "stripe":
            if not os.environ.get("STRIPE_SECRET_KEY") or not os.environ.get("STRIPE_WEBHOOK_SECRET") or not os.environ.get("PUBLIC_BASE_URL") or not os.environ.get("MANAGE_LINK_SECRET"):
                raise ValueError("Online booking is being set up. Please check back soon")
        else:
            raise ValueError("Payment method is not configured")
        if d.get("agree") is not True: raise ValueError("Please agree to the lesson policies")
        names = [str(d.get(k, "")).strip() for k in ("parent", "gymnast", "email", "phone")]
        if any(not x or len(x) > 120 for x in names) or "@" not in names[2]: raise ValueError("Enter valid contact details")
        notes = str(d.get("notes", ""))[:1000]
        start = parse_dt(d["start_at"]); minutes = int(d["minutes"])
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            lesson = c.execute("SELECT * FROM lesson_types WHERE minutes=? AND active=1 AND price_cents>0", (minutes,)).fetchone()
            if not lesson or not available(c, start, minutes): raise ValueError("This time is no longer available")
            c.execute("INSERT INTO parents(name,email,phone) VALUES(?,?,?)", (names[0], names[2].lower(), names[3])); parent = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO gymnasts(parent_id,name) VALUES(?,?)", (parent, names[1])); gymnast = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            booking_id = secrets.token_urlsafe(18); token = manage_token(booking_id)
            policy_version, _ = current_policy(c)
            if d.get("policy_version") != policy_version: raise ValueError("Policies changed. Refresh and review them before booking")
            c.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (booking_id, parent, gymnast, lesson["id"], iso(start), iso(start + timedelta(minutes=minutes)), "pending", notes, policy_version, iso(utc_now()), hashlib.sha256(token.encode()).hexdigest(), iso(utc_now() + timedelta(minutes=45)) if method == "stripe" else None, None, iso(utc_now())))
            c.execute("INSERT INTO payments(booking_id,amount_cents,status) VALUES(?,?,?)", (booking_id, lesson["price_cents"], "pending"))
            c.commit()
        base = os.environ.get("PUBLIC_BASE_URL", f"http://{self.headers.get('Host','localhost:8000')}").rstrip("/")
        if method == "venmo":
            return self.reply(200, {"manage_url": base + "/manage?token=" + token, "venmo_url": venmo_url(), "reference": booking_id, "amount_cents": lesson["price_cents"], "status": "pending"})
        try:
            session = stripe_post("checkout/sessions", {"mode": "payment", "customer_email": names[2], "line_items[0][price_data][currency]": "usd", "line_items[0][price_data][unit_amount]": str(lesson["price_cents"]), "line_items[0][price_data][product_data][name]": f"{minutes}-minute private gymnastics lesson", "line_items[0][quantity]": "1", "success_url": base + "/success?booking=" + booking_id + "&token=" + token, "cancel_url": base + "/book?cancelled=1", "client_reference_id": booking_id, "metadata[booking_id]": booking_id, "expires_at": str(int(time.time() + 1800))})
        except Exception:
            with db() as c: c.execute("UPDATE bookings SET status='payment_failed' WHERE id=?", (booking_id,))
            raise ValueError("Checkout could not start. Please try again")
        with db() as c:
            c.execute("UPDATE bookings SET stripe_session_id=? WHERE id=?", (session["id"], booking_id))
            c.execute("UPDATE payments SET stripe_session_id=? WHERE booking_id=?", (session["id"], booking_id))
        return self.reply(200, {"checkout_url": session["url"]})

    def webhook(self):
        raw = self.body(); secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        signature = self.headers.get("Stripe-Signature", "")
        parts = dict(p.split("=", 1) for p in signature.split(",") if "=" in p)
        timestamp = int(parts.get("t", "0"))
        expected = hmac.new(secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
        if not secret or abs(time.time() - timestamp) > 300 or not any(hmac.compare_digest(expected, p[3:]) for p in signature.split(",") if p.startswith("v1=")):
            return self.reply(400, {"error": "Invalid signature"})
        event = json.loads(raw); obj = event.get("data", {}).get("object", {})
        confirmed_id = None
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM webhook_events WHERE id=?", (event["id"],)).fetchone(): return self.reply(200, {"ok": True})
            if event["type"] == "checkout.session.completed" and obj.get("payment_status") == "paid":
                booking = c.execute("SELECT b.*,l.minutes,pm.amount_cents price_cents FROM bookings b JOIN payments pm ON pm.booking_id=b.id JOIN lesson_types l ON l.id=b.lesson_type_id WHERE b.id=? AND (b.stripe_session_id=? OR b.stripe_session_id IS NULL)", (obj.get("client_reference_id"), obj.get("id"))).fetchone()
                if booking and obj.get("amount_total") == booking["price_cents"] and obj.get("currency") == "usd":
                    c.execute("UPDATE bookings SET stripe_session_id=COALESCE(stripe_session_id,?) WHERE id=?", (obj.get("id"), booking["id"]))
                    c.execute("UPDATE payments SET stripe_session_id=COALESCE(stripe_session_id,?) WHERE booking_id=?", (obj.get("id"), booking["id"]))
                    conflict = any(overlap(parse_dt(booking["start_at"]), parse_dt(booking["end_at"]), parse_dt(r["start_at"]), parse_dt(r["end_at"])) for r in c.execute("SELECT start_at,end_at FROM bookings WHERE id<>? AND status='confirmed'", (booking["id"],)))
                    changed = c.execute("UPDATE bookings SET status=? WHERE id=? AND status='pending'", ("payment_conflict" if conflict else "confirmed", booking["id"])).rowcount
                    c.execute("UPDATE payments SET status='paid',stripe_payment_id=? WHERE booking_id=?", (obj.get("payment_intent"), booking["id"]))
                    if changed and not conflict: confirmed_id = booking["id"]
            c.execute("INSERT INTO webhook_events VALUES(?,?)", (event["id"], iso(utc_now())))
            c.commit()
            if confirmed_id:
                try: send_confirmation(c, confirmed_id, manage_token(confirmed_id))
                except Exception as e: print("Confirmation email failed:", e)
        return self.reply(200, {"ok": True})

    def manage_change(self, d):
        token = str(d.get("token", "")); action = d.get("action")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            b = c.execute("SELECT b.*,l.minutes FROM bookings b JOIN lesson_types l ON l.id=b.lesson_type_id WHERE manage_token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not b: return self.reply(404, {"error": "Booking not found"})
            if b["status"] != "confirmed" or parse_dt(b["start_at"]) <= utc_now() + timedelta(hours=12): return self.reply(403, {"error": "Online changes close 12 hours before your lesson. Please contact Coach Dacia directly."})
            if action == "cancel": c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
            elif action == "reschedule":
                start = parse_dt(d["start_at"])
                if not available(c, start, b["minutes"], b["id"]): raise ValueError("Time unavailable")
                c.execute("UPDATE bookings SET start_at=?,end_at=? WHERE id=?", (iso(start), iso(start + timedelta(minutes=b["minutes"])), b["id"]))
            else: raise ValueError("Unknown action")
            c.commit()
        return self.reply(200, {"ok": True})

    def admin_write(self, path, d):
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            if path == "/api/admin/price":
                minutes, cents = int(d["minutes"]), int(d["price_cents"])
                if minutes not in (30, 40) or not 100 <= cents <= 100000: raise ValueError("Invalid price")
                c.execute("UPDATE lesson_types SET price_cents=?,active=1 WHERE minutes=?", (cents, minutes))
            elif path == "/api/admin/availability":
                weekday = d.get("weekday"); day = d.get("specific_date")
                start, end = d["start_time"], d["end_time"]
                if (weekday is None) == (day is None) or not "00:00" <= start < end <= "23:59": raise ValueError("Invalid availability")
                if weekday is not None and int(weekday) not in range(7): raise ValueError("Invalid weekday")
                if day: datetime.strptime(day, "%Y-%m-%d")
                c.execute("INSERT INTO availability(weekday,specific_date,start_time,end_time) VALUES(?,?,?,?)", (weekday, day, start, end))
            elif path == "/api/admin/block":
                start, end = parse_dt(d["start_at"]), parse_dt(d["end_at"])
                if end <= start: raise ValueError("Invalid block")
                c.execute("INSERT INTO blocked_times(start_at,end_at) VALUES(?,?)", (iso(start), iso(end)))
            elif path == "/api/admin/buffer":
                minutes = int(d["minutes"])
                if minutes not in range(0, 121): raise ValueError("Invalid buffer")
                c.execute("UPDATE settings SET value=? WHERE key='buffer_minutes'", (str(minutes),))
            elif path == "/api/admin/policies":
                lines = [str(line).strip() for line in d.get("policies", [])]
                if not 1 <= len(lines) <= 20 or any(not line or len(line) > 500 for line in lines): raise ValueError("Enter 1–20 policy statements")
                content = json.dumps(lines)
                version = hashlib.sha256(content.encode()).hexdigest()[:16]
                c.execute("INSERT OR IGNORE INTO policy_versions VALUES(?,?)", (version, content))
                c.execute("UPDATE settings SET value=? WHERE key='policy_version'", (version,))
            elif path == "/api/admin/booking":
                b = c.execute("SELECT b.*,l.minutes FROM bookings b JOIN lesson_types l ON l.id=b.lesson_type_id WHERE b.id=?", (d["id"],)).fetchone()
                if not b: raise ValueError("Booking not found")
                if d["action"] == "confirm_payment":
                    if b["status"] != "pending" or b["stripe_session_id"]:
                        raise ValueError("Only pending Venmo requests can be confirmed here")
                    conflict = c.execute("SELECT 1 FROM bookings WHERE id<>? AND status='confirmed' AND start_at<? AND end_at>? LIMIT 1", (b["id"], b["end_at"], b["start_at"])).fetchone()
                    if conflict: raise ValueError("Another confirmed lesson overlaps this time")
                    c.execute("UPDATE bookings SET status='confirmed',hold_expires_at=NULL WHERE id=?", (b["id"],))
                    c.execute("UPDATE payments SET status='paid' WHERE booking_id=?", (b["id"],))
                elif d["action"] == "cancel":
                    c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
                    if b["status"] == "pending": c.execute("UPDATE payments SET status='cancelled' WHERE booking_id=?", (b["id"],))
                elif d["action"] == "reschedule":
                    start = parse_dt(d["start_at"])
                    if not available(c, start, b["minutes"], b["id"]): raise ValueError("Time unavailable")
                    c.execute("UPDATE bookings SET start_at=?,end_at=? WHERE id=?", (iso(start), iso(start + timedelta(minutes=b["minutes"])), b["id"]))
                else: raise ValueError("Unknown action")
            elif path == "/api/admin/delete-rule":
                table = {"availability": "availability", "blocked": "blocked_times"}.get(d.get("kind"))
                if not table: raise ValueError("Invalid rule")
                c.execute(f"DELETE FROM {table} WHERE id=?", (int(d["id"]),))
            else: return self.reply(404, {"error": "Not found"})
            c.commit()
            if path == "/api/admin/booking" and d["action"] == "confirm_payment":
                try: send_confirmation(c, b["id"], manage_token(b["id"]))
                except Exception as error: print("Confirmation email failed:", error)
        return self.reply(200, {"ok": True})


def send_due_reminders():
    """Run hourly from cron or a scheduler; sends once, about 24 hours before."""
    if not os.environ.get("SMTP_HOST"): return
    with db() as c:
        start, end = iso(utc_now() + timedelta(hours=23)), iso(utc_now() + timedelta(hours=25))
        rows = c.execute("SELECT id FROM bookings WHERE status='confirmed' AND start_at BETWEEN ? AND ? AND id NOT IN (SELECT booking_id FROM reminders)", (start, end)).fetchall()
        for row in rows:
            send_confirmation(c, row["id"], manage_token(row["id"]), reminder=True)
            c.execute("INSERT OR IGNORE INTO reminders VALUES(?,?)", (row["id"], iso(utc_now())))


try:
    from flask import Flask, Response, request
except ImportError:
    app = None  # Local standard-library server does not require Flask.
else:
    app = Flask(__name__, static_folder=None)

    class FlaskHandler(Handler):
        """Use the same validated routes from Vercel's Flask runtime."""

        def __init__(self):
            self.path = request.full_path
            self.headers = request.headers
            self.rfile = io.BytesIO(request.get_data())
            self.wfile = io.BytesIO()
            self.response_status = 200
            self.response_headers = []

        def send_response(self, code, message=None):
            self.response_status = code

        def send_header(self, keyword, value):
            self.response_headers.append((keyword, value))

        def end_headers(self):
            pass

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
    @app.route("/<path:path>", methods=["GET", "POST"])
    def vercel_route(path):
        if not os.environ.get("DATABASE_URL") and os.environ.get("ALLOW_LOCAL_FLASK_SQLITE") != "1":
            return Response("Database is not configured", status=503)
        handler = FlaskHandler()
        if request.method == "POST":
            handler.do_POST()
        else:
            handler.do_GET()
        return Response(handler.wfile.getvalue(), status=handler.response_status, headers=handler.response_headers)


if __name__ == "__main__":
    initialize()
    if "--send-reminders" in os.sys.argv:
        send_due_reminders()
        raise SystemExit(0)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Serving on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
