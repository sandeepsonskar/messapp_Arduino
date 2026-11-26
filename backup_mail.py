"""
Excel reports + e-mail for the Mess Management System.

    python backup_mail.py eod        # TODAY: one e-mail per mess (its own Excel) + one e-mail with all messes
                                     # and a zipped copy of the database.  Scheduled at 23:05 every night.
    python backup_mail.py daily      # yesterday, all messes, one e-mail
    python backup_mail.py weekly     # last 7 days, all messes, one e-mail (+ local zip of the photos folder)

    options:  --date YYYY-MM-DD   (end date; default today for eod, yesterday for daily/weekly)
              --no-email          (only create the Excel files in the backups folder)

Settings are read from backup_config.json (same folder).
"""
import os, re, sys, json, sqlite3, zipfile, smtplib, shutil, tempfile, argparse, time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MESS_DB", os.path.join(BASE, "mess.db"))
CONFIG_PATH = os.environ.get("MESS_BACKUP_CONFIG", os.path.join(BASE, "backup_config.json"))
OUT_DIR = os.environ.get("MESS_BACKUP_DIR", os.path.join(BASE, "backups"))
MEALS = ["breakfast", "lunch", "dinner"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def snapshot_db(dst):
    """Consistent copy of the live database (safe while the web app is running)."""
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def style_sheet(ws):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
        c.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    for i, col in enumerate(ws.columns, 1):
        w = max((len(str(c.value)) for c in list(col)[:300] if c.value is not None), default=8)
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(w + 2, 10), 40)


def build_workbook(con, start, end, path, mess_id=None):
    """Excel report for [start, end]. mess_id=None -> all messes. Returns {meal: [expected, attended]}."""
    con.row_factory = sqlite3.Row
    if mess_id:
        messes = con.execute("SELECT id, name FROM messes WHERE id=?", (mess_id,)).fetchall()
    else:
        messes = con.execute("SELECT id, name FROM messes ORDER BY name").fetchall()
    registered = {r["mess_id"]: r["c"] for r in con.execute(
        "SELECT mess_id, COUNT(*) c FROM users WHERE role='student' AND active=1 AND mess_id IS NOT NULL GROUP BY mess_id")}

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Date", "Mess", "Meal", "Registered (now)", "Marked not coming", "Expected", "Attended"])
    totals = {m: [0, 0] for m in MEALS}
    for d in daterange(start, end):
        iso = d.isoformat()
        ab = {(r["mess_id"], r["meal"]): r["c"] for r in con.execute(
            "SELECT u.mess_id, a.meal, COUNT(*) c FROM absences a JOIN users u ON u.id=a.student_id "
            "WHERE a.date=? AND u.active=1 AND u.mess_id IS NOT NULL GROUP BY u.mess_id, a.meal", (iso,))}
        en = {(r["mess_id"], r["meal"]): r["c"] for r in con.execute(
            "SELECT mess_id, meal, COUNT(*) c FROM passes WHERE date=? AND used_at IS NOT NULL "
            "GROUP BY mess_id, meal", (iso,))}
        for m in messes:
            reg = registered.get(m["id"], 0)
            for meal in MEALS:
                a, e = ab.get((m["id"], meal), 0), en.get((m["id"], meal), 0)
                ws.append([iso, m["name"], meal.capitalize(), reg, a, reg - a, e])
                totals[meal][0] += reg - a
                totals[meal][1] += e
    style_sheet(ws)

    ws = wb.create_sheet("Entries")
    ws.append(["Date", "Mess", "Meal", "Student ID", "Name", "Entry time", "Had marked absent"])
    sql = ("SELECT p.date, m.name mess, p.meal, s.roll, s.name, p.used_at, "
           "(SELECT 1 FROM absences a WHERE a.student_id=p.student_id AND a.date=p.date AND a.meal=p.meal) AS ab "
           "FROM passes p JOIN users s ON s.id=p.student_id LEFT JOIN messes m ON m.id=p.mess_id "
           "WHERE p.date BETWEEN ? AND ? AND p.used_at IS NOT NULL")
    args = [start.isoformat(), end.isoformat()]
    if mess_id:
        sql += " AND p.mess_id=?"
        args.append(mess_id)
    for r in con.execute(sql + " ORDER BY p.date, m.name, p.meal, p.used_at", args):
        ws.append([r["date"], r["mess"], r["meal"].capitalize(), r["roll"], r["name"],
                   (r["used_at"] or "")[11:19], "Yes" if r["ab"] else "No"])
    style_sheet(ws)

    ws = wb.create_sheet("Students")
    ws.append(["Student ID", "Name", "Mess", "Status"])
    sql = ("SELECT u.roll, u.name, m.name mess, u.active FROM users u LEFT JOIN messes m ON m.id=u.mess_id "
           "WHERE u.role='student'")
    args = []
    if mess_id:
        sql += " AND u.mess_id=?"
        args.append(mess_id)
    for r in con.execute(sql + " ORDER BY u.roll", args):
        ws.append([r["roll"], r["name"], r["mess"] or "— not allotted —", "Active" if r["active"] else "Blocked"])
    style_sheet(ws)

    ws = wb.create_sheet("Upcoming absences")
    ws.append(["Date", "Meal", "Student ID", "Name", "Mess"])
    sql = ("SELECT a.date, a.meal, s.roll, s.name, m.name mess FROM absences a JOIN users s ON s.id=a.student_id "
           "LEFT JOIN messes m ON m.id=s.mess_id WHERE a.date > ?")
    args = [end.isoformat()]
    if mess_id:
        sql += " AND s.mess_id=?"
        args.append(mess_id)
    for r in con.execute(sql + " ORDER BY a.date, a.meal, m.name, s.roll", args):
        ws.append([r["date"], r["meal"].capitalize(), r["roll"], r["name"], r["mess"]])
    style_sheet(ws)

    wb.save(path)
    return totals


