"""
Multi-Mess Management System - Flask + SQLite
Roles:  admin (adds messes / students, allocates)  |  mess (supervisor login, scans QR)  |  student
Run:  python app.py      (production: see DEPLOY.md)
"""
import os, re, io, csv, sqlite3, secrets, calendar, time, ipaddress, math, zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, jsonify, Response, abort, send_file)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
import segno
from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MESS_DB", os.path.join(BASE, "mess.db"))
PHOTO_DIR = os.environ.get("MESS_PHOTOS", os.path.join(BASE, "photos"))
BRAND_DIR = os.environ.get("MESS_BRAND", os.path.join(BASE, "branding"))
LOGO_PATH = os.path.join(BRAND_DIR, "logo.png")
MEALS = ["breakfast", "lunch", "dinner"]
MEAL_META = {
    "breakfast": {"label": "Breakfast", "color": "#f59e0b", "icon": "🍳"},
    "lunch":     {"label": "Lunch",     "color": "#16a34a", "icon": "🍛"},
    "dinner":    {"label": "Dinner",    "color": "#4f46e5", "icon": "🌙"},
}
DEFAULTS = {
    "org_name": "University Mess System",
    "breakfast_start": "06:00", "breakfast_end": "11:00",
    "lunch_start": "12:00",     "lunch_end": "16:00",
    "dinner_start": "19:00",    "dinner_end": "23:00",
}
PER_PAGE = 50

app = Flask(__name__)
# behind Cloudflare Tunnel: trust forwarded https + host so QR links use the public https address
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024      # allows a bulk photo ZIP
_sk = os.path.join(BASE, ".secret_key")
if not os.path.exists(_sk):
    with open(_sk, "w") as f:
        f.write(secrets.token_hex(32))
app.secret_key = open(_sk).read().strip()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# ---------------------------------------------------------------- database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows

def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur

