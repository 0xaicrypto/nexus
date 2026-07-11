#!/usr/bin/env bash
# generate_self_signed_cert.sh — produce a self-signed TLS cert + key
# for the Nexus server.
#
# When to use this
# ================
# You want HTTPS for remote access but don't have a
# domain (so Let's Encrypt is hard) and don't want to set up Caddy +
# nip.io. Trade-off: browsers show a "Not Secure" warning the user has
# to click through once.
#
# What it does
# ============
# Calls openssl to mint a 10-year RSA-2048 cert covering:
#   - The hostname you pass on the command line (default: $HOSTNAME or
#     auto-detected public IP)
#   - localhost / 127.0.0.1 / ::1 (so dev still works)
# Drops `cert.pem` + `key.pem` into the repo root.
#
# Usage
# =====
#   ./scripts/generate_self_signed_cert.sh                   # auto-detect
#   ./scripts/generate_self_signed_cert.sh nexus.local       # custom CN
#   ./scripts/generate_self_signed_cert.sh 165.227.135.198   # IP
#
# After running, start the server with:
#   uv run nexus-server --ssl-certfile cert.pem --ssl-keyfile key.pem
# or set env vars SSL_CERTFILE=cert.pem SSL_KEYFILE=key.pem.
#
# Trust the cert in browsers / desktop
# ====================================
# - Brave/Chrome: visit https://<host>:8001, click "Advanced" →
#   "Proceed to <host> (unsafe)" once. Future visits are remembered
#   for the session.
# - Avalonia desktop's embedded WebView: same one-time prompt.
# - macOS: optionally `security add-trusted-cert -d -r trustRoot \
#   -k ~/Library/Keychains/login.keychain-db cert.pem` — kills the
#   warning permanently for that mac.

set -euo pipefail

cd "$(dirname "$0")/.."

CN="${1:-${HOSTNAME:-$(curl -fsSL --max-time 5 https://ifconfig.me 2>/dev/null || echo 'localhost')}}"
CERT_OUT="cert.pem"
KEY_OUT="key.pem"

if ! command -v openssl >/dev/null 2>&1; then
    echo "✗ openssl not found. Install with:"
    echo "    macOS:   brew install openssl"
    echo "    Debian:  sudo apt install openssl"
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Generating self-signed TLS cert for: $CN"
echo "════════════════════════════════════════════════════════════════"
echo ""

# SAN config — IPs go to IP.x, hostnames to DNS.x. We always include
# localhost variants so dev workflows keep working.
SAN_CONFIG="$(mktemp)"
cat > "$SAN_CONFIG" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
CN = $CN

[v3_req]
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = @alt_names

[alt_names]
DNS.1 = localhost
IP.1  = 127.0.0.1
IP.2  = ::1
EOF

# Append the requested CN as either DNS or IP based on shape.
if [[ "$CN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "IP.3  = $CN" >> "$SAN_CONFIG"
else
    echo "DNS.2 = $CN" >> "$SAN_CONFIG"
fi

openssl req \
    -x509 \
    -newkey rsa:2048 \
    -nodes \
    -days 3650 \
    -keyout "$KEY_OUT" \
    -out "$CERT_OUT" \
    -config "$SAN_CONFIG" \
    2>&1 | grep -v "^\.\.\." || true

rm -f "$SAN_CONFIG"

# Permissions: key MUST be 0600 (uvicorn refuses world-readable keys
# in some configs, and it's the right hygiene anyway).
chmod 600 "$KEY_OUT"
chmod 644 "$CERT_OUT"

echo ""
echo "✓ wrote $CERT_OUT (cert)"
echo "✓ wrote $KEY_OUT (private key, 0600)"
echo ""
echo "Start the server with HTTPS:"
echo ""
echo "  cd packages/server"
echo "  uv run nexus-server \\"
echo "    --ssl-certfile ../../$CERT_OUT \\"
echo "    --ssl-keyfile ../../$KEY_OUT"
echo ""
echo "Then point the desktop at  https://$CN:8001"
echo ""
echo "Cert details:"
openssl x509 -in "$CERT_OUT" -noout -subject -issuer -dates 2>/dev/null | sed 's/^/  /'
