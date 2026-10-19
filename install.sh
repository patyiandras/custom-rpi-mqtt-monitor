#!/usr/bin/env bash
# ============================================================================
#  custom-rpi-mqtt-monitor telepítő / frissítő — a Pi-n, a repó mappájából:
#      sudo ./install.sh              telepítés vagy frissítés (bekéri az MQTT-adatokat; Enter = marad)
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
  echo "Új config létrehozva: $CONF"
fi
chgrp "$APP_USER" "$CONF" && chmod 640 "$CONF"

# A config egy [mqtt]-kulcsának átírása (az érték környezeti változóban megy, így nem látszik a ps-ben)
set_mqtt_value() {
  KEY="$1" VALUE="$2" CONF="$CONF" python3 - <<'PYEOF'
import json, os, re
path, key, value = os.environ["CONF"], os.environ["KEY"], os.environ["VALUE"]
text = open(path, encoding="utf-8").read()
line = "%s = %s" % (key, json.dumps(value))
pattern = r'(?m)^(\s*#?\s*)%s\s*=.*$' % re.escape(key)
text, n = re.subn(pattern, lambda m: line, text, count=1)
if not n:
    text = text.replace("[mqtt]\n", "[mqtt]\n%s\n" % line, 1)
open(path, "w", encoding="utf-8").write(text)
PYEOF
}
conf_value() {
  sed -n "s/^$1 *= *\"\(.*\)\".*/\1/p" "$CONF" | head -1
}

# MQTT-adatok bekérése (Enter = a meglévő marad). A jelszót addig kéri, amíg a broker el nem fogadja.
if [ -t 0 ]; then
  OK=0
  for attempt in 1 2 3; do
    CUR_HOST=$(conf_value host); CUR_USER=$(conf_value username); CUR_PASS=$(conf_value password)
    read -r -p "MQTT broker címe [$CUR_HOST]: " NEW_HOST || true
    read -r -p "MQTT felhasználó [$CUR_USER]: " NEW_USER || true
    if [ "$CUR_PASS" = "IDE_A_JELSZO" ] || [ -z "$CUR_PASS" ]; then
      PROMPT="MQTT jelszó: "
    else
      PROMPT="MQTT jelszó [Enter = a meglévő marad]: "
    fi
    read -r -s -p "$PROMPT" NEW_PASS || true; echo
    [ -n "$NEW_HOST" ] && set_mqtt_value host "$NEW_HOST"
    [ -n "$NEW_USER" ] && set_mqtt_value username "$NEW_USER"
    [ -n "$NEW_PASS" ] && set_mqtt_value password "$NEW_PASS"
    if [ "$(conf_value password)" = "IDE_A_JELSZO" ] || [ -z "$(conf_value password)" ]; then
      echo "Jelszó nélkül nem tud csatlakozni, add meg."
      continue
    fi
    if sudo -u "$APP_USER" python3 "$APP_DIR/rpi_mqtt_monitor.py" --config "$CONF" --test-mqtt; then
      OK=1; break
    fi
    echo "Próbáld újra."
  done
  [ "$OK" = 1 ] || { echo "[HIBA] Háromszor sem sikerült bejelentkezni a brokerhez."; exit 1; }
else
  grep -q 'IDE_A_JELSZO' "$CONF" && { echo "[HIBA] A $CONF-ban még a minta-jelszó van (nem interaktív futás)."; exit 1; }
fi

echo "=== 5/6 Sudo-jogok (csak a konkrét parancsok): $SUDOERS ==="
TMP=$(mktemp)
CMDS=$(sudo -u "$APP_USER" python3 "$APP_DIR/rpi_mqtt_monitor.py" --config "$CONF" --print-sudo)
{
  echo "# custom-rpi-mqtt-monitor — generálta: install.sh ($(date '+%F %T')). Kézzel ne szerkeszd."
  SAFE='^/[][A-Za-z0-9@._/ -]+$'   # csak abszolút útvonalú, biztonságos karakterekből álló parancs
  while IFS= read -r cmd; do
    [[ "$cmd" =~ $SAFE ]] && echo "$APP_USER ALL=(root) NOPASSWD: $cmd"
  done <<< "$CMDS"
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
