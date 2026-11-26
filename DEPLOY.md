# Put the mess server on the internet (Cloudflare Tunnel)

The server stays on the mess Raspberry Pi / PC. Cloudflare gives it a public HTTPS address.
No router port-forwarding is needed. Requires a domain whose DNS is on Cloudflare (free plan is fine).

## 1. Run the app as a service
    sudo apt install python3-pip
    cd /home/pi/mess-system && pip install -r requirements.txt gunicorn --break-system-packages

/etc/systemd/system/mess.service
    [Unit]
    Description=Mess app
    After=network.target
    [Service]
    User=pi
    WorkingDirectory=/home/pi/mess-system
    ExecStart=/usr/local/bin/gunicorn -w 1 --threads 4 --timeout 180 -b 127.0.0.1:5000 app:app
    Restart=always
    [Install]
    WantedBy=multi-user.target

    sudo systemctl enable --now mess

## 2. Install cloudflared
Raspberry Pi (64-bit):
    curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
PC / VM (Ubuntu x86-64): same URL with `cloudflared-linux-amd64.deb`
    sudo dpkg -i cloudflared.deb

## 3. Create the tunnel
    cloudflared tunnel login                       # opens a browser link, pick your domain
    cloudflared tunnel create mess                 # prints a tunnel UUID
    cloudflared tunnel route dns mess mess.yourdomain.in

/etc/cloudflared/config.yml   (copy ~/.cloudflared/<UUID>.json to /etc/cloudflared/ too)
    tunnel: mess
    credentials-file: /etc/cloudflared/<UUID>.json
    ingress:
      - hostname: mess.yourdomain.in
        service: http://127.0.0.1:5000
      - service: http_status:404

    sudo cloudflared service install
    sudo systemctl enable --now cloudflared

Open https://mess.yourdomain.in  - done.

## 4. Lock passes to each mess's WiFi
Log in as admin from inside a mess's WiFi -> Messes -> copy "Your IP as seen by the server" into that mess's
"Allowed public IPs" and save. Students of that mess can still log in and mark absences from anywhere,
but get a QR pass only from that network. (If all messes share one university IP, use the same IP for all.)
If a public IP changes, update it here.

## 5. Before going live
- Change the admin password (forced at first login).
- Back up mess.db daily:  `0 2 * * * cp /home/pi/mess-system/mess.db /home/pi/backup/mess-$(date +\%F).db`
- Login is rate-limited (8 wrong tries / 10 min per IP).
