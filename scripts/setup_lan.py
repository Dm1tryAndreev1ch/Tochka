"""Prepare a private LAN CA, iOS/Android onboarding, and a TLS proxy."""

import argparse
import ipaddress
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

ROOT = Path(__file__).resolve().parent.parent


def configure_env(updates):
    path = ROOT / ".env"
    lines = [line for line in path.read_text().splitlines() if line.partition("=")[0] not in updates]
    lines.extend(f"{key}={value}" for key, value in updates.items())
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip")
    args = parser.parse_args()
    if not shutil.which("mkcert") or not shutil.which("caddy"):
        raise SystemExit("Install native TLS tools: brew install mkcert caddy")
    ip = args.ip or subprocess.check_output(["ipconfig", "getifaddr", "en0"], text=True).strip()
    address = ipaddress.ip_address(ip)
    if not address.is_private or address.is_loopback:
        raise SystemExit("Use a private Wi-Fi address")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    private = Path.home() / ".cache/voice-desk/private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    ca = private / "lan-ca"
    old_ca = runtime / "lan-ca"
    if not ca.exists() and old_ca.exists():
        shutil.copytree(old_ca, ca)
    ca.mkdir(exist_ok=True, mode=0o700)
    ca.chmod(0o700)
    subprocess.run(
        [
            "mkcert",
            "-cert-file",
            str(runtime / "lan-server.pem"),
            "-key-file",
            str(private / "lan-server-key.pem"),
            ip,
            "localhost",
            "127.0.0.1",
        ],
        env={**os.environ, "CAROOT": str(ca)},
        check=True,
    )
    (private / "lan-server-key.pem").chmod(0o600)
    (ca / "rootCA-key.pem").chmod(0o600)
    certificate = x509.load_pem_x509_certificate((ca / "rootCA.pem").read_bytes())
    der = certificate.public_bytes(serialization.Encoding.DER)
    public = runtime / "mobile-public"
    public.mkdir(exist_ok=True)
    (public / "voice-desk-ca.cer").write_bytes(der)
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": "local.voice-desk.wifi",
        "PayloadUUID": str(uuid4()),
        "PayloadDisplayName": "Точка · Wi-Fi",
        "PayloadContent": [
            {
                "PayloadType": "com.apple.security.root",
                "PayloadVersion": 1,
                "PayloadIdentifier": "local.voice-desk.wifi.ca",
                "PayloadUUID": str(uuid4()),
                "PayloadDisplayName": "Точка Wi-Fi CA",
                "PayloadCertificateFileName": "voice-desk-ca.cer",
                "PayloadContent": der,
            }
        ],
    }
    (public / "voice-desk.mobileconfig").write_bytes(plistlib.dumps(profile))
    url = f"https://{ip}:8443"
    setup_url = f"http://{ip}:8765"
    config = {
        "ip": ip,
        "url": url,
        "setup_url": setup_url,
        "ca_sha256": certificate.fingerprint(hashes.SHA256()).hex().upper(),
    }
    (runtime / "mobile.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    import qrcode
    import qrcode.image.svg

    qrcode.make(setup_url, image_factory=qrcode.image.svg.SvgPathImage).save(public / "setup-qr.svg")
    (runtime / "Caddyfile").write_text(
        """{
    admin off
    auto_https disable_redirects
}
https://"""
        + ip
        + """:8443, https://localhost:8443, https://127.0.0.1:8443 {
    tls /certs/lan-server.pem /certs/lan-server-key.pem
    reverse_proxy 127.0.0.1:7860 {
        header_up Host 127.0.0.1:7860
    }
}
"""
    )
    config_path = runtime / "Caddyfile"
    caddy_config = (
        config_path.read_text()
        .replace("/certs/lan-server.pem", str(runtime / "lan-server.pem"))
        .replace("/certs/lan-server-key.pem", str(private / "lan-server-key.pem"))
    )
    caddy_config = caddy_config.replace(
        "    reverse_proxy 127.0.0.1:7860", "    bind 127.0.0.1 " + ip + "\n    reverse_proxy 127.0.0.1:7860"
    )
    config_path.write_text(caddy_config)
    subprocess.run(["caddy", "validate", "--config", str(config_path), "--adapter", "caddyfile"], check=True)
    for old_key in (runtime / "lan-server-key.pem", old_ca / "rootCA-key.pem"):
        if old_key.exists():
            old_key.unlink()
    configure_env(
        {
            "ALLOWED_ORIGINS": f"http://127.0.0.1:7860,http://localhost:7860,{url},https://127.0.0.1:8443,https://localhost:8443",
            "ICE_HOST_IP": f"127.0.0.1,{ip}",
            "LAN_ENABLED": "true",
            "LAN_IP": ip,
        }
    )
    print(json.dumps(config, ensure_ascii=False, indent=2))
    print("The CA private key remains on the Mac. Install only the public certificate on phones.")


if __name__ == "__main__":
    main()