def tomorrow_prep(con, mess_id, day):
    """Meals to cook for `day` in one mess: registered minus those marked not coming."""
    reg = con.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1 AND mess_id=?",
                      (mess_id,)).fetchone()[0]
    out = {}
    for meal in MEALS:
        ab = con.execute("SELECT COUNT(*) FROM absences a JOIN users u ON u.id=a.student_id "
                         "WHERE a.date=? AND a.meal=? AND u.active=1 AND u.mess_id=?",
                         (day.isoformat(), meal, mess_id)).fetchone()[0]
        out[meal] = reg - ab
    return out


def build_message(cfg, subject, body, attachments):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg.set_content(body)
    for path, mime in attachments:
        maintype, subtype = mime.split("/")
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))
    return msg


def smtp_connect(cfg):
    host, port = cfg["smtp_host"], int(cfg.get("smtp_port", 587))
    sec = cfg.get("smtp_security") or ("ssl" if port == 465 else "starttls")
    if sec == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        s = smtplib.SMTP(host, port, timeout=60)
        if sec == "starttls":
            s.starttls()
    if cfg.get("smtp_user"):
        s.login(cfg["smtp_user"], cfg["smtp_password"])
    return s


def send_all(cfg, msgs):
    """Send every message over one SMTP connection; returns the list of failures."""
    failed = []
    s = smtp_connect(cfg)
    try:
        for m in msgs:
            try:
                s.send_message(m)
                print("  sent:", m["Subject"])
            except Exception as e:                       # keep going with the other messes
                failed.append((m["Subject"], str(e)))
                print("  FAILED:", m["Subject"], e)
            time.sleep(1)
    finally:
        try:
            s.quit()
        except Exception:
            pass
    return failed


def prune(keep_days):
    cutoff = datetime.now() - timedelta(days=keep_days)
    for f in os.listdir(OUT_DIR):
        p = os.path.join(OUT_DIR, f)
        if os.path.isfile(p) and datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
            os.remove(p)


