#!/usr/bin/env bash
# =============================================================================
# Pi Streamer — One-shot installer for Raspberry Pi 3B
#
# Streams audio from a USB sound card line-in to an Icecast MP3 stream.
# Designed for always-on, headless, unattended operation.
#
# Usage:
#   sudo bash install.sh
#
# Prerequisites:
#   - Raspberry Pi OS Lite (64-bit, Bookworm) — fresh install
#   - USB audio adapter plugged in
#   - Internet connection
#   - SSH access
# =============================================================================
set -euo pipefail

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# --- Root check ---
if [[ $EUID -ne 0 ]]; then
    err "Run as root: sudo bash install.sh"
    exit 1
fi

REAL_USER="${SUDO_USER:-pi}"
INSTALL_DIR="/opt/pi-streamer"
SERVICE_NAME="pi-streamer"

echo ""
echo "============================================"
echo "  Pi Streamer Installer"
echo "  Line-In → Icecast MP3 Streaming"
echo "============================================"
echo ""

# =============================================================================
# 1. System packages
# =============================================================================
info "Updating package lists..."
apt-get update -qq

info "Installing dependencies (this takes a few minutes on Pi 3B)..."

# Pre-answer icecast2 configuration dialogs to prevent blocking
echo "icecast2 icecast2/icecast-setup boolean true" | debconf-set-selections

apt-get install -y \
    icecast2 \
    sox \
    ffmpeg \
    alsa-utils \
    python3 \
    python3-flask \
    python3-venv \
    curl \
    jq

info "Packages installed."

# =============================================================================
# 2. Tailscale
# =============================================================================
if ! command -v tailscale &>/dev/null; then
    info "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
    info "Tailscale installed."
else
    info "Tailscale already installed."
fi

# Enable and start tailscaled
systemctl enable --now tailscaled 2>/dev/null || true

# Check if already authenticated
if ! tailscale status &>/dev/null; then
    echo ""
    echo "============================================"
    echo "  TAILSCALE SETUP"
    echo "============================================"
    echo ""
    info "Starting Tailscale authentication..."
    echo ""
    tailscale up
    echo ""
fi

TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
TS_NAME=$(tailscale status --self --json 2>/dev/null | jq -r '.Self.DNSName // "unknown"' | sed 's/\.$//')
info "Tailscale connected: ${TS_IP} (${TS_NAME})"

# =============================================================================
# 3. Detect USB audio device
# =============================================================================
info "Detecting USB audio device..."
USB_CARD=""
while IFS= read -r line; do
    if echo "$line" | grep -qi "usb"; then
        USB_CARD=$(echo "$line" | awk -F'[ :]' '{print $2}')
        USB_NAME=$(echo "$line" | sed 's/.*\[//;s/\].*//')
        break
    fi
done < <(arecord -l 2>/dev/null || true)

if [[ -z "$USB_CARD" ]]; then
    warn "No USB audio device detected!"
    warn "Plug in your USB sound card and re-run, or set ALSA_DEVICE manually"
    warn "in ${INSTALL_DIR}/pi-streamer.conf after installation."
    USB_CARD="1"
    USB_NAME="not detected"
fi

ALSA_DEVICE="hw:${USB_CARD},0"
info "USB audio: card ${USB_CARD} [${USB_NAME}] → ${ALSA_DEVICE}"

# =============================================================================
# 4. Configure Icecast
# =============================================================================
info "Configuring Icecast..."

# Generate a random password
ICECAST_PW=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16)

cat > /etc/icecast2/icecast.xml << ICEXML
<icecast>
    <location>Pi Streamer</location>
    <admin>admin@localhost</admin>
    <limits>
        <clients>20</clients>
        <sources>2</sources>
        <queue-size>262144</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-on-connect>1</burst-on-connect>
        <burst-size>65535</burst-size>
    </limits>
    <authentication>
        <source-password>${ICECAST_PW}</source-password>
        <relay-password>${ICECAST_PW}</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>${ICECAST_PW}</admin-password>
    </authentication>
    <hostname>localhost</hostname>
    <listen-socket>
        <port>8000</port>
    </listen-socket>
    <mount>
        <mount-name>/scanner</mount-name>
    </mount>
    <fileserve>1</fileserve>
    <paths>
        <basedir>/usr/share/icecast2</basedir>
        <logdir>/var/log/icecast2</logdir>
        <webroot>/usr/share/icecast2/web</webroot>
        <adminroot>/usr/share/icecast2/admin</adminroot>
        <alias source="/" destination="/status.xsl"/>
    </paths>
    <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>3</loglevel>
        <logsize>10000</logsize>
    </logging>
    <security>
        <chroot>0</chroot>
    </security>
</icecast>
ICEXML

# Enable Icecast to start on boot
sed -i 's/ENABLE=false/ENABLE=true/' /etc/default/icecast2 2>/dev/null || true
systemctl enable icecast2
systemctl restart icecast2

info "Icecast configured (password: ${ICECAST_PW})"

# =============================================================================
# 5. Install application
# =============================================================================
info "Installing Pi Streamer to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}/templates"

# --- Config file ---
cat > "${INSTALL_DIR}/pi-streamer.conf" << CONF
# Pi Streamer Configuration
# Edit and restart: sudo systemctl restart pi-streamer

# ALSA capture device (find with: arecord -l)
ALSA_DEVICE=${ALSA_DEVICE}

# Icecast connection
ICECAST_HOST=localhost
ICECAST_PORT=8000
ICECAST_SOURCE_PASSWORD=${ICECAST_PW}

# Web UI port
WEB_UI_PORT=5080
CONF

# --- App ---
cp "$(dirname "$0")/app.py" "${INSTALL_DIR}/app.py"
cp "$(dirname "$0")/templates/index.html" "${INSTALL_DIR}/templates/index.html"

chown -R root:root "${INSTALL_DIR}"
chmod 644 "${INSTALL_DIR}/pi-streamer.conf"

info "Application installed."

# =============================================================================
# 6. Systemd service
# =============================================================================
info "Creating systemd service..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << UNIT
[Unit]
Description=Pi Streamer — Line-In to Icecast
After=network.target icecast2.service
Wants=icecast2.service

[Service]
Type=simple
EnvironmentFile=${INSTALL_DIR}/pi-streamer.conf
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/app.py
WorkingDirectory=${INSTALL_DIR}
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pi-streamer

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

info "Service installed and started."

# =============================================================================
# 7. Summary
# =============================================================================
echo ""
echo "============================================"
echo "  INSTALLATION COMPLETE"
echo "============================================"
echo ""
echo "  Audio device:  ${ALSA_DEVICE} [${USB_NAME}]"
echo ""
echo "  Web UI:"
echo "    Local:     http://localhost:5080"
echo "    Tailscale: http://${TS_IP}:5080"
echo "    DNS:       http://${TS_NAME}:5080"
echo ""
echo "  Stream URL:"
echo "    Local:     http://localhost:8000/scanner"
echo "    Tailscale: http://${TS_IP}:8000/scanner"
echo "    DNS:       http://${TS_NAME}:8000/scanner"
echo ""
echo "  Icecast admin:"
echo "    URL:       http://localhost:8000/admin/"
echo "    User:      admin"
echo "    Password:  ${ICECAST_PW}"
echo ""
echo "  Config file:  ${INSTALL_DIR}/pi-streamer.conf"
echo "  Logs:         journalctl -u pi-streamer -f"
echo ""
echo "  Commands:"
echo "    sudo systemctl restart pi-streamer"
echo "    sudo systemctl stop pi-streamer"
echo "    sudo systemctl status pi-streamer"
echo ""
echo "============================================"
