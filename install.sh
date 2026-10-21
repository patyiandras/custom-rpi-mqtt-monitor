#!/usr/bin/env bash
# ============================================================================
#  custom-rpi-mqtt-monitor telepítő / frissítő — a Pi-n, a repó mappájából:
#      sudo ./install.sh              telepítés vagy frissítés (bekéri az MQTT-adatokat és az eszköznevet; Enter = marad)
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

# A config egy kulcsának olvasása / átírása szakaszon belül ([mqtt] host, [device] name, ...).
# Az érték környezeti változóban megy át, így nem látszik a ps-ben.
conf_value() {
  SECTION="$1" KEY="$2" CONF="$CONF" python3 - <<'PYEOF'
import json, os, re
sec, key = None, os.environ["KEY"]
for line in open(os.environ["CONF"], encoding="utf-8"):
    m = re.match(r"\s*\[([^\]]+)\]", line)
    if m:
        sec = m.group(1).strip()
        continue
    m = re.match(r"\s*%s\s*=\s*(.*)$" % re.escape(key), line)
    if m and sec == os.environ["SECTION"]:
        rest = m.group(1).strip()
        try:
            value = json.JSONDecoder().raw_decode(rest)[0]
        except ValueError:
            value = rest.split("#")[0].strip()
        print(value)
        break
PYEOF
}
set_value() {
  SECTION="$1" KEY="$2" VALUE="$3" CONF="$CONF" python3 - <<'PYEOF'
import json, os, re
path, section, key = os.environ["CONF"], os.environ["SECTION"], os.environ["KEY"]
new = "%s = %s" % (key, json.dumps(os.environ["VALUE"], ensure_ascii=False))
lines = open(path, encoding="utf-8").read().split("\n")
head = next((i for i, l in enumerate(lines) if re.match(r"\s*\[%s\]" % re.escape(section), l)), None)
if head is None:
    lines += ["", "[%s]" % section, new]
else:
    end = next((i for i in range(head + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    hit = next((i for i in range(head + 1, end) if re.match(r"\s*#?\s*%s\s*=" % re.escape(key), lines[i])), None)
    if hit is None:
        lines.insert(head + 1, new)
    else:
        lines[hit] = new
open(path, "w", encoding="utf-8").write("\n".join(lines))
PYEOF
}

if [ -t 0 ]; then
  # --- Az MQTT-felhasználó neve szabadon választható: előbb ezt kérjük, hogy a HA-teendőkben már ez szerepeljen
  CUR_USER=$(conf_value mqtt username); CUR_USER=${CUR_USER:-pi4b}
  echo
  echo "Ennek a gépnek saját MQTT-felhasználó kell a Home Assistant"
  echo "Mosquitto brokerében. A neve szabadon választható."
  read -r -p "MQTT felhasználónév [$CUR_USER]: " NEW_USER || true
  [ -n "$NEW_USER" ] && set_value mqtt username "$NEW_USER"
  HA_USER=$(conf_value mqtt username)
  # --- Mit kell előtte a Home Assistantban beállítani (rövid sorok: telefonos terminálban is olvasható)
  cat <<EOF

------------------------------------------------------------
 Home Assistant oldalon ehhez ez kell (ha még nincs meg):

 1. Mosquitto broker bővítmény:
    Beállítások > Bővítmények > Bővítménybolt
    > Mosquitto broker > Telepítés, majd Indítás.

 2. Saját MQTT-felhasználó ennek a gépnek:
    Mosquitto broker > Konfiguráció > logins:
      - username: $HA_USER
        password: <hosszú, véletlen jelszó>
    Mentés, majd a bővítmény újraindítása.

 3. MQTT integráció: Beállítások > Eszközök és
    szolgáltatások > MQTT (a Mosquitto után a HA
    felajánlja, elég jóváhagyni).

 A broker címe a HA gép IP-címe, port: 1883.
 Ezután a gép magától megjelenik a HA-ban eszközként.
------------------------------------------------------------

EOF
  # --- MQTT-adatok (Enter = a meglévő marad). A jelszót addig kéri, amíg a broker el nem fogadja.
  OK=0
  for attempt in 1 2 3; do
    CUR_HOST=$(conf_value mqtt host); CUR_USER=$(conf_value mqtt username); CUR_PASS=$(conf_value mqtt password)
    read -r -p "MQTT broker címe [$CUR_HOST]: " NEW_HOST || true
    NEW_USER=""
    if [ "$attempt" -gt 1 ]; then  # sikertelen bejelentkezés után a név is javítható
      read -r -p "MQTT felhasználó [$CUR_USER]: " NEW_USER || true
    fi
    if [ "$CUR_PASS" = "IDE_A_JELSZO" ] || [ -z "$CUR_PASS" ]; then
      PROMPT="MQTT jelszó: "
    else
      PROMPT="MQTT jelszó [Enter = a meglévő marad]: "
    fi
    read -r -s -p "$PROMPT" NEW_PASS || true; echo
    [ -n "$NEW_HOST" ] && set_value mqtt host "$NEW_HOST"
    [ -n "$NEW_USER" ] && set_value mqtt username "$NEW_USER"
    [ -n "$NEW_PASS" ] && set_value mqtt password "$NEW_PASS"
    if [ "$(conf_value mqtt password)" = "IDE_A_JELSZO" ] || [ -z "$(conf_value mqtt password)" ]; then
      echo "Jelszó nélkül nem tud csatlakozni, add meg."
      continue
    fi
    if sudo -u "$APP_USER" python3 "$APP_DIR/rpi_mqtt_monitor.py" --config "$CONF" --test-mqtt; then
      OK=1; break
    fi
    echo "Próbáld újra."
  done
  [ "$OK" = 1 ] || { echo "[HIBA] Háromszor sem sikerült bejelentkezni a brokerhez."; exit 1; }

  # --- Eszköz a HA-ban: megjelenő név és az entitás-id előtag (sensor.<id>_cpu_homerseklet, ...)
  CUR_NAME=$(conf_value device name); CUR_ID=$(conf_value device id)
  read -r -p "Eszköz neve a Home Assistantban [$CUR_NAME]: " NEW_NAME || true
  [ -n "$NEW_NAME" ] && set_value device name "$NEW_NAME"
  while true; do
    read -r -p "Entitás-azonosító előtag (kisbetű, szám, _) [$CUR_ID]: " NEW_ID || true
    NEW_ID=${NEW_ID:-$CUR_ID}
    [[ "$NEW_ID" =~ ^[a-z0-9_]+$ ]] && break
    echo "Csak kisbetű, szám és _ lehet (pl. pi4b, nas_pi)."
  done
  if [ "$NEW_ID" != "$CUR_ID" ]; then
    # Új előtag = új entitások: a régi id-jű HA-entitásokat (discovery) előbb eltávolítjuk.
    if [ -f /var/lib/rpi-mqtt-monitor/discovery.json ]; then
      echo "Az előtag megváltozott ($CUR_ID -> $NEW_ID): a régi entitások törlése a HA-ból..."
      systemctl stop rpi-mqtt-monitor.service 2>/dev/null || true
      STATE_DIRECTORY=/var/lib/rpi-mqtt-monitor python3 "$APP_DIR/rpi_mqtt_monitor.py" --config "$CONF" --remove-discovery || true
      rm -f /var/lib/rpi-mqtt-monitor/discovery.json
    fi
    set_value device id "$NEW_ID"
  fi
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
