#!/bin/bash
set -e

# --- Configuration ---
# Take architecture from arguments, default to host architecture
ARCH=${1:-$(dpkg --print-architecture)}
PKG_NAME="wifibox-agent"
VERSION="1.0.0"
PKG_DIR="${PKG_NAME}_${VERSION}_${ARCH}"
BUILD_SRC="build_src"

# --- Tailscale Configuration ---
# Replace this with your Tailscale OAuth Key or Auth Key
# Example: tskey-client-cXXXXXXXXXXXX
TAILSCALE_OAUTH_KEY="tskey-client-kuXBDwn6cR11CNTRL-aTJxb2CqNs5vwWd4w9Zor5UY8GnFBGsZi"


echo "=========================================="
echo " Building Cythonized Debian Package"
echo " Target Architecture: $ARCH"
echo " Package Name: $PKG_DIR.deb"
echo "=========================================="

if [ ! -f "wifibox_agent.py" ]; then
    echo "Error: wifibox_agent.py not found in the current directory."
    exit 1
fi

# Auto-install build dependencies if running directly (GitHub Actions also handles this)
if command -v apt-get >/dev/null; then
    echo "Auto-installing build dependencies..."
    apt-get update
    apt-get install -y dpkg-dev cython3 gcc python3 python3-dev
fi

# 1. Create Directory Structure
mkdir -p "$PKG_DIR/DEBIAN"
mkdir -p "$PKG_DIR/home/oldendome/wifibox-agent"
mkdir -p "$PKG_DIR/lib/systemd/system"
mkdir -p "$BUILD_SRC"

# 2. Create the Control File (Added 'curl' for the Tailscale install script)
cat << EOF > "$PKG_DIR/DEBIAN/control"
Package: $PKG_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3, python3-pip, wireguard-tools, net-tools, iw, iptables, isc-dhcp-server, dnsmasq, rsync, curl
Maintainer: Your Name <your.email@example.com>
Description: Wifibox Agent Prometheus Exporter (Cythonized)
 A background binary service to monitor Raspberry Pi network, VPN, Tailscale, and DHCP health.
EOF

# 3. Create Post-Install Script
cat << EOF_POSTINST > "$PKG_DIR/DEBIAN/postinst"
#!/bin/bash
set -e

echo "Installing Python dependencies (prometheus_client, requests)..."
pip3 install prometheus_client requests --break-system-packages || pip3 install prometheus_client requests

# Setup user
if ! id "dev" &>/dev/null; then
    useradd -r -s /bin/false dev
fi

chown -R dev:dev /home/oldendome/wifibox-agent
chmod 755 /home/oldendome/wifibox-agent/wifibox-agent

# --- Tailscale Installation & Authentication ---
if ! command -v tailscale &> /dev/null; then
    echo "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
else
    echo "Tailscale is already installed."
fi

# Authenticate Tailscale if the key was provided
if [ "$TAILSCALE_OAUTH_KEY" != "tskey-client-YOUR_OAUTH_KEY_HERE" ] && [ -n "$TAILSCALE_OAUTH_KEY" ]; then
    echo "Authenticating Tailscale with OAuth Key..."
    # You can add additional flags here like --accept-routes or --advertise-tags if needed
    tailscale up --authkey "$TAILSCALE_OAUTH_KEY" --reset || echo "Warning: Tailscale authentication failed."
else
    echo "Skipping Tailscale authentication (No valid OAuth key provided in build script)."
fi
# -----------------------------------------------

echo "Starting wifibox-agent service..."
systemctl daemon-reload
systemctl enable wifibox-agent.service
systemctl restart wifibox-agent.service
EOF_POSTINST

# 4. Create Pre-Remove Script
cat << 'EOF' > "$PKG_DIR/DEBIAN/prerm"
#!/bin/bash
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    systemctl stop wifibox-agent.service || true
    systemctl disable wifibox-agent.service || true
fi
EOF

# 5. Create Post-Remove Script
cat << 'EOF' > "$PKG_DIR/DEBIAN/postrm"
#!/bin/bash
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    systemctl daemon-reload
fi
EOF

chmod 755 "$PKG_DIR/DEBIAN/postinst"
chmod 755 "$PKG_DIR/DEBIAN/prerm"
chmod 755 "$PKG_DIR/DEBIAN/postrm"

# 6. Cythonize and Compile the Python script
echo "Compiling Python script to C with Cython..."
cp wifibox_agent.py "$BUILD_SRC/"
cython3 --embed -o "$BUILD_SRC/wifibox_agent.c" "$BUILD_SRC/wifibox_agent.py"

echo "Compiling C code to binary executable with GCC..."
CFLAGS=$(python3-config --cflags)
LDFLAGS=$(python3-config --embed --ldflags 2>/dev/null || python3-config --ldflags)

gcc -Os $CFLAGS -o "$PKG_DIR/home/oldendome/wifibox-agent/wifibox-agent" "$BUILD_SRC/wifibox_agent.c" $LDFLAGS

# 7. Create the Systemd Service File
cat << 'EOF_SERVICE' > "$PKG_DIR/lib/systemd/system/wifibox-agent.service"
[Unit]
Description=Wifibox Agent Prometheus Exporter
After=network.target

[Service]
User=dev
ExecStart=/home/oldendome/wifibox-agent/wifibox-agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF_SERVICE

# 8. Build the Debian package
echo "Building the .deb file..."
dpkg-deb --build "$PKG_DIR"

# 9. Cleanup
rm -rf "$PKG_DIR"
rm -rf "$BUILD_SRC"

echo "Success! Package created: $PKG_DIR.deb"