def meal_lines(totals):
    return [f"  {m.capitalize():10s} attended {totals[m][1]} of {totals[m][0]} expected" for m in MEALS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["eod", "daily", "weekly"])
    ap.add_argument("--date", help="end date YYYY-MM-DD")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    today = date.today()
    if a.date:
        end = date.fromisoformat(a.date)
    else:
        end = today if a.mode == "eod" else today - timedelta(days=1)
    start = end - timedelta(days=6) if a.mode == "weekly" else end
    os.makedirs(OUT_DIR, exist_ok=True)
    period = end.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
    stamp = end.isoformat() if start == end else f"{start.isoformat()}_to_{end.isoformat()}"

    tmp = tempfile.mkdtemp()
    msgs = []
    try:
        snap = os.path.join(tmp, "mess_snapshot.db")
        snapshot_db(snap)
        con = sqlite3.connect(snap)
        con.row_factory = sqlite3.Row

        if a.mode == "eod":                              # one e-mail per mess
            for m in con.execute("SELECT id, name FROM messes ORDER BY name").fetchall():
                safe = re.sub(r"[^A-Za-z0-9_-]+", "_", m["name"]).strip("_") or f"mess{m['id']}"
                xlsx = os.path.join(OUT_DIR, f"{safe}_{stamp}.xlsx")
                totals = build_workbook(con, start, end, xlsx, mess_id=m["id"])
                prep = tomorrow_prep(con, m["id"], end + timedelta(days=1))
                body = "\n".join([f"{m['name']} - daily report for {period}", ""] + meal_lines(totals) + [
                    "", f"To cook tomorrow ({(end + timedelta(days=1)).isoformat()}):",
                    "  " + ", ".join(f"{k.capitalize()} {v}" for k, v in prep.items()), "",
                    "Attached: Excel report (Summary, Entries, Students, Upcoming absences)."])
                print(body.split("\n")[0])
                msgs.append(build_message(cfg, f"[{m['name']}] Daily report {period}", body, [(xlsx, XLSX)]))

        # combined report (+ database copy)
        full = os.path.join(OUT_DIR, f"mess_all_{a.mode}_{stamp}.xlsx")
        totals = build_workbook(con, start, end, full)
        attachments = [(full, XLSX)]
        if cfg.get("attach_db", True):
            zpath = os.path.join(OUT_DIR, f"mess_db_{end.isoformat()}.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "mess.db")
            attachments.append((zpath, "application/zip"))
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    photos = os.environ.get("MESS_PHOTOS", os.path.join(BASE, "photos"))
    if a.mode == "weekly" and os.path.isdir(photos):      # kept on the server only (too large for e-mail)
        shutil.make_archive(os.path.join(OUT_DIR, f"photos_{end.isoformat()}"), "zip", photos)

    body = "\n".join([f"All messes - {a.mode} report for {period}", ""] + meal_lines(totals) + [
        "", "Attached: Excel report (all messes)" + (" and a zipped copy of the database." if len(attachments) > 1 else "."),
        "To restore, unzip mess.db into the application folder."])
    label = "Daily report + database backup" if a.mode == "eod" else f"{a.mode.capitalize()} report"
    msgs.append(build_message(cfg, f"[All messes] {label} {period}", body, attachments))
    print(body)

    if a.no_email:
        print("Excel files saved in", OUT_DIR)
        return
    failed = send_all(cfg, msgs)
    prune(int(cfg.get("keep_days", 60)))
    if failed:
        raise RuntimeError("%d e-mail(s) failed: %s" % (len(failed), "; ".join(f"{s}: {e}" for s, e in failed)))
    print("All e-mails sent to", ", ".join(cfg["to"]))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                               # visible in backup_error.log
        with open(os.path.join(BASE, "backup_error.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}  {type(e).__name__}: {e}\n")
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
