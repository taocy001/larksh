#!/bin/sh
# larksh install script — compatible with standard Linux and OpenWrt
set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/larksh}"

# Python version check (requires 3.10+)
python3 -c "
import sys
if sys.version_info < (3, 10):
    print(f'❌ Python 3.10+ required, current version {sys.version.split()[0]}')
    sys.exit(1)
" || exit 1

echo "→ Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r bot messaging security shell utils main.py requirements.txt larksh-client "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/larksh-client"

echo "→ Creating virtual environment and installing dependencies ..."
python3 -m venv "$INSTALL_DIR/.venv"
# Prefer the lock file to ensure version consistency
if [ -f "$INSTALL_DIR/requirements.lock" ]; then
  "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.lock"
else
  "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
fi

echo "→ Creating log directory ..."
mkdir -p /var/log/larksh

echo ""
echo "✅ Installation complete!"
echo "   1. Configure: cp dist-files/config.example.yaml $INSTALL_DIR/config.yaml && vi $INSTALL_DIR/config.yaml"
echo "   2. Start:     $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/main.py --config $INSTALL_DIR/config.yaml"
echo ""

# Log rotation
if command -v logrotate >/dev/null 2>&1 && [ -f dist-files/larksh.logrotate ]; then
  echo "   Log rotation (optional):"
  echo "     cp dist-files/larksh.logrotate /etc/logrotate.d/larksh"
  echo ""
fi

# systemd (standard Linux)
if command -v systemctl >/dev/null 2>&1 && [ -f dist-files/larksh.service ]; then
  echo "   systemd deployment (remember to set User= to the actual runtime user):"
  echo "     cp dist-files/larksh.service /etc/systemd/system/"
  echo "     systemctl enable --now larksh"
  echo ""
fi

# OpenWrt procd init.d (optional)
if [ -d /etc/init.d ] && ! command -v systemctl >/dev/null 2>&1; then
  echo "   OpenWrt autostart:"
  echo "     cat > /etc/init.d/larksh <<'EOF'"
  echo "     #!/bin/sh /etc/rc.common"
  echo "     START=99"
  echo "     start() { $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/main.py --config /etc/larksh/config.yaml > /var/log/larksh/larksh.log 2>&1 & }"
  echo "     stop() { killall -f 'python.*main.py' 2>/dev/null || true; }"
  echo "     EOF"
  echo "     chmod +x /etc/init.d/larksh && /etc/init.d/larksh enable"
fi
