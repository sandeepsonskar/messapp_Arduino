# Installing on Ubuntu Server

## 1. Copy the project to the server
From your PC (replace the user and IP):   scp mess-system.zip user@<server-ip>:~
(or use a USB drive / WinSCP)

## 2. Install
    sudo apt install -y unzip
    unzip mess-system.zip
    cd mess-system
    sudo bash install_ubuntu.sh

The script sets the time zone to Asia/Kolkata (meal timings follow the server clock), installs everything into
/opt/mess-system, creates the database, makes the HTTPS certificate, starts two services that also start at every boot,
and opens ports 5000 and 5443 if the ufw firewall is on.

  Students    : http://<server-ip>:5000
  Supervisors : https://<server-ip>:5443   (first time: Advanced -> Proceed, then allow the camera)

## 3. Bring your old data (optional)
Before running the installer, copy `mess.db` and the `photos` folder from your old copy into the unzipped `mess-system` folder.
Already installed? Stop the services, copy them into /opt/mess-system, fix the owner, start again:
    sudo systemctl stop mess-http mess-https
    sudo cp mess.db /opt/mess-system/ && sudo cp -r photos /opt/mess-system/
    sudo rm -f /opt/mess-system/mess.db-wal /opt/mess-system/mess.db-shm
    sudo chown -R mess:mess /opt/mess-system
    sudo systemctl start mess-http mess-https

## 4. Give the server a fixed IP (important)
Edit /etc/netplan/*.yaml (example), then `sudo netplan apply`:
    network:
      version: 2
      ethernets:
        enp3s0:                     # your interface name: ip a
          dhcp4: false
          addresses: [192.168.1.50/24]
          routes: [{to: default, via: 192.168.1.1}]
          nameservers: {addresses: [8.8.8.8, 1.1.1.1]}
(or reserve the IP for this machine in your router). If the IP ever changes, restart the services - the certificate is renewed automatically.

## 5. E-mail reports (every night 23:05)
Edit /opt/mess-system/backup_config.json: put the mailbox password in "smtp_password" (and check smtp_host / smtp_port from your
mail hosting: "Connect devices / SMTP settings"). Then run `sudo bash install_ubuntu.sh` again - it schedules:
  * every night 23:05  -> one e-mail PER MESS with that mess's Excel, plus one e-mail with all messes and a zipped copy of the database
  * every Monday 06:30 -> weekly report for all messes
Test now (sends today's reports immediately):
    sudo -u mess /opt/mess-system/venv/bin/python /opt/mess-system/backup_mail.py eod
If an e-mail fails, the reason is in /opt/mess-system/backup_error.log and backups/cron.log.

## Everyday commands
    sudo systemctl status mess-http mess-https
    sudo systemctl restart mess-http mess-https
    sudo journalctl -u mess-http -f            # live log
Upgrade to a new zip: unzip it and run `sudo bash install_ubuntu.sh` again - data and settings are kept.
Backups: copy /opt/mess-system/mess.db, /opt/mess-system/photos and /opt/mess-system/branding (the logo) somewhere safe every week.
