# custom-rpi-mqtt-monitor

Kis Python-szolgáltatás a Raspberry Pi-re (elsősorban Pi 4B; Raspberry Pi OS / Raspbian Buster-től, Python 3.7+):
a Pi állapotát MQTT-n küldi a Home Assistantnak **MQTT Discovery**-vel, így a Pi magától
megjelenik eszközként, szenzorokkal és gombokkal. Weboldal, nyitott port és bejelentkezés nem kell:
a Pi csak kifelé csatlakozik a brokerhez.

Függőség: `python3`, `python3-paho-mqtt` (1.4-től 2.x-ig jó), Python 3.11 alatt `python3-toml`,
opcionálisan `smartmontools` (a SMART-adatokhoz 7.0+ kell, mert JSON-kimenetet használ; Busteren 6.6 van,
ott a SMART-entitások egyszerűen nem jelennek meg).
Minden mást közvetlenül a `/proc`, `/sys`, `systemctl`, `journalctl` és a Docker Engine API
(unix socket) ad.

## Mit lát a HA?

**Fő eszköz** („Raspberry Pi 4B”, entitás-előtag: `pi4b_`):

| Entitás | Megjegyzés |
|---|---|
| CPU hőmérséklet, CPU-terhelés, CPU-frekvencia, Load (1/5/15 perc) | 30 mp-enként |
| Memória-használat (attribútum: MB), Swap-használat | |
| Hálózat le / fel (kB/s), IP-cím, WiFi-jel (ha WiFi-n van) | |
| Bekapcsolás ideje | timestamp |
| Alulfeszültség, Fojtás (hő/frekvencia), Fojtás vagy alulfeszültség volt indulás óta, Throttled-kód | `get_throttled` bitjei — a Pi tápellátásának legfontosabb jelzője |
| `<SSD/HDD/SD-kártya> <csatolási pont>` telítettség (%) és szabad hely (GB) | minden valós csatolási pont, 5 percenként |
| `<SSD/HDD> (sdX)` hőmérséklet, SMART-hiba | `smartctl`; alvó HDD-t nem ébreszt fel |
| Frissíthető csomagok (attribútum: biztonsági frissítések száma, csomaglista), Újraindítás szükséges | óránként |
| Docker image-ek (attribútum: gazdátlan, nem használt), Docker image-ek mérete, Futó konténerek | |
| Kernel, Monitor verzió | diagnosztika |
| Utolsó parancs | minden HA-ból kért művelet és az eredménye (audit) |
| Gombok: **Újraindítás**, **Leállítás**, Adatok frissítése | |

**Szolgáltatásonként** külön (al)eszköz, pl. „Raspberry Pi 4B · cloudflared”:
Fut, Állapot (attribútumok: leírás, mióta, újraindítások száma, enabled), **Napló** (utolsó sor;
az utolsó 40 sor a `lines` attribútumban), Napló frissítése, **Újraindítás**.
Alapból figyelt unitok: `ssh`, `docker`, `containerd`, `cloudflared*`, `cockpit.socket` (configban bővíthető, glob is lehet).

**Docker-konténerenként** külön (al)eszköz, pl. „Raspberry Pi 4B · Docker cloudflared”:
Fut, Állapot (healthcheck-kel; attribútumok: image, indulás, restart count, exit code, compose projekt),
**Napló**, Napló frissítése, **Újraindítás**, **Image** frissítés-entitás. Az Image entitás jelzi,
ha a registryben újabb image van, mint amin a konténer fut. Telepíteni nem tud: a frissítés
`docker compose pull && docker compose up -d` marad. Az új vagy törölt konténerek maguktól
megjelennek, illetve eltűnnek.

A napló attribútumokat a HA-ban pl. egy markdown-kártya mutathatja:

```yaml
type: markdown
content: >
  {% for l in state_attr('sensor.pi4b_svc_cloudflared_naplo', 'lines') or [] %}`{{ l }}`<br>{% endfor %}
```

## Biztonság

- **Nincs bejövő port.** A Pi csak a brokerhez csatlakozik, saját MQTT-felhasználóval.
- **Csak fix parancsok.** Az ügynök kizárólag ezeket ismeri: újraindítás, leállítás,
  a *configban felsorolt* unitok újraindítása, konténer-újraindítás és napló lekérése.
  Tetszőleges parancsot nem futtat, payloadból nem épít parancssort.
- **Szűk jogosultság.** Külön `rpimon` rendszerfelhasználóval fut. A sudo csak a konkrét parancsokat
  engedi (`systemctl reboot` / `poweroff` / `restart <a configban felsorolt unit>` és `smartctl` olvasás).
  Ezeket a telepítő generálja: `/etc/sudoers.d/rpi-mqtt-monitor`.
- **Elavult parancs ellen.** A megőrzött (retained) parancsokat eldobja, így egy beragadt üzenet nem indítja
  újra a Pi-t újracsatlakozáskor. Indulás után 60 mp-ig nem fogad újraindítást vagy leállítást
  (nincs reboot-hurok), és ugyanaz a parancs legfeljebb 30 mp-enként fut le.
