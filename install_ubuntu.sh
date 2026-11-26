#!/usr/bin/env bash
# Mess Management System - one-step installer for Ubuntu Server (20.04 / 22.04 / 24.04)
#   unzip mess-system.zip, cd mess-system, then:   sudo bash install_ubuntu.sh
# Safe to run again later (upgrade): your database, photos and settings are kept.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mess-system}"
APP_USER="${APP_USER:-mess}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
CRON_DIR="${CRON_DIR:-/etc/cron.d}"
TZ_NAME="${TZ_NAME:-Asia/Kolkata}"          # meal timings follow the server clock
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ] && [ "${ALLOW_NONROOT:-0}" != "1" ]; then
  echo "Please run:  sudo bash install_ubuntu.sh"; exit 1
fi

echo "== 1/8  Time zone"
if command -v timedatectl >/dev/null 2>&1; then
  cur="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  if [ "$cur" != "$TZ_NAME" ]; then
    timedatectl set-timezone "$TZ_NAME" && echo "   time zone set to $TZ_NAME (was: ${cur:-unknown})"
  else
    echo "   already $TZ_NAME"
  fi
fi

echo "== 2/8  Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

echo "== 3/8  Service user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "== 4/8  Copying the project to $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC" != "$APP_DIR" ]; then
  if [ -f "$APP_DIR/app.py" ]; then
    echo "   existing install found -> upgrading code, keeping data and settings"
    EXCL=(--exclude=venv --exclude='mess.db*' --exclude=photos --exclude=branding --exclude=backups --exclude=.secret_key
          --exclude=backup_config.json --exclude=cert.pem --exclude=key.pem --exclude=cert_ips.txt --exclude=__pycache__)
  else
    EXCL=(--exclude=venv --exclude=__pycache__)
  fi
  tar -C "$SRC" "${EXCL[@]}" -cf - . | tar -C "$APP_DIR" -xf -
fi
mkdir -p "$APP_DIR/photos" "$APP_DIR/backups"

echo "== 5/8  Python environment"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" gunicorn
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

run_as() { sudo -u "$APP_USER" -H "$@"; }
echo "== 6/8  Database and HTTPS certificate"
cd "$APP_DIR"
run_as "$APP_DIR/venv/bin/python" -c "import app"          # creates mess.db and the default admin on first run
run_as "$APP_DIR/venv/bin/python" "$APP_DIR/make_cert.py"

echo "== 7/8  Services (start automatically at every boot)"
cat > "$SYSTEMD_DIR/mess-http.service" <<EOF
[Unit]
Description=Mess app - HTTP (students)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 --threads 8 --timeout 180 -b 0.0.0.0:5000 app:app
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
cat > "$SYSTEMD_DIR/mess-https.service" <<EOF
[Unit]
Description=Mess app - HTTPS (supervisor camera scanner)
After=network-online.target mess-http.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStartPre=$APP_DIR/venv/bin/python $APP_DIR/make_cert.py
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 --threads 8 --timeout 180 -b 0.0.0.0:5443 --certfile $APP_DIR/cert.pem --keyfile $APP_DIR/key.pem app:app
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable mess-http mess-https
systemctl restart mess-http mess-https

echo "== 8/8  Firewall and backups"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 5000/tcp; ufw allow 5443/tcp
fi
if grep -qE "APP-PASSWORD|MAILBOX-PASSWORD" "$APP_DIR/backup_config.json" 2>/dev/null; then
  echo "   e-mail backup NOT scheduled yet: edit $APP_DIR/backup_config.json, then run this installer again."
else
  cat > "$CRON_DIR/mess-backup" <<EOF
# 23:05 every night: one e-mail per mess (its Excel) + one with all messes and the database backup
5 23 * * * $APP_USER cd $APP_DIR && $APP_DIR/venv/bin/python backup_mail.py eod >> $APP_DIR/backups/cron.log 2>&1
# Monday 06:30: weekly report for all messes (+ local zip of the photos folder)
30 6 * * 1 $APP_USER cd $APP_DIR && $APP_DIR/venv/bin/python backup_mail.py weekly >> $APP_DIR/backups/cron.log 2>&1
EOF
  chmod 644 "$CRON_DIR/mess-backup"
  echo "   e-mail reports scheduled: every night 23:05 (per mess + database backup) and weekly Mon 06:30"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "=============================================================="
echo " Installed.  Data folder: $APP_DIR  (mess.db, photos/)"
echo "   Students    : http://${IP:-<server-ip>}:5000"
echo "   Supervisors : https://${IP:-<server-ip>}:5443   (camera scanner)"
echo " First login   : admin / admin123  (you must change it)"
echo " Status        : sudo systemctl status mess-http mess-https"
echo " Logs          : sudo journalctl -u mess-http -f"
echo "=============================================================="
