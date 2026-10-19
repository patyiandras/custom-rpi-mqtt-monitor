#!/usr/bin/env bash
# ============================================================================
#  custom-rpi-mqtt-monitor telepítő / frissítő — a Pi-n, a repó mappájából:
#      sudo ./install.sh              telepítés vagy frissítés (config-módosítás után is ezt futtasd)
#      sudo ./install.sh --uninstall  eltávolítás (a HA-entitásokat is törli)
#  Többször is futtatható; a meglévő /etc/rpi-mqtt-monitor/config.toml-hoz nem nyúl.
# ============================================================================
set -euo pipefail

APP_USER=rpimon
APP_DIR=/opt/rpi-mqtt-monitor
CONF_DIR=/etc/rpi-mqtt-monitor
CONF=$CONF_DIR/config.toml
SUDOERS=/etc/sudoers.d/rpi-mqtt-monitor
UNIT=/etc/systemd/system/rpi-mqtt-monitor.service
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "[HIBA] root kell: sudo $0 $*"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  if [ -f "$APP_DIR/rpi_mqtt_monitor.py" ] && [ -f "$CONF" ]; then
    STATE_DIRECTORY=/var/lib/rpi-mqtt-monitor python3 "$APP_DIR/rpi_mqtt_monitor.py" --config "$CONF" --remove-discovery || true
  fi
  systemctl disable --now rpi-mqtt-monitor.service 2>/dev/null || true
  rm -f "$UNIT" "$SUDOERS"
  rm -rf "$APP_DIR" /var/lib/rpi-mqtt-monitor
  systemctl daemon-reload
  userdel "$APP_USER" 2>/dev/null || true
  echo "Eltávolítva. A config megmaradt: $CONF (kézzel törölhető)."
  exit 0
fi

echo "=== 1/6 Csomagok ==="
PKGS="python3 python3-paho-mqtt smartmontools"
python3 -c 'import tomllib' 2>/dev/null || PKGS="$PKGS python3-toml"   # Python < 3.11 (pl. Buster)
apt-get install -y --no-install-recommends $PKGS >/dev/null 2>&1 || { apt-get update >/dev/null; apt-get install -y --no-install-recommends $PKGS >/dev/null; }
echo "OK: $PKGS"

echo "=== 2/6 Felhasználó: $APP_USER ==="
id "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$APP_USER"
for g in docker video systemd-journal adm; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$APP_USER"
done
id "$APP_USER"

echo "=== 3/6 Program: $APP_DIR ==="
install -d -m 755 "$APP_DIR"
install -m 755 "$SRC/rpi_mqtt_monitor.py" "$APP_DIR/rpi_mqtt_monitor.py"

echo "=== 4/6 Config: $CONF ==="
install -d -m 750 -g "$APP_USER" "$CONF_DIR"
if [ ! -f "$CONF" ]; then
  install -m 640 -g "$APP_USER" "$SRC/config.example.toml" "$CONF"
  echo
  echo ">>> Új config létrehozva: $CONF"
  echo ">>> Írd bele az MQTT-jelszót (és nézd át a figyelt szolgáltatásokat), majd futtasd újra: sudo $0"
  exit 0
fi
chgrp "$APP_USER" "$CONF" && chmod 640 "$CONF"
grep -q 'IDE_A_JELSZO' "$CONF" && { echo "[HIBA] A $CONF-ban még a minta-jelszó van."; exit 1; }

echo "=== 5/6 Sudo-jogok (csak a konkrét parancsok): $SUDOERS ==="
SYSTEMCTL=$(command -v systemctl)
SMARTCTL=$(command -v smartctl || true)
TMP=$(mktemp)
{
  echo "# custom-rpi-mqtt-monitor — generálta: install.sh ($(date '+%F %T')). Kézzel ne szerkeszd."
  echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL reboot, $SYSTEMCTL poweroff"
  UNITS=$(sudo -u "$APP_USER" python3 "$APP_DIR/rpi_mqtt_monitor.py" --config "$CONF" --print-units)
  for u in $UNITS; do
    # csak biztonságos unitnév kerülhet a sudoers-be
    [[ "$u" =~ ^[A-Za-z0-9@._-]+$ ]] && echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $u"
  done
  if [ -n "$SMARTCTL" ]; then
    echo "$APP_USER ALL=(root) NOPASSWD: $SMARTCTL -n standby -j -H -A /dev/sd[a-z], $SMARTCTL -n standby -j -H -A -d sat /dev/sd[a-z]"
  fi
} > "$TMP"
visudo -cf "$TMP" >/dev/null || { echo "[HIBA] A generált sudoers hibás:"; cat "$TMP"; rm -f "$TMP"; exit 1; }
install -m 440 "$TMP" "$SUDOERS"
rm -f "$TMP"
sed 's/^/   /' "$SUDOERS"

echo "=== 6/6 Szolgáltatás ==="
install -m 644 "$SRC/rpi-mqtt-monitor.service" "$UNIT"
systemctl daemon-reload
systemctl enable rpi-mqtt-monitor.service >/dev/null
systemctl restart rpi-mqtt-monitor.service
sleep 3
systemctl --no-pager --lines=15 status rpi-mqtt-monitor.service || true
echo
echo "Kész. Napló: journalctl -u rpi-mqtt-monitor -f"
echo "A HA-ban: Beállítások > Eszközök > MQTT > \"$(sed -n 's/^name *= *"\(.*\)".*/\1/p' "$CONF" | head -1)\""