- **Docker-csoport.** Az `rpimon` a `docker` csoport tagja, mert a konténerállapothoz és -újraindításhoz
  kell. Ez a gyakorlatban root-egyenértékű jog, ezért a config-fájl (MQTT-jelszó) csak root és `rpimon`
  számára olvasható. Ha Docker nem kell: `[docker] enabled = false`, és vedd ki az `rpimon`-t a csoportból.
- **Broker-oldali korlát (opcionális, ajánlott).** Mosquitto ACL-lel a `pi4b` user csak a saját témáit érje el.
  Lásd lent.

## Telepítés

### 1. MQTT-felhasználó a HA-ban

HA → Beállítások → Bővítmények → **Mosquitto broker** → Konfiguráció → `logins`:

```yaml
- username: pi4b
  password: <hosszú véletlen jelszó>
```

Mentés, majd a bővítmény újraindítása.

### 2. Telepítés a Pi-n

```bash
git clone https://github.com/patyiandras/custom-rpi-mqtt-monitor.git
cd custom-rpi-mqtt-monitor
sudo ./install.sh
```

A telepítő bekéri a broker címét, az MQTT-felhasználót és a jelszót. Újrafuttatáskor Enterre a meglévő érték
marad. A jelszót próba-bejelentkezéssel ellenőrzi, és csak akkor megy tovább, ha a broker elfogadta. A többi
beállítás (pl. figyelt szolgáltatások) a `/etc/rpi-mqtt-monitor/config.toml`-ban van; módosítás után
`sudo ./install.sh`. Bejelentkezés-próba külön: `sudo -u rpimon python3 /opt/rpi-mqtt-monitor/rpi_mqtt_monitor.py --test-mqtt`.

Utána a HA-ban: Beállítások → Eszközök és szolgáltatások → MQTT → **Raspberry Pi 4B**.

- **Próba MQTT nélkül:** `sudo -u rpimon python3 /opt/rpi-mqtt-monitor/rpi_mqtt_monitor.py --dump`.
  Kiírja az összes összegyűjtött adatot.
- **Napló:** `journalctl -u rpi-mqtt-monitor -f`.
- **Frissítés:** `git pull && sudo ./install.sh`.
- **Config-módosítás után** (pl. új figyelt unit) is `sudo ./install.sh`, mert ez generálja újra
  a sudo-jogokat.
- **Eltávolítás:** `sudo ./install.sh --uninstall`. A HA-entitásokat is törli.

### 3. (Opcionális) Mosquitto ACL

A Mosquitto bővítményben `customize: active: true, folder: mosquitto`. Ezután a `/share/mosquitto/` mappába
két fájl kell:

`acl.conf`:
```
acl_file /share/mosquitto/accesscontrollist
```

`accesscontrollist`:
```
user pi4b
topic readwrite rpi-mqtt-monitor/pi4b/#
topic write homeassistant/+/pi4b/#
topic read homeassistant/status

# FIGYELEM: ACL-fájl mellett MINDEN más felhasználót is fel kell sorolni, különben elveszítik
# a hozzáférést: a HA-t (homeassistant), a bővítményeket (addons), a SmartThings-hidat, Shellyt, ...
user homeassistant
topic readwrite #

user addons
topic readwrite #

user <masik_user>
topic readwrite #
```

## HA-gombok megerősítéssel

Az Újraindítás és a Leállítás gombot dashboardon érdemes megerősítéssel kitenni:

```yaml
type: button
entity: button.pi4b_ujrainditas
tap_action:
  action: perform-action
  perform_action: button.press
  target: { entity_id: button.pi4b_ujrainditas }
  confirmation: { text: "Biztosan újraindítod a Pi-t?" }
```

## MQTT-témák

| Téma | Tartalom |
|---|---|
| `rpi-mqtt-monitor/pi4b/status` | `online` / `offline` (LWT, retained) |
| `rpi-mqtt-monitor/pi4b/system`, `.../system_slow` | rendszeradatok JSON-ban |
| `rpi-mqtt-monitor/pi4b/disk/<mount>`, `.../smart/<sdX>` | lemezek |
| `rpi-mqtt-monitor/pi4b/service/<unit>[/log]` | szolgáltatás-állapot és -napló |
| `rpi-mqtt-monitor/pi4b/container/<név>[/log\|/update]` | konténer-állapot, -napló és image-frissítés |
| `rpi-mqtt-monitor/pi4b/last_command` | az utolsó parancs eredménye |
| `rpi-mqtt-monitor/pi4b/cmd/reboot\|shutdown\|refresh` | parancsok (nem retained!) |
| `rpi-mqtt-monitor/pi4b/cmd/service/<unit>/restart\|log` | |
| `rpi-mqtt-monitor/pi4b/cmd/container/<név>/restart\|log` | |

## Fejlesztés

`python -m unittest discover -s tests`: füstteszt hamis `/proc`-adatokkal, Linux nélkül is fut
(a discovery-t és a value_template-eket ellenőrzi; jinja2 kell hozzá).
