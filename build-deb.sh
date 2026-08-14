#!/bin/bash

set -e

# Check for required build tools
if ! command -v gcc &> /dev/null || ! command -v python3-config &> /dev/null; then
  echo "[-] Missing build tools. Please install gcc and python3-dev."
  exit 1
fi

CYTHON_BIN=$(command -v cython3 || command -v cython)
if [ -z "$CYTHON_BIN" ]; then
  echo "[-] Cython not found. Please install cython3."
  exit 1
fi

if [ ! -f "wifibox_agent_collector.py" ]; then
  echo "[-] Error: wifibox_agent_collector.py not found in the current directory!"
  exit 1
fi

PKG_NAME="wifibox-collector"
PKG_VERSION="1.0.6"
BUILD_DIR="./wifibox-collector-build"
INSTALL_DIR="home/oldendome/wifibox-agent-collector"
SERVICE_USER="oldendome"

# FORCE THE ARCHITECTURE TO armhf
ARCH="armhf"

echo "[+] Cleaning previous build files..."
rm -rf "$BUILD_DIR"
rm -f "${PKG_NAME}_${PKG_VERSION}_${ARCH}.deb"

echo "[+] Creating Debian package directory structure..."
mkdir -p "$BUILD_DIR/DEBIAN"
mkdir -p "$BUILD_DIR/$INSTALL_DIR/data"
mkdir -p "$BUILD_DIR/$INSTALL_DIR/pushed_backlogs"
mkdir -p "$BUILD_DIR/lib/systemd/system"

echo "[+] Writing control file..."
cat << EOF > "$BUILD_DIR/DEBIAN/control"
Package: ${PKG_NAME}
Version: ${PKG_VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Depends: python3, python3-requests, python3-pip
Maintainer: oldendome <admin@local>
Description: Wifibox Fleet Collector and Push Receiver Service (Cythonized)
 Scrapes fleet metrics, manages push backlogs, and exposes endpoints for Prometheus and Grafana.
EOF

echo "[+] Writing post-installation script..."
cat << EOF > "$BUILD_DIR/DEBIAN/postinst"
#!/bin/bash
set -e
SERVICE_USER="${SERVICE_USER}"
INSTALL_DIR="/${INSTALL_DIR}"

echo "[+] Ensuring python3-prometheus-client is installed..."
if ! python3 -c "import prometheus_client" &>/dev/null; then
    pip3 install prometheus-client --break-system-packages || pip3 install prometheus-client
fi

echo "[+] Setting up file permissions..."
if id "\$SERVICE_USER" &>/dev/null; then
    chown -R "\$SERVICE_USER:\$SERVICE_USER" "\$INSTALL_DIR"
fi

chmod +x "\$INSTALL_DIR/wifibox-agent-collector"

echo "[+] Enabling and starting systemd service..."
systemctl daemon-reload
systemctl enable wifibox-collector.service
systemctl restart wifibox-collector.service
exit 0
EOF
chmod 755 "$BUILD_DIR/DEBIAN/postinst"

echo "[+] Writing pre-removal script..."
cat << EOF > "$BUILD_DIR/DEBIAN/prerm"
#!/bin/bash
set -e
echo "[+] Stopping wifibox-collector service..."
systemctl stop wifibox-collector.service || true
systemctl disable wifibox-collector.service || true
exit 0
EOF
chmod 755 "$BUILD_DIR/DEBIAN/prerm"

echo "[+] Creating default wifibox_inventory.json..."
cat << 'EOF' > "$BUILD_DIR/$INSTALL_DIR/wifibox_inventory.json"
[
  {
    "uid": "wb-a1b2c3d4",
    "wg_ip": "10.5.1.132",
    "site": "sokmatech",
    "mode": "ap",
    "notes": "box acuan"
  }
]
EOF

echo "[+] Copying python source for Cythonizing..."
cp wifibox_agent_collector.py /tmp/wifibox_agent_collector.py

echo "[+] Cythonizing and compiling the python script..."
$CYTHON_BIN -3 --embed -o /tmp/wifibox_agent_collector.c /tmp/wifibox_agent_collector.py

PYTHON_CFLAGS=$(python3-config --cflags)
if python3-config --ldflags --embed >/dev/null 2>&1; then
    PYTHON_LDFLAGS=$(python3-config --ldflags --embed)
else
    PYTHON_LDFLAGS=$(python3-config --ldflags)
fi

echo "[+] Compiling C code to binary for $ARCH..."
gcc -O3 $PYTHON_CFLAGS /tmp/wifibox_agent_collector.c $PYTHON_LDFLAGS -o "$BUILD_DIR/$INSTALL_DIR/wifibox-agent-collector"
rm -f /tmp/wifibox_agent_collector.c /tmp/wifibox_agent_collector.py

echo "[+] Writing systemd service file..."
cat << EOF > "$BUILD_DIR/lib/systemd/system/wifibox-collector.service"
[Unit]
Description=Wifibox Fleet Collector Service
After=network.target

[Service]
User=${SERVICE_USER}
WorkingDirectory=/${INSTALL_DIR}
ExecStart=/${INSTALL_DIR}/wifibox-agent-collector
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "[+] Building Debian package..."
dpkg-deb --build "$BUILD_DIR" "${PKG_NAME}_${PKG_VERSION}_${ARCH}.deb"

echo "[+] Cleaning up build directory..."
rm -rf "$BUILD_DIR"

echo "[+] Success! Package created: ${PKG_NAME}_${PKG_VERSION}_${ARCH}.deb"
