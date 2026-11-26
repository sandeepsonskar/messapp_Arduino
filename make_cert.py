"""Create / refresh the self-signed HTTPS certificate for this server's current IP addresses."""
import run_local as R

ips = R.local_ips()
R.ensure_cert(ips)
print("HTTPS certificate ready for:", ", ".join(ips))
