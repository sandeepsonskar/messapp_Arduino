# Arduino Uno USB-OTG integration (Mess System)

This integration preserves the existing Flask/SQLite student pass and QR workflow.
The scanner continues to call `/api/verify`. A physical unlock is possible only for
`status == "ok"`; Flask then creates a random 30-second ticket tied to the logged-in
supervisor. The scanner consumes that ticket through `/api/consume-unlock` before
calling the Android USB bridge. Tickets are one-use and cannot be replayed.

## What changed

- `app.py`: adds the `unlock_tickets` table, returns a one-time ticket only for an
  accepted pass, and adds the authenticated one-time consume endpoint.
- `templates/scan.html`: after a successful verification, consumes the ticket and
  asks `AndroidBridge.sendUnlock()` to send the serial command.
- `run_local.py`: generates a local self-signed certificate with CA constraints so
  it can be installed as a user CA on the dedicated Android supervisor device.
- `android-usb-bridge/`: Android Studio project. WebView opens the existing scanner;
  USB serial sends `ACCESS_GRANTED` at 9600 baud.

## Server setup

1. Back up your current `app.py`, `templates/scan.html`, database and photos.
2. Replace the project files with this updated project (or copy the changed files).
3. Stop the server. Remove the old generated `cert.pem`, `key.pem`, and `cert_ips.txt`
   once, so `run_local.py` creates the new local CA-capable certificate. Do not delete
   your database, `.secret_key`, photos, or branding.
4. Start with `python run_local.py`. Note the HTTPS supervisor URL printed, e.g.
   `https://10.2.4.22:5443/scan`.
5. On the Android phone, transfer `cert.pem` from the server to the phone. Install it
   through Android Settings -> Security (or Security & privacy) -> Encryption &
   credentials -> Install a certificate -> CA certificate. Menu names vary by Android
   version. Install only on the trusted, dedicated supervisor device. Then remove the
   transferred copy if desired.

The app intentionally does not bypass HTTPS certificate errors. The installed CA is
what lets Android WebView trust the local HTTPS server. Do not expose this development
certificate or server to untrusted networks.

## Android Studio setup

1. Open the `android-usb-bridge` folder in Android Studio.
2. In `MainActivity.kt`, change `START_URL` to the exact HTTPS supervisor URL printed
   by `run_local.py`, ending in `/scan`.
3. Sync Gradle and build the debug APK: Build -> Build Bundle(s) / APK(s) -> Build APK(s).
4. Install the APK on the Android phone.
5. Connect Arduino Uno to the phone using a data-capable USB OTG adapter/cable.
6. Allow Android USB access and camera permission. Log in using a mess-supervisor
   account; the app loads the existing `/scan` page.

The phone and server must be on the same LAN/Wi-Fi. The Android app does not require
internet access to reach the local server, but Gradle needs internet during the first
build to download dependencies.

## Arduino wiring and sketch

- Relay module IN -> Arduino D7
- Relay module VCC/GND -> its rated supply and Arduino GND (only if the module requires
  a shared logic ground)
- Use a properly rated, isolated relay module and separate supply for the lock. Never
  power an electric lock from the Arduino 5V pin.

Upload `arduino/relay_unlock.ino`. The sketch assumes an active-LOW relay module and
energizes it for 2 seconds. If your relay is active-HIGH, change `RELAY_ON` to `HIGH`
 and `RELAY_OFF` to `LOW`.

## Test sequence

1. Test the relay separately with a safe low-voltage test load before connecting a lock.
2. Start the server, open the Android app, connect USB and verify the status says Arduino
   connected at 9600 baud.
3. Scan a valid student QR during the correct meal window and for the correct mess.
   Only the green ALLOW result should send `ACCESS_GRANTED`.
4. Test invalid, used, wrong-meal, wrong-mess and blocked passes. None should activate
   the relay.
5. Try replaying an old unlock ticket: `/api/consume-unlock` must reject it.

## Important operational notes

- `/api/verify` marks a valid pass as used before the door command is sent, matching the
  existing system's attendance semantics. If the phone/USB/relay fails after that point,
  the pass remains consumed; a supervisor will need an administrative correction path.
- The included Arduino sketch uses a blocking 2-second delay. Do not use it if other
  time-critical Arduino tasks must continue during unlocking; replace it with a
  `millis()` state machine in that case.
- A relay is not a complete life-safety access-control system. Ensure the lock type,
  fire egress, emergency release, power-failure behavior and local electrical rules are
  handled by a qualified installer. Do not connect mains voltage on a breadboard.
- Keep the supervisor login private. The one-time ticket prevents replay, but it does
  not replace normal account security, server hardening, HTTPS trust and physical
  protection of the phone/Arduino.
