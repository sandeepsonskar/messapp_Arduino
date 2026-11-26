# Multi-Mess Management System (Flask + SQLite)

Three logins: **Admin** (adds messes, supervisors, students, allocates students to messes),
**Mess supervisor** (one or more logins per mess, scans / verifies QR), **Student**.

## Android app for mess supervisors: see the separate mess-supervisor-app project (the server already supports it: the app can only be used with mess-supervisor logins).

## Ubuntu server: see UBUNTU.md  (unzip, then `sudo bash install_ubuntu.sh`)

## Run on the local network (recommended: camera scanner works)
    pip install -r requirements.txt
    python run_local.py          (or double-click start_mess.bat)
It prints two addresses:
    Students    : http://<server-ip>:5000
    Supervisors : https://<server-ip>:5443      <- use this one for the camera scanner
The https address uses an automatic self-signed certificate. On the supervisor's phone, the FIRST time:
"Your connection is not private" -> Advanced -> Proceed to <ip> (unsafe) -> log in -> Scan -> Allow camera. This is normal for a local server.
Students must use http:// (not https://) on port 5000. Windows firewall: allow TCP ports 5000 and 5443.
(`python app.py` still works for a plain http-only test.)  First start creates  admin / admin123  (you must change it at first login).

## Set-up order (admin)
1. **Messes** -> add each mess (4) with its supervisor login ID + password. Add more supervisor logins if a mess has several entrances.
2. **Students** -> import the CSV (`roll,name,mess,password`, ~5000 rows ~15-30 s) or add one by one.
   Allocate to messes: put the mess name in the CSV, or tick students and move them, or "Allot by ID prefix",
   or "Auto-split unallocated equally".
3. **Photos**: Students page -> "Upload ZIP" (images named by student ID, e.g. `R0001.jpg`, ~1000 per ZIP) or click 📷 on a student row.
   Students can also upload their own photo once from their page (admin can replace it). "Students without photo (CSV)" lists who is missing.
   Photos are resized to small JPEGs and stored in the `photos` folder (back this folder up too).
4. Every student / supervisor must set a new password at first login (default password = their ID).
5. **Settings** -> meal timings (Breakfast 6-11, Lunch 12-16, Dinner 19-23 by default).
6. Optional: **Messes** -> "Allowed public IPs" per mess so passes can only be created from that mess's WiFi.

## Daily flow
- Every student is counted as coming to all three meals. A student who will NOT come marks those meals on future dates (calendar or date range).
- During a meal window the student opens the page: QR + 6-digit code for that meal appears automatically (for his allotted mess only).
- Mess supervisor logs in on his phone and taps **📷 Scan student QR**. The camera opens, reads the QR by itself and shows the result
  full-screen with the student's PHOTO, name, ID and meal: green ALLOW / red reject (already used, wrong meal time, WRONG MESS - allotted to <mess>, blocked, invalid).
  ALLOW closes by itself after 4 s (or tap) and scanning continues - no button presses between students. Manual 6-digit code entry is on the same page.
  **The phone camera works only on a secure address: use the https:// link (Cloudflare Tunnel, see DEPLOY.md) or localhost.**
  On plain http://192.168.x.x the page offers "Take photo of QR" (opens the camera app) and manual code instead.
  (One-phone workaround for LAN-only use: Chrome -> chrome://flags -> "Insecure origins treated as secure" -> add http://<server-ip>:5000 -> Enabled -> relaunch.)
- Mess dashboard: entered / expected per meal today, how many to cook tomorrow. Admin dashboard: all messes in one table + totals.
- Report: per day, per mess (admin) or own mess (supervisor), with CSV download.

See DEPLOY.md for internet access with a Cloudflare Tunnel.
