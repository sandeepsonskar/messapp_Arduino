# Mess Management + Arduino USB OTG Integration

This repository packages the supplied Flask/SQLite mess-management application with an Android WebView/USB-serial bridge and an Arduino Uno relay sketch.

## Components

- `app.py`, `templates/`, `static/`: Flask mess-management application and QR scanner.
- `android-usb-bridge/`: Android application source (Kotlin, WebView, USB serial).
- `arduino/relay_unlock.ino`: Arduino Uno sketch; serial speed 9600 baud; relay output D7.
- `.github/workflows/build-android-apk.yml`: GitHub Actions workflow to compile a debug APK.
- `ARDUINO_INTEGRATION.md`: server, Android, wiring and testing instructions.
- `GITHUB_SETUP.md`: steps to publish this folder and download the APK artifact.

## Before building

Edit `START_URL` in `android-usb-bridge/app/src/main/java/com/vsipl/messbridge/MainActivity.kt` to match the HTTPS URL of your Flask server, ending in `/scan`.

## Security and deployment

Keep this repository private. Do not commit live student data, SQLite databases, generated TLS certificates/private keys, `.secret_key`, credentials or signing keys. This is a prototype integration; validate the complete system with a safe test load and review door egress, emergency release and power-failure behavior before deployment.

See `GITHUB_SETUP.md` and `ARDUINO_INTEGRATION.md` for details.
