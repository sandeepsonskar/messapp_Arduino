"""
Run the mess server on the local network with BOTH:
    http://<this-pc-ip>:5000    -> students (normal)
    https://<this-pc-ip>:5443   -> mess supervisors (camera scanner needs HTTPS)
A self-signed certificate is created automatically (re-created if the PC's IP changes).
First time on a phone: the browser warns "not private" -> Advanced -> Proceed. Then the camera works.

    python run_local.py
"""
import os, socket, datetime, ipaddress, threading
from werkzeug.serving import make_server

BASE = os.path.dirname(os.path.abspath(__file__))
CERT, KEY, META = (os.path.join(BASE, n) for n in ("cert.pem", "key.pem", "cert_ips.txt"))
HTTP_PORT, HTTPS_PORT = 5000, 5443


def local_ips():
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # no packet is sent; just finds the LAN interface
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    return sorted(ips)


def ensure_cert(ips):
    want = ",".join(ips)
    if os.path.exists(CERT) and os.path.exists(KEY) and os.path.exists(META) and open(META).read() == want:
        return
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mess Server")])
    san = x509.SubjectAlternativeName([x509.DNSName("localhost")] +
                                      [x509.IPAddress(ipaddress.ip_address(i)) for i in ips])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(san, critical=False)
            # This local self-signed certificate is also installed as a user CA
            # on the dedicated supervisor Android device for WebView HTTPS trust.
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=True, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False,
                                         decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(META, "w") as f:
        f.write(want)


if __name__ == "__main__":
    import app as A
    ips = local_ips()
    ensure_cert(ips)
    http_srv = make_server("0.0.0.0", HTTP_PORT, A.app, threaded=True)
    https_srv = make_server("0.0.0.0", HTTPS_PORT, A.app, threaded=True, ssl_context=(CERT, KEY))
    print("\n=== Mess server running ===")
    for ip in ips:
        if ip != "127.0.0.1":
            print(f"  Students    : http://{ip}:{HTTP_PORT}")
            print(f"  Supervisors : https://{ip}:{HTTPS_PORT}   (use this for the camera scanner)")
    print("Press Ctrl+C to stop.\n")
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    try:
        https_srv.serve_forever()
    except KeyboardInterrupt:
        pass