def hash_pw(pw, fast=False):
    # "fast" is used only for bulk-imported initial passwords (students must change them at first login)
    return generate_password_hash(pw, method="pbkdf2:sha256:10000" if fast else "pbkdf2:sha256:200000")

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS messes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        allowed_ips TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roll TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'student',      -- admin | mess | student
        mess_id INTEGER,                           -- student: allotted mess, mess: the mess he runs
        active INTEGER NOT NULL DEFAULT 1,
        must_change INTEGER NOT NULL DEFAULT 1);
    CREATE INDEX IF NOT EXISTS ix_users_mess ON users(role, mess_id);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS absences(         -- "I will NOT come" (default = student comes)
        student_id INTEGER NOT NULL, date TEXT NOT NULL, meal TEXT NOT NULL,
        PRIMARY KEY(student_id, date, meal));
    CREATE INDEX IF NOT EXISTS ix_abs ON absences(date, meal);
    CREATE TABLE IF NOT EXISTS passes(           -- daily QR / code
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL, mess_id INTEGER NOT NULL,
        date TEXT NOT NULL, meal TEXT NOT NULL,
        code TEXT NOT NULL, created_at TEXT NOT NULL, used_at TEXT,
        UNIQUE(student_id, date, meal), UNIQUE(date, code));
    CREATE INDEX IF NOT EXISTS ix_pass ON passes(date, mess_id, meal);
    CREATE TABLE IF NOT EXISTS unlock_tickets(
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_unlock_ticket_expiry ON unlock_tickets(expires_at, used_at);
    """)
    for k, v in DEFAULTS.items():
        con.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        con.execute("INSERT INTO users(roll,name,pw_hash,role) VALUES(?,?,?,'admin')",
                    ("admin", "Administrator", hash_pw("admin123")))
        print("*** Default admin created ->  user: admin   password: admin123  (you must change it at first login) ***")
    con.commit()
    con.close()

def setting(key):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else DEFAULTS.get(key, "")

def current_meal():
    now = datetime.now().strftime("%H:%M")
    for m in MEALS:
        if setting(m + "_start") <= now <= setting(m + "_end"):
            return m
    return None


# ---------------------------------------------------------------- network checks
def client_ip():
    """Real visitor IP. Header trusted only when the request comes from this machine (cloudflared)."""
    ra = request.remote_addr or ""
    if ra in ("127.0.0.1", "::1"):
        return (request.headers.get("CF-Connecting-IP")
                or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or ra)
    return ra

def in_mess_network(mess):
    """May this visitor create a pass for this mess? (mess.allowed_ips blank = no restriction)"""
    allowed = [x.strip() for x in (mess["allowed_ips"] or "").split(",") if x.strip()]
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address(client_ip())
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback:      # direct access over the mess LAN
        return True
    for a in allowed:
        try:
            if addr in ipaddress.ip_network(a, strict=False):
                return True
        except ValueError:
            continue
    return False

FAILS = {}
def too_many_fails(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    return len(FAILS[ip]) >= 8


# ---------------------------------------------------------------- auth
def current_user():
    if "_user" not in g:
        uid = session.get("uid")
        g._user = q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True) if uid else None
    return g._user

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.path))
            if u["role"] not in roles:
                flash("You do not have access to that page.", "err")
                return redirect(url_for("index"))
            return f(*a, **k)
        return w
    return deco

login_required = role_required("admin", "mess", "student")

@app.before_request
def force_password_change():
    if request.endpoint in (None, "static", "login", "logout", "password", "logo"):
        return
    u = current_user()
    if u and u["must_change"]:
        flash("Please set a new password to continue.", "err")
        return redirect(url_for("password"))

@app.context_processor
def inject():
    u = current_user()
    brand = setting("org_name")
    if u and u["role"] == "mess" and u["mess_id"]:
        m = q("SELECT name FROM messes WHERE id=?", (u["mess_id"],), one=True)
        if m:
            brand = m["name"]
    return dict(brand=brand, org_name=setting("org_name"), user=u, MEALS=MEALS, META=MEAL_META,
                photo_v=photo_version, logo_v=logo_version())

HOME = {"admin": "admin", "mess": "mess_home", "student": "student"}

@app.route("/")
def index():
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    return redirect(url_for(HOME[u["role"]]))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if too_many_fails(client_ip()):
            flash("Too many wrong attempts. Try again after 10 minutes.", "err")
            return render_template("login.html")
        roll = request.form.get("roll", "").strip()
        u = q("SELECT * FROM users WHERE roll=? AND active=1", (roll,), one=True)
        if u and check_password_hash(u["pw_hash"], request.form.get("password", "")):
            if "MessSupervisorApp" in request.headers.get("User-Agent", "") and u["role"] != "mess":
                flash("This app is for mess supervisors only. Please use the website.", "err")
                return render_template("login.html")
            session.clear()
            session["uid"] = u["id"]
            session.permanent = True
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("index"))
        FAILS.setdefault(client_ip(), []).append(time.time())
        flash("Wrong ID or password.", "err")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/password", methods=["GET", "POST"])
@login_required
def password():
    u = current_user()
    if request.method == "POST":
        new = request.form.get("new", "")
        if not check_password_hash(u["pw_hash"], request.form.get("old", "")):
            flash("Old password is wrong.", "err")
        elif len(new) < 6 or new == u["roll"]:
            flash("New password must be at least 6 characters and different from your ID.", "err")
        else:
            ex("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (hash_pw(new), u["id"]))
            flash("Password changed.", "ok")
            return redirect(url_for("index"))
    return render_template("password.html")


# ---------------------------------------------------------------- photos
def photo_path(uid):
    return os.path.join(PHOTO_DIR, f"{int(uid)}.jpg")

def photo_version(uid):
    """mtime of the student's photo (used to refresh browser cache) or None if no photo."""
    try:
        return int(os.path.getmtime(photo_path(uid)))
    except OSError:
        return None

def photo_ids():
    try:
        return {int(f[:-4]) for f in os.listdir(PHOTO_DIR) if f.endswith(".jpg") and f[:-4].isdigit()}
    except OSError:
        return set()

def save_photo(uid, data):
    """Resize to max 360px, fix phone rotation, store as small JPEG. Returns True on success."""
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        im.thumbnail((360, 360))
        os.makedirs(PHOTO_DIR, exist_ok=True)
        im.save(photo_path(uid), "JPEG", quality=78, optimize=True)
        return True
    except Exception:
        return False

def remove_photo(uid):
    try:
        os.remove(photo_path(uid))
    except OSError:
        pass

@app.route("/photo/<int:uid>")
@login_required
def photo(uid):
    u = current_user()
    if u["role"] == "student" and u["id"] != uid:
        abort(404)
    if u["role"] == "mess" and not q("SELECT 1 FROM users WHERE id=? AND mess_id=?", (uid, u["mess_id"]), one=True):
        abort(404)
    p = photo_path(uid)
    if not os.path.exists(p):
        abort(404)
    resp = send_file(p, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp

@app.post("/student/photo")
@role_required("student")
def student_photo():
    u = current_user()
    f = request.files.get("photo")
    if photo_version(u["id"]):
        flash("Your photo is already on record. Ask the admin if it must be changed.", "err")
    elif not f:
        flash("Choose a photo first.", "err")
    else:
        data = f.read()
        if len(data) > 10 * 1024 * 1024 or not save_photo(u["id"], data):
            flash("Could not read that image. Use a JPG or PNG under 10 MB.", "err")
        else:
            flash("Photo saved.", "ok")
    return redirect(url_for("student"))

@app.route("/admin/photo/<int:uid>", methods=["GET", "POST"])
@role_required("admin")
def admin_photo(uid):
    s = q("SELECT u.*, m.name AS mess_name FROM users u LEFT JOIN messes m ON m.id=u.mess_id "
          "WHERE u.id=? AND u.role='student'", (uid,), one=True)
    if not s:
        abort(404)
    if request.method == "POST":
        if request.form.get("act") == "delete":
            remove_photo(uid)
            flash("Photo removed.", "ok")
        else:
            f = request.files.get("photo")
            data = f.read() if f else b""
            if data and len(data) <= 10 * 1024 * 1024 and save_photo(uid, data):
                flash("Photo saved.", "ok")
            else:
                flash("Could not read that image. Use a JPG or PNG under 10 MB.", "err")
        return redirect(url_for("admin_photo", uid=uid))
    return render_template("photo.html", s=s)

@app.route("/admin/no_photo.csv")
@role_required("admin")
def no_photo_csv():
    have = photo_ids()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["roll", "name", "mess"])
    for r in q("SELECT u.id, u.roll, u.name, m.name AS mess FROM users u LEFT JOIN messes m ON m.id=u.mess_id "
               "WHERE u.role='student' AND u.active=1 ORDER BY u.roll"):
        if r["id"] not in have:
            w.writerow([r["roll"], r["name"], r["mess"] or ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=students_without_photo.csv"})


# ---------------------------------------------------------------- college logo
def logo_version():
    try:
        return int(os.path.getmtime(LOGO_PATH))
    except OSError:
        return None

def save_logo(data):
    """Store the college logo as a PNG (max 600 px, transparency kept)."""
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
        im.thumbnail((600, 600))
        os.makedirs(BRAND_DIR, exist_ok=True)
        im.save(LOGO_PATH, "PNG", optimize=True)
        return True
    except Exception:
        return False

@app.route("/logo")
def logo():
    if not os.path.exists(LOGO_PATH):
        abort(404)
    resp = send_file(LOGO_PATH, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ---------------------------------------------------------------- statistics
def mess_stats(d):
    """{mess_id: {name, registered, meals:{meal:{absent, expected, entered}}}} for date d (ISO)."""
    reg = {r["mess_id"]: r["c"] for r in q(
        "SELECT mess_id, COUNT(*) c FROM users WHERE role='student' AND active=1 AND mess_id IS NOT NULL "
        "GROUP BY mess_id")}
    ab = {(r["mess_id"], r["meal"]): r["c"] for r in q(
        "SELECT u.mess_id, a.meal, COUNT(*) c FROM absences a JOIN users u ON u.id=a.student_id "
        "WHERE a.date=? AND u.active=1 AND u.mess_id IS NOT NULL GROUP BY u.mess_id, a.meal", (d,))}
    en = {(r["mess_id"], r["meal"]): r["c"] for r in q(
        "SELECT mess_id, meal, COUNT(*) c FROM passes WHERE date=? AND used_at IS NOT NULL "
        "GROUP BY mess_id, meal", (d,))}
    out = {}
    for m in q("SELECT id, name FROM messes ORDER BY name"):
        r = reg.get(m["id"], 0)
        out[m["id"]] = dict(name=m["name"], registered=r, meals={
            meal: dict(absent=ab.get((m["id"], meal), 0),
                       expected=r - ab.get((m["id"], meal), 0),
                       entered=en.get((m["id"], meal), 0)) for meal in MEALS})
    return out


# ---------------------------------------------------------------- student side
def qr_svg(code):
    url = request.host_url + "v/" + code
    return Markup(segno.make(url, error="m").svg_inline(scale=5, border=1, dark="#111"))

def ensure_pass(u, mess, meal):
    """Pass for the running meal is created automatically when the student opens the page."""
    today = date.today().isoformat()
    if q("SELECT 1 FROM passes WHERE student_id=? AND date=? AND meal=?", (u["id"], today, meal), one=True):
        return
    for _ in range(20):
        code = str(secrets.randbelow(900000) + 100000)
        try:
            ex("INSERT INTO passes(student_id,mess_id,date,meal,code,created_at) VALUES(?,?,?,?,?,?)",
               (u["id"], mess["id"], today, meal, code, datetime.now().isoformat(timespec="seconds")))
            return
        except sqlite3.IntegrityError:
            db().rollback()

@app.route("/student")
@role_required("student")
def student():
    u = current_user()
    today = date.today()
    try:
        y, mo = map(int, request.args.get("m", "").split("-"))
        date(y, mo, 1)
    except ValueError:
        y, mo = today.year, today.month

    weeks = calendar.Calendar(0).monthdatescalendar(y, mo)
    first, last = weeks[0][0].isoformat(), weeks[-1][-1].isoformat()
    absent = {(r["date"], r["meal"]) for r in q(
        "SELECT date, meal FROM absences WHERE student_id=? AND date BETWEEN ? AND ?",
        (u["id"], first, last))}
    used = {(r["date"], r["meal"]) for r in q(
        "SELECT date, meal FROM passes WHERE student_id=? AND used_at IS NOT NULL "
        "AND date BETWEEN ? AND ?", (u["id"], first, last))}
    days = []
    for wk in weeks:
        for d in wk:
            iso = d.isoformat()
            days.append(dict(iso=iso, n=d.day, in_month=d.month == mo, today=d == today,
                             future=d > today,
                             absent={m: (iso, m) in absent for m in MEALS},
                             used={m: (iso, m) in used for m in MEALS}))
    prev = (date(y, mo, 1) - timedelta(days=1)).strftime("%Y-%m")
    nxt = (date(y, mo, 28) + timedelta(days=5)).strftime("%Y-%m")

    mess = q("SELECT * FROM messes WHERE id=?", (u["mess_id"],), one=True) if u["mess_id"] else None
    cur = current_meal()
    in_mess = bool(mess) and in_mess_network(mess)
    if cur and in_mess:
        ensure_pass(u, mess, cur)
    passes = {}
    for r in q("SELECT * FROM passes WHERE student_id=? AND date=?", (u["id"], today.isoformat())):
        passes[r["meal"]] = dict(code=r["code"], used_at=r["used_at"], svg=qr_svg(r["code"]))
    return render_template("student.html", days=days, month_name=date(y, mo, 1).strftime("%B %Y"),
                           prev=prev, nxt=nxt, cur=cur, passes=passes, mess=mess, in_mess=in_mess, my_photo=photo_version(u["id"]),
                           tomorrow=(today + timedelta(days=1)).isoformat())

@app.post("/api/absence")
@role_required("student")
def api_absence():
    u = current_user()
    data = request.get_json(silent=True) or {}
    try:
        d = date.fromisoformat(data.get("date", ""))
    except ValueError:
        return jsonify(ok=False, error="bad date"), 400
    meal = data.get("meal")
    if meal not in MEALS or d <= date.today():
        return jsonify(ok=False, error="Only future dates can be changed"), 400
    if data.get("absent"):
        ex("INSERT OR IGNORE INTO absences VALUES(?,?,?)", (u["id"], d.isoformat(), meal))
    else:
        ex("DELETE FROM absences WHERE student_id=? AND date=? AND meal=?", (u["id"], d.isoformat(), meal))
    return jsonify(ok=True)

@app.post("/absence/range")
@role_required("student")
def absence_range():
    u = current_user()
    try:
        start = date.fromisoformat(request.form["start"])
        end = date.fromisoformat(request.form["end"])
    except (KeyError, ValueError):
        flash("Choose valid dates.", "err")
        return redirect(url_for("student"))
    meals = [m for m in request.form.getlist("meals") if m in MEALS]
    start = max(start, date.today() + timedelta(days=1))
    if end < start or (end - start).days > 92 or not meals:
        flash("Pick meals and a valid range (from tomorrow, max 3 months).", "err")
        return redirect(url_for("student"))
    absent = request.form.get("action") == "absent"
    d = start
    while d <= end:
        for m in meals:
            if absent:
                db().execute("INSERT OR IGNORE INTO absences VALUES(?,?,?)", (u["id"], d.isoformat(), m))
            else:
                db().execute("DELETE FROM absences WHERE student_id=? AND date=? AND meal=?",
                             (u["id"], d.isoformat(), m))
        d += timedelta(days=1)
    db().commit()
    flash(("Marked NOT coming" if absent else "Marked as coming again") + f" for {start} to {end}.", "ok")
    return redirect(url_for("student"))


# ---------------------------------------------------------------- mess supervisor side
@app.route("/mess")
@role_required("mess")
def mess_home():
    u = current_user()
    mess = q("SELECT * FROM messes WHERE id=?", (u["mess_id"],), one=True)
    if not mess:
        flash("No mess is linked to this login. Contact the admin.", "err")
        return render_template("mess.html", mess=None, stats=None, tomorrow=None, recent=[], today="", cur=None)
    today = date.today()
    t, tm = today.isoformat(), (today + timedelta(days=1)).isoformat()
    stats = mess_stats(t)[mess["id"]]
    tomorrow = mess_stats(tm)[mess["id"]]
    recent = q("SELECT s.name, s.roll, p.meal, p.used_at FROM passes p JOIN users s ON s.id=p.student_id "
               "WHERE p.date=? AND p.mess_id=? AND p.used_at IS NOT NULL ORDER BY p.used_at DESC LIMIT 15",
               (t, mess["id"]))
    return render_template("mess.html", mess=mess, stats=stats, tomorrow=tomorrow, recent=recent,
                           cur=current_meal(), today=today.strftime("%A, %d %B %Y"),
                           total_entered=sum(x["entered"] for x in stats["meals"].values()))

def check_pass(u, code):
    """Validate a pass for today and mark it used if OK. Returns (status, info-dict-or-None)."""
    p = q("SELECT p.*, s.name, s.roll, s.active, m.name AS mess_name FROM passes p "
          "JOIN users s ON s.id=p.student_id LEFT JOIN messes m ON m.id=p.mess_id "
          "WHERE p.date=? AND p.code=?", (date.today().isoformat(), code), one=True)
    if not p:
        return "invalid", None
    info = dict(p)
    if not p["active"]:
        return "blocked", info
    if u["role"] == "mess" and p["mess_id"] != u["mess_id"]:
        return "wrongmess", info
    if p["used_at"]:
        return "used", info
    if p["meal"] != current_meal():
        return "wrongmeal", info
    now = datetime.now().isoformat(timespec="seconds")
    ex("UPDATE passes SET used_at=? WHERE id=?", (now, p["id"]))
    info["used_at"] = now
    return "ok", info

@app.route("/v/<code>")
@role_required("admin", "mess")
def verify(code):
    status, info = check_pass(current_user(), code)
    pv = photo_version(info["student_id"]) if info and status != "wrongmess" else None
    return render_template("verify.html", status=status, p=info, cur=current_meal(), pv=pv)

@app.route("/scan")
@role_required("admin", "mess")
def scan():
    return render_template("scan.html")

@app.post("/api/verify")
@role_required("admin", "mess")
def api_verify():
    """Used by the in-browser camera scanner: verify a code and return the result as JSON."""
    code = re.sub(r"\D", "", str((request.get_json(silent=True) or {}).get("code", "")))[:6]
    status, info = check_pass(current_user(), code) if len(code) == 6 else ("invalid", None)
    out = dict(status=status, name=None, roll=None, meal=None, mess_name=None, used_at=None, photo=None)
    if info:
        out.update(name=info["name"], roll=info["roll"], meal=MEAL_META[info["meal"]]["label"],
                   mess_name=info["mess_name"], used_at=(info["used_at"] or "")[11:16])
        if status != "wrongmess":
            v = photo_version(info["student_id"])
            if v:
                out["photo"] = f"/photo/{info['student_id']}?v={v}"
    cur = current_meal()
    out["current_meal"] = MEAL_META[cur]["label"] if cur else None
    # A ticket is created only after the pass has been successfully marked used.
    # It is short-lived, tied to this supervisor login, and can be consumed once.
    if status == "ok":
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        ex("INSERT INTO unlock_tickets(token,user_id,created_at,expires_at,used_at) VALUES(?,?,?,?,NULL)",
           (token, current_user()["id"], now.isoformat(timespec="seconds"),
            (now + timedelta(seconds=30)).isoformat(timespec="seconds")))
        out["unlock_ticket"] = token
    return jsonify(out)

@app.post("/api/consume-unlock")
@role_required("admin", "mess")
def api_consume_unlock():
    """Atomically consume a short-lived, session-bound ticket before a physical unlock."""
    token = str((request.get_json(silent=True) or {}).get("ticket", ""))[:128]
    if not token:
        return jsonify(authorized=False), 400
    now = datetime.now().isoformat(timespec="seconds")
    cur = db().execute(
        "UPDATE unlock_tickets SET used_at=? WHERE token=? AND user_id=? AND used_at IS NULL AND expires_at>=?",
        (now, token, current_user()["id"], now))
    db().commit()
    if cur.rowcount != 1:
        return jsonify(authorized=False), 403
    return jsonify(authorized=True)

@app.post("/lookup")
@role_required("admin", "mess")
def lookup():
    return redirect(url_for("verify", code=request.form.get("code", "").strip()))


# ---------------------------------------------------------------- admin: dashboard
@app.route("/admin")
@role_required("admin")
def admin():
    today = date.today()
    t, tm = today.isoformat(), (today + timedelta(days=1)).isoformat()
    st, st2 = mess_stats(t), mess_stats(tm)
    tot = {m: dict(entered=0, expected=0, tomorrow=0) for m in MEALS}
    for mid, s in st.items():
        for m in MEALS:
            tot[m]["entered"] += s["meals"][m]["entered"]
            tot[m]["expected"] += s["meals"][m]["expected"]
            tot[m]["tomorrow"] += st2[mid]["meals"][m]["expected"]
    unalloc = q("SELECT COUNT(*) c FROM users WHERE role='student' AND active=1 AND mess_id IS NULL", one=True)["c"]
    have = photo_ids()
    nophoto = sum(1 for r in q("SELECT id FROM users WHERE role='student' AND active=1") if r["id"] not in have)
    return render_template("admin.html", nophoto=nophoto, st=st, st2=st2, tot=tot, unalloc=unalloc,
                           registered=sum(s["registered"] for s in st.values()),
                           total_entered=sum(t_["entered"] for t_ in tot.values()),
                           today=today.strftime("%A, %d %B %Y"), cur=current_meal())


# ---------------------------------------------------------------- admin: messes & supervisors
@app.route("/admin/messes", methods=["GET", "POST"])
@role_required("admin")
def messes():
    if request.method == "POST":
        act = request.form.get("act")
        f = request.form
        try:
            if act == "add":
                name = f.get("name", "").strip()
                roll, sname = f.get("sup_roll", "").strip(), f.get("sup_name", "").strip() or "Supervisor"
                pw = f.get("sup_pw", "").strip() or roll
                if not (name and roll):
                    flash("Mess name and supervisor login ID are required.", "err")
                else:
                    cur = db().execute("INSERT INTO messes(name) VALUES(?)", (name,))
                    db().execute("INSERT INTO users(roll,name,pw_hash,role,mess_id) VALUES(?,?,?,'mess',?)",
                                 (roll, sname, hash_pw(pw), cur.lastrowid))
                    db().commit()
                    flash(f"Mess '{name}' created. Supervisor login: {roll} / {pw}", "ok")
            elif act == "update" and f.get("name", "").strip():
                ex("UPDATE messes SET name=?, allowed_ips=? WHERE id=?",
                   (f.get("name", "").strip(), f.get("allowed_ips", "").strip(), f.get("mess_id", type=int)))
                flash("Mess updated.", "ok")
            elif act == "delete":
                mid = f.get("mess_id", type=int)
                if q("SELECT 1 FROM users WHERE role='student' AND mess_id=?", (mid,), one=True):
                    flash("Move the students to another mess first.", "err")
                else:
                    db().execute("DELETE FROM users WHERE role='mess' AND mess_id=?", (mid,))
                    db().execute("DELETE FROM messes WHERE id=?", (mid,))
                    db().commit()
                    flash("Mess deleted.", "ok")
            elif act == "add_sup":
                roll, pw = f.get("roll", "").strip(), f.get("password", "").strip()
                ex("INSERT INTO users(roll,name,pw_hash,role,mess_id) VALUES(?,?,?,'mess',?)",
                   (roll, f.get("name", "").strip() or "Supervisor", hash_pw(pw or roll), f.get("mess_id", type=int)))
                flash(f"Supervisor {roll} added (password: {pw or roll}).", "ok")
            elif act in ("sup_reset", "sup_delete"):
                uid = f.get("uid", type=int)
                s = q("SELECT * FROM users WHERE id=? AND role='mess'", (uid,), one=True)
                if s and act == "sup_reset":
                    ex("UPDATE users SET pw_hash=?, must_change=1 WHERE id=?", (hash_pw(s["roll"]), uid))
                    flash(f"Password of {s['roll']} reset to the login ID.", "ok")
                elif s:
                    ex("DELETE FROM users WHERE id=?", (uid,))
        except sqlite3.IntegrityError:
            db().rollback()
            flash("That name or login ID already exists.", "err")
        return redirect(url_for("messes"))
    data = []
    counts = {r["mess_id"]: r["c"] for r in q(
        "SELECT mess_id, COUNT(*) c FROM users WHERE role='student' AND mess_id IS NOT NULL GROUP BY mess_id")}
    for m in q("SELECT * FROM messes ORDER BY name"):
        data.append(dict(m=m, students=counts.get(m["id"], 0),
                         sups=q("SELECT * FROM users WHERE role='mess' AND mess_id=? ORDER BY roll", (m["id"],))))
    return render_template("messes.html", data=data, my_ip=client_ip())


# ---------------------------------------------------------------- admin: students
def mess_lookup():
    return {m["name"].lower(): m["id"] for m in q("SELECT id, name FROM messes")}

@app.route("/admin/students", methods=["GET", "POST"])
@role_required("admin")
def students():
    if request.method == "POST":
        f = request.form
        act = f.get("act")
        rowact = f.get("rowact")
        if rowact:
            act, _, sid = rowact.partition(":")
            sid = int(sid)
            s = q("SELECT * FROM users WHERE id=? AND role='student'", (sid,), one=True)
            if s and act == "toggle":
                ex("UPDATE users SET active=1-active WHERE id=?", (sid,))
            elif s and act == "reset":
                ex("UPDATE users SET pw_hash=?, must_change=1 WHERE id=?", (hash_pw(s["roll"]), sid))
                flash(f"Password of {s['name']} reset to their ID.", "ok")
            elif s and act == "delete":
                for t in ("absences", "passes"):
                    db().execute(f"DELETE FROM {t} WHERE student_id=?", (sid,))
                db().execute("DELETE FROM users WHERE id=?", (sid,))
                db().commit()
                remove_photo(sid)
        elif act == "photos_zip":
            fl = request.files.get("zip")
            ok = bad = 0
            unknown = []
            if fl:
                rows_ = q("SELECT id, roll FROM users WHERE role='student'")
                rolls = {r["roll"]: r["id"] for r in rows_}
                rolls_l = {r["roll"].lower(): r["id"] for r in rows_}
                try:
                    z = zipfile.ZipFile(fl.stream)
                except zipfile.BadZipFile:
                    flash("That is not a valid ZIP file.", "err")
                    return redirect(request.url)
                for info in z.infolist():
                    name = os.path.basename(info.filename)
                    stem, ext = os.path.splitext(name)
                    if info.is_dir() or name.startswith(".") or ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                        continue
                    uid = rolls.get(stem) or rolls_l.get(stem.lower())
                    if uid is None:
                        unknown.append(stem)
                    elif info.file_size > 10 * 1024 * 1024 or not save_photo(uid, z.read(info)):
                        bad += 1
                    else:
                        ok += 1
            flash(f"Photos saved: {ok}. Unreadable: {bad}. No matching student ID: {len(unknown)}"
                  + (f" (e.g. {', '.join(unknown[:5])})" if unknown else ""), "ok")
        elif act == "add":
            roll, name = f.get("roll", "").strip(), f.get("name", "").strip()
            mid = f.get("mess_id", type=int)
            if roll and name:
                try:
                    ex("INSERT INTO users(roll,name,pw_hash,role,mess_id) VALUES(?,?,?,'student',?)",
                       (roll, name, hash_pw(f.get("password", "").strip() or roll), mid))
                    flash(f"Added {name}.", "ok")
                except sqlite3.IntegrityError:
                    flash("That ID already exists.", "err")
        elif act == "csv":
            fl = request.files.get("csv")
            added = skipped = nomess = 0
            if fl:
                lookup_ = mess_lookup()
                for r in csv.reader(io.StringIO(fl.read().decode("utf-8-sig"))):
                    if len(r) < 2 or r[0].strip().lower() in ("roll", "id", "roll no", "student id"):
                        continue
                    roll, name = r[0].strip(), r[1].strip()
                    mname = r[2].strip().lower() if len(r) > 2 else ""
                    mid = lookup_.get(mname) if mname else None
                    if mname and mid is None:
                        nomess += 1
                    pw = r[3].strip() if len(r) > 3 and r[3].strip() else roll
                    try:
                        db().execute("INSERT INTO users(roll,name,pw_hash,role,mess_id) VALUES(?,?,?,'student',?)",
                                     (roll, name, hash_pw(pw, fast=True), mid))
                        added += 1
                    except sqlite3.IntegrityError:
                        skipped += 1
                db().commit()
            flash(f"Imported {added} students; skipped {skipped} duplicates; {nomess} had an unknown mess name (left unallocated).", "ok")
        elif act == "allocate":
            ids = f.getlist("ids", type=int)
            target = f.get("mess_id", "")
            mid = None if target == "none" else int(target)
            if ids:
                db().execute("UPDATE users SET mess_id=? WHERE role='student' AND id IN (%s)" % ",".join("?" * len(ids)),
                             [mid] + ids)
                db().commit()
                flash(f"{len(ids)} students updated.", "ok")
            else:
                flash("Tick at least one student.", "err")
        elif act == "prefix":
            pre, mid = f.get("prefix", "").strip(), f.get("mess_id", type=int)
            if pre and mid:
                n = db().execute("UPDATE users SET mess_id=? WHERE role='student' AND substr(roll,1,?)=?",
                                 (mid, len(pre), pre)).rowcount
                db().commit()
                flash(f"{n} students with ID starting '{pre}' allotted.", "ok")
        elif act == "autosplit":
            ids = [r["id"] for r in q("SELECT id FROM users WHERE role='student' AND mess_id IS NULL ORDER BY roll")]
            ms = [m["id"] for m in q("SELECT id FROM messes ORDER BY name")]
            if ids and ms:
                size = math.ceil(len(ids) / len(ms))
                for i, mid in enumerate(ms):
                    chunk = ids[i * size:(i + 1) * size]
                    if chunk:
                        db().execute("UPDATE users SET mess_id=? WHERE id IN (%s)" % ",".join("?" * len(chunk)),
                                     [mid] + chunk)
                db().commit()
                flash(f"{len(ids)} unallocated students split equally across {len(ms)} messes.", "ok")
            else:
                flash("Nothing to split (no unallocated students, or no mess yet).", "err")
        return redirect(request.url)

    term = request.args.get("s", "").strip()
    mf = request.args.get("mess", "all")
    page = max(1, request.args.get("page", 1, type=int))
    where, args = ["u.role='student'"], []
    if term:
        where.append("(u.roll LIKE ? OR u.name LIKE ?)")
        args += [f"%{term}%", f"%{term}%"]
    if mf == "none":
        where.append("u.mess_id IS NULL")
    elif mf.isdigit():
        where.append("u.mess_id=?")
        args.append(int(mf))
    W = " AND ".join(where)
    total = q(f"SELECT COUNT(*) c FROM users u WHERE {W}", args, one=True)["c"]
    rows = q(f"SELECT u.*, m.name AS mess_name FROM users u LEFT JOIN messes m ON m.id=u.mess_id "
             f"WHERE {W} ORDER BY u.roll LIMIT ? OFFSET ?", args + [PER_PAGE, (page - 1) * PER_PAGE])
    return render_template("students.html", rows=rows, term=term, mf=mf, page=page, total=total,
                           pages=max(1, math.ceil(total / PER_PAGE)), messes=q("SELECT * FROM messes ORDER BY name"))


# ---------------------------------------------------------------- reports (admin: any mess, supervisor: own)
@app.route("/report")
@role_required("admin", "mess")
def report():
    u = current_user()
    try:
        d = date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        d = date.today()
    iso = d.isoformat()
    mess_id = u["mess_id"] if u["role"] == "mess" else request.args.get("mess", type=int)
    st = mess_stats(iso)
    if mess_id:
        st = {k: v for k, v in st.items() if k == mess_id}
    registered = sum(s["registered"] for s in st.values())
    summary = {m: dict(absent=sum(s["meals"][m]["absent"] for s in st.values()),
                       expected=sum(s["meals"][m]["expected"] for s in st.values()),
                       entered=sum(s["meals"][m]["entered"] for s in st.values())) for m in MEALS}
    sql = ("SELECT s.roll, s.name, m.name AS mess_name, p.meal, p.used_at, "
           "(SELECT 1 FROM absences a WHERE a.student_id=p.student_id AND a.date=p.date AND a.meal=p.meal) AS absent "
           "FROM passes p JOIN users s ON s.id=p.student_id LEFT JOIN messes m ON m.id=p.mess_id "
           "WHERE p.date=? AND p.used_at IS NOT NULL")
    args = [iso]
    if mess_id:
        sql += " AND p.mess_id=?"
        args.append(mess_id)
    sql += " ORDER BY p.meal, p.used_at"
    if request.args.get("csv"):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["date", "mess", "meal", "roll", "name", "entry_time", "had_marked_absent"])
        for r in q(sql, args):
            w.writerow([iso, r["mess_name"], r["meal"], r["roll"], r["name"], r["used_at"], "yes" if r["absent"] else "no"])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=mess_{iso}.csv"})
    rows = q(sql + " LIMIT 500", args)
    return render_template("report.html", d=iso, rows=rows, summary=summary, registered=registered,
                           mess_id=mess_id, all_messes=q("SELECT * FROM messes ORDER BY name"))

@app.route("/admin/settings", methods=["GET", "POST"])
@role_required("admin")
def settings():
    if request.method == "POST":
        for k in DEFAULTS:
            v = request.form.get(k, "").strip()
            if v:
                ex("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
        msg, cat = "Settings saved.", "ok"
        if request.form.get("remove_logo"):
            try:
                os.remove(LOGO_PATH)
            except OSError:
                pass
        f = request.files.get("logo")
        if f and f.filename:
            data = f.read()
            if len(data) > 5 * 1024 * 1024 or not save_logo(data):
                msg, cat = "Settings saved, but the logo could not be read. Use a PNG or JPG under 5 MB.", "err"
        flash(msg, cat)
        return redirect(url_for("settings"))
    return render_template("settings.html", s={k: setting(k) for k in DEFAULTS})


init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
