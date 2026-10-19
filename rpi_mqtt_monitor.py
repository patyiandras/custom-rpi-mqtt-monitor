#!/usr/bin/env python3
"""custom-rpi-mqtt-monitor: Raspberry Pi állapot MQTT-n a Home Assistantnak (MQTT Discovery).

Futtatás:      rpi_mqtt_monitor.py --config /etc/rpi-mqtt-monitor/config.toml
Próba (MQTT nélkül, kiírja az összegyűjtött adatokat):  ... --dump
A telepítő használja:  --print-units (a figyelt systemd unitok), --remove-discovery (eltávolításkor)

Csak a standard könyvtárat és a paho-mqtt-t használja (Debian: python3-paho-mqtt, 1.4-től 2.x-ig jó).
Python 3.7+ (Raspbian Buster is); 3.11 alatt a TOML-hoz python3-toml (vagy tomli) kell.
"""
import argparse
import http.client
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:  # Debian 10/11: python3-toml
        tomllib = None
        import toml

VERSION = "0.1.3"
REPO_URL = "https://github.com/patyiandras/custom-rpi-mqtt-monitor"
ORIGIN = {"name": "custom-rpi-mqtt-monitor", "sw_version": VERSION, "support_url": REPO_URL}

log = logging.getLogger("rpi-mqtt-monitor")

DEFAULT_CONFIG = {
    "mqtt": {
        "host": "localhost", "port": 1883, "username": "", "password": "",
        "tls": False, "tls_insecure": False, "ca_certs": "",
        "discovery_prefix": "homeassistant", "base_topic": "rpi-mqtt-monitor", "keepalive": 60,
    },
    "device": {"id": "pi4b", "name": "Raspberry Pi 4B"},
    "intervals": {"fast": 30, "slow": 300, "updates": 3600},
    "services": {"units": ["ssh", "docker", "cloudflared*"], "restart": True},
    "docker": {
        "enabled": True, "socket": "/var/run/docker.sock", "restart": True,
        "exclude": [], "image_update_check": True,
    },
    "disks": {"mounts": [], "exclude_mounts": ["/boot", "/boot/firmware"], "smart": True, "labels": {}},
    "system": {"allow_reboot": True, "allow_shutdown": True, "command_grace": 60, "command_cooldown": 30},
    "logs": {"lines": 40},
}

SERVICE_STATES = {
    "active": "fut", "inactive": "leállítva", "failed": "hiba", "activating": "indul",
    "deactivating": "leáll", "reloading": "újratölt",
}
CONTAINER_STATES = {
    "running": "fut", "exited": "leállt", "restarting": "újraindul", "paused": "szüneteltetve",
    "created": "létrehozva", "dead": "halott", "removing": "törlés alatt",
}
HEALTH_STATES = {"healthy": "egészséges", "unhealthy": "hibás", "starting": "indul"}
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ---------------------------------------------------------------------------
#  Segédfüggvények
# ---------------------------------------------------------------------------
def load_config(path):
    if tomllib:
        with open(path, "rb") as f:
            user = tomllib.load(f)
    else:
        with open(path, encoding="utf-8") as f:
            user = toml.load(f)
    cfg = {section: dict(values) for section, values in DEFAULT_CONFIG.items()}
    for section, values in user.items():
        if section not in cfg or not isinstance(values, dict):
            raise SystemExit(f"[HIBA] Ismeretlen config-szakasz: [{section}]")
        for key, value in values.items():
            if key not in cfg[section]:
                raise SystemExit(f"[HIBA] Ismeretlen config-kulcs: [{section}] {key}")
            cfg[section][key] = value
    if not re.fullmatch(r"[a-z0-9_]+", cfg["device"]["id"]):
        raise SystemExit("[HIBA] device.id: csak kisbetű, szám és _ lehet")
    return cfg


def read(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return default


def run(cmd, timeout=30):
    """(returncode, stdout, stderr) — soha nem dob kivételt."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def unit_name(unit):
    return unit[:-len(".service")] if unit.endswith(".service") else unit


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "x"


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat(timespec="seconds")


def log_payload(lines, n):
    lines = [ANSI_RE.sub("", line).rstrip()[:300] for line in lines if line.strip()][-n:]
    return {"last": lines[-1][:250] if lines else "(üres)", "lines": lines, "updated": iso()}


def binary(expr):
    return "{{ 'ON' if value_json.%s else 'OFF' }}" % expr


# ---------------------------------------------------------------------------
#  Rendszeradatok (/proc, /sys)
# ---------------------------------------------------------------------------
class System:
    THROTTLED_SYSFS = "/sys/devices/platform/soc/soc:firmware/get_throttled"

    def __init__(self, broker_host):
        self.broker_host = broker_host
        self.prev_cpu = None
        self.prev_net = None
        self.cpu_usage()  # alapérték a következő méréshez
        self.has_throttled = self.throttled() is not None
        self.has_wifi = self.wifi_signal() is not None

    def device_info(self):
        os_release = dict(
            line.split("=", 1) for line in (read("/etc/os-release", "") or "").splitlines() if "=" in line
        )
        return {
            "model": read("/proc/device-tree/model") or "Raspberry Pi",
            "serial": read("/proc/device-tree/serial-number"),
            "os": os_release.get("PRETTY_NAME", "Linux").strip('"'),
            "kernel": os.uname().release,
        }

    def cpu_usage(self):
        line = (read("/proc/stat", "") or "").splitlines()[0]
        vals = [int(v) for v in line.split()[1:9]]
        idle, total = vals[3] + vals[4], sum(vals)
        prev, self.prev_cpu = self.prev_cpu, (idle, total)
        if not prev or total <= prev[1]:
            return None
        return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)

    @staticmethod
    def meminfo():
        info = {}
        for line in (read("/proc/meminfo", "") or "").splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) if rest.split() else 0
        return info

    @staticmethod
    def boot_time():
        for line in (read("/proc/stat", "") or "").splitlines():
            if line.startswith("btime "):
                return iso(int(line.split()[1]))
        return None

    @staticmethod
    def default_iface():
        for line in (read("/proc/net/route", "") or "").splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return f[0]
        return None

    def net_rates(self, iface):
        for line in (read("/proc/net/dev", "") or "").splitlines()[2:]:
            name, _, data = line.partition(":")
            if name.strip() == iface:
                d = data.split()
                rx, tx = int(d[0]), int(d[8])
                break
        else:
            return None, None
        now = time.monotonic()
        prev, self.prev_net = self.prev_net, (iface, rx, tx, now)
        if not prev or prev[0] != iface or now <= prev[3]:
            return None, None
        dt = now - prev[3]
        return round(max(0, rx - prev[1]) / dt / 1024, 1), round(max(0, tx - prev[2]) / dt / 1024, 1)

    def ip_address(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((self.broker_host, 1))
                return s.getsockname()[0]
        except OSError:
            return None

    @staticmethod
    def mac_address(iface):
        return read(f"/sys/class/net/{iface}/address") if iface else None

    @staticmethod
    def wifi_signal():
        for line in (read("/proc/net/wireless", "") or "").splitlines()[2:]:
            f = line.split()
            if len(f) > 3:
                try:
                    return float(f[3].rstrip("."))
                except ValueError:
                    pass
        return None

    def throttled(self):
        raw = read(self.THROTTLED_SYSFS)
        if raw is None and shutil.which("vcgencmd"):
            rc, out, _ = run(["vcgencmd", "get_throttled"])
            m = re.search(r"0x[0-9a-fA-F]+", out)
            raw = m.group(0) if rc == 0 and m else None
        try:
            return int(raw, 16) if raw is not None else None
        except ValueError:
            return None

    def collect_fast(self):
        mem = self.meminfo()
        mem_total, mem_avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        swap_total, swap_free = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
        temp = read("/sys/class/thermal/thermal_zone0/temp")
        freq = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        iface = self.default_iface()
        rx, tx = self.net_rates(iface) if iface else (None, None)
        load = os.getloadavg()
        data = {
            "cpu_temp": round(int(temp) / 1000, 1) if temp else None,
            "cpu_usage": self.cpu_usage(),
            "cpu_freq": round(int(freq) / 1000) if freq else None,
            "load_1": round(load[0], 2), "load_5": round(load[1], 2), "load_15": round(load[2], 2),
            "mem_usage": round(100 * (mem_total - mem_avail) / mem_total, 1) if mem_total else None,
            "mem_used_mb": round((mem_total - mem_avail) / 1024), "mem_total_mb": round(mem_total / 1024),
            "swap_usage": round(100 * (swap_total - swap_free) / swap_total, 1) if swap_total else 0,
            "net_rx": rx, "net_tx": tx, "iface": iface, "ip": self.ip_address(),
            "boot_time": self.boot_time(),
        }
        if self.has_wifi:
            data["wifi_signal"] = self.wifi_signal()
        if self.has_throttled:
            t = self.throttled() or 0
            data.update({
                "throttled": hex(t),
                "undervoltage": bool(t & 0x1),
                "throttling": bool(t & 0xE),
                "throttled_since_boot": bool(t & 0xF0000),
            })
        return data

    @staticmethod
    def collect_updates():
        rc, out, _ = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=180)
        if rc != 0:
            return None
        inst = [line for line in out.splitlines() if line.startswith("Inst ")]
        return {
            "updates": len(inst),
            "updates_security": sum(1 for line in inst if "-security" in line),
            "updates_list": [line.split()[1] for line in inst][:50],
        }


# ---------------------------------------------------------------------------
#  Lemezek: telítettség + SMART
# ---------------------------------------------------------------------------
class Disks:
    def __init__(self, cfg):
        self.cfg = cfg
        self.smartctl = shutil.which("smartctl") if cfg["smart"] else None
        self.smart_cache = {}

    @staticmethod
    def parent_disk(dev, mnt=None):
        sys_path = None
        if mnt:  # a /dev/root-hoz nincs eszközfájl: a csatolási pont major:minor számából keressük
            try:
                st_dev = os.stat(mnt).st_dev
                sys_path = os.path.realpath(f"/sys/dev/block/{os.major(st_dev)}:{os.minor(st_dev)}")
            except OSError:
                sys_path = None
        if not sys_path or not os.path.exists(sys_path):
            name = os.path.basename(os.path.realpath(dev))
            sys_path = os.path.realpath(f"/sys/class/block/{name}")
        name = os.path.basename(sys_path)
        if os.path.exists(os.path.join(sys_path, "partition")):
            return os.path.basename(os.path.dirname(sys_path))
        return name

    def kind(self, disk):
        if disk in self.cfg["labels"]:
            return self.cfg["labels"][disk]
        if disk.startswith("mmcblk"):
            return "SD-kártya"
        rotation = (self.smart_cache.get(disk) or {}).get("rotation_rate")
        if rotation is not None:
            return "SSD" if rotation == 0 else "HDD"
        rot = read(f"/sys/block/{disk}/queue/rotational")
        return {"1": "HDD", "0": "SSD"}.get(rot, "Lemez")

    def mounts(self):
        seen, result = set(), []
        wanted = self.cfg["mounts"]
        for line in (read("/proc/mounts", "") or "").splitlines():
            dev, mnt, fstype = line.split()[:3]
            mnt = mnt.replace("\\040", " ")
            if not dev.startswith("/dev/") or dev.startswith("/dev/loop"):
                continue
            if wanted and mnt not in wanted:
                continue
            if not wanted and mnt in self.cfg["exclude_mounts"]:
                continue
            if dev in seen:
                continue
            seen.add(dev)
            result.append((dev, mnt, fstype))
        return result

    def smart(self, disk):
        if not self.smartctl or not disk.startswith("sd"):
            return None
        for extra in ([], ["-d", "sat"]):
            rc, out, _ = run(["sudo", "-n", self.smartctl, "-n", "standby", "-j", "-H", "-A", *extra, f"/dev/{disk}"])
            if re.search(r"STANDBY|SLEEP", out):
                return {"standby": True}  # alvó HDD-t nem ébresztünk fel
            try:
                j = json.loads(out)
            except ValueError:
                continue
            if "smart_status" in j or "temperature" in j:
                return {
                    "passed": j.get("smart_status", {}).get("passed"),
                    "temperature": j.get("temperature", {}).get("current"),
                    "power_on_hours": j.get("power_on_time", {}).get("hours"),
                    "model": j.get("model_name"),
                    "rotation_rate": j.get("rotation_rate"),
                    "standby": False,
                }
        return None

    def collect(self):
        disks = {}
        for dev in sorted({self.parent_disk(d, m) for d, m, _ in self.mounts()}):
            info = self.smart(dev)
            if info and info.get("standby") and dev in self.smart_cache:
                info = dict(self.smart_cache[dev], standby=True)
            elif not info and dev in self.smart_cache:
                info = self.smart_cache[dev]  # átmeneti hiba: a régi érték marad, az entitás nem tűnik el
            if info and len(info) > 1:
                self.smart_cache[dev] = info
                disks[dev] = dict(info, kind=self.kind(dev))
        mounts = {}
        for dev, mnt, fstype in self.mounts():
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            disk = self.parent_disk(dev, mnt)
            mounts["root" if mnt == "/" else slug(mnt)] = {
                "mount": mnt, "device": dev, "disk": disk, "fstype": fstype, "kind": self.kind(disk),
                "usage": round(100 * used / (used + avail), 1) if used + avail else 0,
                "total_gb": round(total / 1e9, 1), "used_gb": round(used / 1e9, 1), "free_gb": round(avail / 1e9, 1),
            }
        return mounts, disks


# ---------------------------------------------------------------------------
#  systemd szolgáltatások
# ---------------------------------------------------------------------------
class Services:
    PROPS = "Id,Description,LoadState,ActiveState,SubState,ActiveEnterTimestamp,NRestarts,UnitFileState,MainPID"

    def __init__(self, cfg):
        self.patterns = cfg["units"]
        self.systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"

    def expand(self):
        units = []
        for pattern in self.patterns:
            if not re.search(r"\.(service|socket|timer|mount|path|target)$", pattern):
                pattern += ".service"
            found = []
            for cmd in (["list-units", "--all"], ["list-unit-files"]):
                rc, out, _ = run([self.systemctl, *cmd, "--no-legend", "--plain", "--no-pager", pattern])
                for line in out.splitlines():
                    name = line.split()[0] if line.split() else ""
                    if name and not re.search(r"@\.\w+$", name) and name not in found:
                        found.append(name)
            units += [u for u in found if u not in units]
        return units

    def status(self, unit):
        rc, out, _ = run([self.systemctl, "show", "--timestamp=unix", f"--property={self.PROPS}", unit])
        if rc != 0:
            rc, out, _ = run([self.systemctl, "show", f"--property={self.PROPS}", unit])
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("LoadState") in (None, "not-found"):
            return None
        active, sub = props.get("ActiveState", ""), props.get("SubState", "")
        text = SERVICE_STATES.get(active, active)
        if active == "active" and sub not in ("running", "exited", ""):
            text = f"{text} ({sub})"
        since = props.get("ActiveEnterTimestamp", "")
        if since.startswith("@"):
            since = iso(int(since[1:]))
        return {
            "unit": unit, "description": props.get("Description"), "active": active, "sub": sub,
            "state_text": text, "since": since or None, "restarts": int(props.get("NRestarts") or 0),
            "enabled": props.get("UnitFileState"), "pid": int(props.get("MainPID") or 0),
        }

    @staticmethod
    def log_command(unit, n):
        journalctl = shutil.which("journalctl") or "/bin/journalctl"
        return [journalctl, "-u", unit, "-n", str(n), "--no-pager", "-q", "-o", "short-iso"]

    def logs(self, unit, n):
        cmd = self.log_command(unit, n)
        rc, out, err = run(cmd)
        if rc != 0 or "insufficient permissions" in err or "not seeing messages" in err:
            rc2, out2, err2 = run(["sudo", "-n", *cmd])  # a telepítő unitonként engedélyezi
            if rc2 == 0:
                rc, out, err = rc2, out2, err2
        return log_payload(out.splitlines() if rc == 0 else [f"journalctl hiba: {err.strip()}"], n)

    def restart(self, unit):
        rc, out, err = run(["sudo", "-n", self.systemctl, "restart", unit], timeout=120)
        return rc == 0, (err or out).strip()


# ---------------------------------------------------------------------------
#  Docker (Engine API a unix socketen, külső csomag nélkül)
# ---------------------------------------------------------------------------
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


class DockerError(Exception):
    pass


class Docker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfg["socket"]

    def available(self):
        return self.cfg["enabled"] and os.path.exists(self.path)

    def call(self, method, path, timeout=30):
        conn = UnixHTTPConnection(self.path, timeout)
        try:
            conn.request(method, path, headers={"Host": "docker"})
            resp = conn.getresponse()
            return resp.status, resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise DockerError(f"{path}: {e}") from e
        finally:
            conn.close()

    def get(self, path, timeout=30):
        status, body = self.call("GET", path, timeout)
        if status != 200:
            raise DockerError(f"{path}: HTTP {status} {body[:200]!r}")
        return json.loads(body)

    def registry_image(self, image_id):
        """Registryből húzott-e az image (van RepoDigest)? Helyben épített image-hez nincs frissítés-ellenőrzés."""
        cache = self.__dict__.setdefault("_registry_cache", {})
        if image_id not in cache:
            img = self.get(f"/images/{urllib.parse.quote(image_id, safe=':')}/json")
            cache[image_id] = any("@" in d for d in img.get("RepoDigests") or [])
        return cache[image_id]

    def containers(self):
        result = {}
        for c in self.get("/containers/json?all=1"):
            name = c["Names"][0].lstrip("/")
            if name in self.cfg["exclude"]:
                continue
            d = self.get(f"/containers/{c['Id']}/json")
            st = d["State"]
            health = (st.get("Health") or {}).get("Status")
            text = CONTAINER_STATES.get(st["Status"], st["Status"])
            if health:
                text += f" ({HEALTH_STATES.get(health, health)})"
            elif st["Status"] == "exited":
                text += f" ({st.get('ExitCode')})"
            labels = d["Config"].get("Labels") or {}
            result[slug(name)] = {
                "name": name, "id": c["Id"][:12], "full_id": c["Id"], "image": d["Config"]["Image"],
                "image_id": d["Image"], "status": st["Status"], "running": bool(st.get("Running")),
                "health": health, "state_text": text, "status_text": c.get("Status"),
                "restart_count": d.get("RestartCount", 0), "started_at": st.get("StartedAt"),
                "finished_at": st.get("FinishedAt"), "exit_code": st.get("ExitCode"),
                "tty": bool(d["Config"].get("Tty")), "compose_project": labels.get("com.docker.compose.project"),
                "registry_image": self.registry_image(d["Image"]),
            }
        return result

    def images(self, containers):
        imgs = self.get("/images/json")
        used = {c["image_id"] for c in containers.values()}
        return {
            "images_count": len(imgs),
            "images_dangling": sum(1 for i in imgs if not i.get("RepoTags") or i["RepoTags"] == ["<none>:<none>"]),
            "images_unused": sum(1 for i in imgs if i["Id"] not in used),
            "images_size_gb": round(sum(i.get("Size", 0) for i in imgs) / 1e9, 2),
        }

    def image_update(self, ct):
        """Fut-e a konténer a registry-ben lévő legfrissebb image-en (digest-összevetés)."""
        ref = ct["image"]
        if ref.startswith("sha256:") or "@" in ref:
            return None
        if ":" not in ref.rsplit("/", 1)[-1]:
            ref += ":latest"
        img = self.get(f"/images/{urllib.parse.quote(ct['image_id'], safe=':')}/json")
        local = [d.split("@", 1)[1] for d in img.get("RepoDigests") or [] if "@" in d]
        if not local:
            return None  # helyben épített image
        remote = self.get(f"/distribution/{urllib.parse.quote(ref, safe='/:')}/json", timeout=60)["Descriptor"]["digest"]
        installed = local[0][7:19]
        info = {"installed_version": installed, "latest_version": installed, "title": ref}
        if remote not in local:
            info["latest_version"] = remote[7:19]
            info["release_summary"] = "Új image a registry-ben. Frissítés: docker compose pull && docker compose up -d"
        return info

    def logs(self, ct, n):
        status, body = self.call("GET", f"/containers/{ct['full_id']}/logs?stdout=1&stderr=1&timestamps=1&tail={n}")
        if status != 200:
            return log_payload([f"docker logs hiba: HTTP {status}"], n)
        if not ct["tty"]:
            chunks, i = [], 0
            while i + 8 <= len(body):
                size = int.from_bytes(body[i + 4:i + 8], "big")
                chunks.append(body[i + 8:i + 8 + size])
                i += 8 + size
            body = b"".join(chunks)
        lines = [re.sub(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)\.\d+Z", r"\1 \2", line)
                 for line in body.decode("utf-8", "replace").splitlines()]
        return log_payload(lines, n)

    def restart(self, ct):
        status, body = self.call("POST", f"/containers/{ct['full_id']}/restart?t=10", timeout=90)
        return status == 204, body.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
#  Az ügynök: gyűjtés, discovery, parancsok
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        m = cfg["mqtt"]
        self.dev_id = cfg["device"]["id"]
        self.base = f"{m['base_topic']}/{self.dev_id}"
        self.disc_prefix = m["discovery_prefix"]
        self.avail_topic = f"{self.base}/status"
        self.lines = int(cfg["logs"]["lines"])
        self.system = System(m["host"])
        self.disks = Disks(cfg["disks"])
        self.services = Services(cfg["services"])
        self.docker = Docker(cfg["docker"])
        self.info = self.system.device_info()
        self.started = time.monotonic()
        self.queue = queue.Queue()
        self.running = True
        self.connected = False
        self.client = None if dry_run else self._mqtt_client()
        # állapot
        self.units = []                # figyelt unitok
        self.service_data = {}         # key -> status
        self.container_data = {}       # key -> info
        self.disk_data = ({}, {})
        self.groups = {}               # discovery-csoport -> {topic: config}
        self.published_discovery = {}  # topic -> json
        self.last_payload = {}         # állapot-topic -> json (változás-figyelés)
        self.last_cmd = {}
        self.last_slow_extra = {"updates": None, "updates_security": None, "updates_list": []}
        self.cleaned = set()
        self.state_file = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/rpi-mqtt-monitor"),
                                       "discovery.json")
        try:
            with open(self.state_file) as f:
                self.previous_groups = json.load(f)  # előző futás discovery-topicjai csoportonként
        except (OSError, ValueError):
            self.previous_groups = {}

    # --- MQTT ----------------------------------------------------------------
    def _mqtt_client(self):
        import paho.mqtt.client as mqtt
        m = self.cfg["mqtt"]
        client_id = f"rpi-mqtt-monitor-{self.dev_id}"
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=client_id, clean_session=True)
        if m["username"]:
            client.username_pw_set(m["username"], m["password"])
        if m["tls"]:
            client.tls_set(ca_certs=m["ca_certs"] or None)
            client.tls_insecure_set(bool(m["tls_insecure"]))
        client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        client.reconnect_delay_set(1, 60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, rc, *args):
        if getattr(rc, "is_failure", rc != 0):
            log.error("MQTT-csatlakozás sikertelen: %s", rc)
            return
        log.info("MQTT csatlakozva: %s:%s", self.cfg["mqtt"]["host"], self.cfg["mqtt"]["port"])
        self.connected = True
        client.subscribe(f"{self.base}/cmd/#", qos=1)
        client.subscribe(f"{self.disc_prefix}/status", qos=1)
        self.queue.put(("connected", None))

    def _on_disconnect(self, *args):
        self.connected = False
        log.warning("MQTT-kapcsolat megszakadt")

    def _on_message(self, client, userdata, msg):
        if msg.topic == f"{self.disc_prefix}/status":
            if msg.payload == b"online":
                self.queue.put(("connected", None))
            return
        if msg.retain:
            log.warning("Megőrzött (retained) parancs eldobva: %s", msg.topic)
            return
        self.queue.put(("cmd", msg.topic[len(self.base) + 5:].split("/")))

    def publish(self, topic, payload, retain=True, force=False):
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        if not force and self.last_payload.get(topic) == data:
            return
        self.last_payload[topic] = data
        if self.client:
            self.client.publish(topic, data, qos=1, retain=retain)
        else:
            print(topic, data)

    # --- Discovery -------------------------------------------------------------
    def device(self):
        dev = {
            "identifiers": [f"rpi_mqtt_monitor_{self.dev_id}"], "name": self.cfg["device"]["name"],
            "manufacturer": "Raspberry Pi Ltd", "model": self.info["model"], "sw_version": self.info["os"],
        }
        if self.info["serial"]:
            dev["serial_number"] = self.info["serial"]
        mac = self.system.mac_address(self.system.default_iface())
        if mac:
            dev["connections"] = [["mac", mac]]
        return dev

    def sub_device(self, kind, key, name, model):
        return {
            "identifiers": [f"rpi_mqtt_monitor_{self.dev_id}_{kind}_{key}"],
            "name": f"{self.cfg['device']['name']} · {name}", "model": model,
            "manufacturer": "Docker" if kind == "ct" else "systemd",
            "via_device": f"rpi_mqtt_monitor_{self.dev_id}",
        }

    def entity(self, group, component, key, name, device, **opts):
        uid = f"{self.dev_id}_{key}"
        config = {
            "name": name, "unique_id": uid, "default_entity_id": f"{component}.{uid}",
            "availability_topic": self.avail_topic, "device": device, "origin": ORIGIN,
        }
        config.update({k: v for k, v in opts.items() if v is not None})
        self.groups.setdefault(group, {})[f"{self.disc_prefix}/{component}/{self.dev_id}/{key}/config"] = config

    def button(self, group, key, name, device, command, **opts):
        self.entity(group, "button", key, name, device, command_topic=f"{self.base}/cmd/{command}",
                    payload_press="PRESS", retain=False, qos=1, **opts)

    def build_system_discovery(self):
        self.groups["system"] = {}
        dev, fast, slow = self.device(), f"{self.base}/system", f"{self.base}/system_slow"
        g = "system"

        def sensor(key, name, field, topic=fast, **opts):
            self.entity(g, "sensor", key, name, dev, state_topic=topic,
                        value_template="{{ value_json.%s }}" % field, **opts)

        meas = {"state_class": "measurement"}
        diag = {"entity_category": "diagnostic"}
        sensor("cpu_homerseklet", "CPU hőmérséklet", "cpu_temp", unit_of_measurement="°C",
               device_class="temperature", suggested_display_precision=1, **meas)
        sensor("cpu_terheles", "CPU-terhelés", "cpu_usage", unit_of_measurement="%", icon="mdi:cpu-64-bit", **meas)
        sensor("cpu_frekvencia", "CPU-frekvencia", "cpu_freq", unit_of_measurement="MHz",
               device_class="frequency", **meas)
        for n in (1, 5, 15):
            sensor(f"load_{n}", f"Load ({n} perc)", f"load_{n}", icon="mdi:gauge", **meas)
        sensor("memoria", "Memória-használat", "mem_usage", unit_of_measurement="%", icon="mdi:memory",
               json_attributes_topic=fast,
               json_attributes_template="{{ {'hasznalt_mb': value_json.mem_used_mb, "
                                        "'osszes_mb': value_json.mem_total_mb} | tojson }}", **meas)
        sensor("swap", "Swap-használat", "swap_usage", unit_of_measurement="%", icon="mdi:swap-horizontal", **meas)
        sensor("halozat_le", "Hálózat le", "net_rx", unit_of_measurement="kB/s", device_class="data_rate", **meas)
        sensor("halozat_fel", "Hálózat fel", "net_tx", unit_of_measurement="kB/s", device_class="data_rate", **meas)
        sensor("ip_cim", "IP-cím", "ip", icon="mdi:ip-network", **diag)
        sensor("bekapcsolva", "Bekapcsolás ideje", "boot_time", device_class="timestamp")
        if self.system.has_wifi:
            sensor("wifi_jel", "WiFi-jel", "wifi_signal", unit_of_measurement="dBm",
                   device_class="signal_strength", **meas, **diag)
        if self.system.has_throttled:
            sensor("throttled_kod", "Throttled-kód", "throttled", icon="mdi:raspberry-pi", **diag)
            for key, name, field, extra in (
                ("alulfeszultseg", "Alulfeszültség", "undervoltage", {}),
                ("korlatozas", "Fojtás (hő/frekvencia)", "throttling", {}),
                ("korlatozas_volt", "Fojtás vagy alulfeszültség volt indulás óta", "throttled_since_boot", diag),
            ):
                self.entity(g, "binary_sensor", key, name, dev, state_topic=fast, value_template=binary(field),
                            device_class="problem", **extra)
        sensor("frissitheto_csomagok", "Frissíthető csomagok", "updates", topic=slow, icon="mdi:package-up",
               json_attributes_topic=slow,
               json_attributes_template="{{ {'biztonsagi': value_json.updates_security, "
                                        "'csomagok': value_json.updates_list} | tojson }}", **meas)
        self.entity(g, "binary_sensor", "ujrainditas_kell", "Újraindítás szükséges", dev, state_topic=slow,
                    value_template=binary("reboot_required"), icon="mdi:restart-alert")
        sensor("kernel", "Kernel", "kernel", topic=slow, icon="mdi:linux", **diag)
        sensor("monitor_verzio", "Monitor verzió", "agent_version", topic=slow, icon="mdi:tag", **diag)
        if self.docker.available():
            sensor("docker_imagek", "Docker image-ek", "images_count", topic=slow, icon="mdi:docker",
                   json_attributes_topic=slow,
                   json_attributes_template="{{ {'gazdatlan': value_json.images_dangling, "
                                            "'nem_hasznalt': value_json.images_unused} | tojson }}", **meas)
            sensor("docker_image_meret", "Docker image-ek mérete", "images_size_gb", topic=slow,
                   unit_of_measurement="GB", device_class="data_size", **meas)
            sensor("docker_futo_kontenerek", "Futó konténerek", "containers_running", topic=slow,
                   icon="mdi:docker", **meas)
        self.entity(g, "sensor", "utolso_parancs", "Utolsó parancs", dev, state_topic=f"{self.base}/last_command",
                    value_template="{{ value_json.state }}", json_attributes_topic=f"{self.base}/last_command",
                    icon="mdi:console")
        sysc = self.cfg["system"]
        if sysc["allow_reboot"]:
            self.button(g, "ujrainditas", "Újraindítás", dev, "reboot", device_class="restart")
        if sysc["allow_shutdown"]:
            self.button(g, "leallitas", "Leállítás", dev, "shutdown", icon="mdi:power")
        self.button(g, "frissites", "Adatok frissítése", dev, "refresh", icon="mdi:refresh",
                    entity_category="diagnostic")

    def build_disk_discovery(self):
        self.groups["disks"] = {}
        dev = self.device()
        mounts, disks = self.disk_data
        for key, m in mounts.items():
            topic = f"{self.base}/disk/{key}"
            label = f"{m['kind']} {m['mount']}"
            self.entity("disks", "sensor", f"tarhely_{key}", f"{label} telítettség", dev, state_topic=topic,
                        value_template="{{ value_json.usage }}", unit_of_measurement="%", icon="mdi:harddisk",
                        state_class="measurement", json_attributes_topic=topic)
            self.entity("disks", "sensor", f"szabad_{key}", f"{label} szabad hely", dev, state_topic=topic,
                        value_template="{{ value_json.free_gb }}", unit_of_measurement="GB",
                        device_class="data_size", state_class="measurement")
        for disk, d in disks.items():
            topic = f"{self.base}/smart/{disk}"
            if d.get("temperature") is not None:
                self.entity("disks", "sensor", f"{disk}_homerseklet", f"{d['kind']} ({disk}) hőmérséklet", dev,
                            state_topic=topic, value_template="{{ value_json.temperature }}",
                            unit_of_measurement="°C", device_class="temperature", state_class="measurement")
            if d.get("passed") is not None:
                self.entity("disks", "binary_sensor", f"{disk}_smart", f"{d['kind']} ({disk}) SMART-hiba", dev,
                            state_topic=topic, value_template="{{ 'OFF' if value_json.passed else 'ON' }}",
                            device_class="problem", json_attributes_topic=topic)

    def build_unit_discovery(self, group, kind, items):
        self.groups[group] = {}
        restart_ok = self.cfg["services" if kind == "svc" else "docker"]["restart"]
        for key, item in items.items():
            name = unit_name(item["unit"]) if kind == "svc" else item["name"]
            model = (item.get("description") or "systemd unit") if kind == "svc" else item["image"]
            dev = self.sub_device(kind, key, name if kind == "svc" else f"Docker {name}", model)
            topic = f"{self.base}/{'service' if kind == 'svc' else 'container'}/{key}"
            cmd = f"{'service' if kind == 'svc' else 'container'}/{key}"
            pre = f"{kind}_{key}"
            running = "active == 'active'" if kind == "svc" else "running"
            self.entity(group, "binary_sensor", f"{pre}_fut", "Fut", dev, state_topic=topic,
                        value_template=binary(running), device_class="running")
            self.entity(group, "sensor", f"{pre}_allapot", "Állapot", dev, state_topic=topic,
                        value_template="{{ value_json.state_text }}", json_attributes_topic=topic,
                        icon="mdi:cog-outline" if kind == "svc" else "mdi:docker")
            self.entity(group, "sensor", f"{pre}_naplo", "Napló", dev, state_topic=f"{topic}/log",
                        value_template="{{ value_json.last }}", json_attributes_topic=f"{topic}/log",
                        icon="mdi:text-box-outline")
            self.button(group, f"{pre}_naplo_frissites", "Napló frissítése", dev, f"{cmd}/log",
                        icon="mdi:text-box-search-outline", entity_category="diagnostic")
            if restart_ok:
                self.button(group, f"{pre}_ujrainditas", "Újraindítás", dev, f"{cmd}/restart",
                            device_class="restart")
            if kind == "ct" and self.cfg["docker"]["image_update_check"] and item.get("registry_image"):
                self.entity(group, "update", f"{pre}_image", "Image", dev, state_topic=f"{topic}/update",
                            entity_category="diagnostic")

    def sync_discovery(self, force=False):
        desired = {t: json.dumps(c, ensure_ascii=False) for g in self.groups.values() for t, c in g.items()}
        if not self.client:
            return
        for topic, data in desired.items():
            if force or self.published_discovery.get(topic) != data:
                self.client.publish(topic, data, qos=1, retain=True)
        for topic in set(self.published_discovery) - set(desired):
            log.info("Discovery törölve: %s", topic)
            self.client.publish(topic, "", qos=1, retain=True)
        self.published_discovery = desired
        self._cleanup_previous_run()
        try:
            with open(self.state_file, "w") as f:
                # a még fel nem épült csoportoknál az előző futás listája marad, hogy később törölhető legyen
                json.dump(dict(self.previous_groups, **{g: list(c) for g, c in self.groups.items()}), f)
        except OSError as e:
            log.debug("Állapotfájl nem írható: %s", e)

    def _cleanup_previous_run(self):
        """Az előző futás óta eltűnt entitások (pl. törölt konténer) discovery-jét is törli."""
        for group, topics in self.previous_groups.items():
            if group in self.cleaned or group not in self.groups:
                continue
            self.cleaned.add(group)
            for topic in set(topics) - set(self.groups[group]):
                log.info("Régi discovery törölve: %s", topic)
                self.client.publish(topic, "", qos=1, retain=True)

    # --- Gyűjtés ---------------------------------------------------------------
    def collect_services(self, refresh_units=False):
        if refresh_units or not self.units:
            self.units = self.services.expand()
        data = {}
        for unit in self.units:
            st = self.services.status(unit)
            if st:
                data[slug(unit_name(unit))] = st
        changed = set(data) != set(self.service_data)
        for key, st in data.items():
            prev = self.service_data.get(key)
            self.publish(f"{self.base}/service/{key}", st)
            if not prev or prev["active"] != st["active"] or f"{self.base}/service/{key}/log" not in self.last_payload:
                self.publish(f"{self.base}/service/{key}/log", self.services.logs(st["unit"], self.lines))
        self.service_data = data
        if changed or "services" not in self.groups:
            self.build_unit_discovery("services", "svc", data)
            self.sync_discovery()

    def collect_containers(self):
        if not self.docker.available():
            return
        try:
            data = self.docker.containers()
            for key, ct in data.items():
                prev = self.container_data.get(key)
                self.publish(f"{self.base}/container/{key}", {k: v for k, v in ct.items() if k != "full_id"})
                if not prev or prev["status"] != ct["status"] or f"{self.base}/container/{key}/log" not in self.last_payload:
                    self.publish(f"{self.base}/container/{key}/log", self.docker.logs(ct, self.lines))
        except (DockerError, ValueError, KeyError) as e:
            log.warning("Docker-lekérdezés sikertelen: %s", e)
            return
        changed = set(data) != set(self.container_data)
        self.container_data = data
        if changed or "containers" not in self.groups:
            self.build_unit_discovery("containers", "ct", data)
            self.sync_discovery()

    def collect_fast(self):
        self.publish(f"{self.base}/system", self.system.collect_fast())
        self.collect_services()
        self.collect_containers()

    def collect_slow(self):
        mounts, disks = self.disk_data = self.disks.collect()
        for key, m in mounts.items():
            self.publish(f"{self.base}/disk/{key}", m)
        for disk, d in disks.items():
            self.publish(f"{self.base}/smart/{disk}", d)
        slow = dict(self.last_slow_extra)
        slow.update({
            "reboot_required": os.path.exists("/run/reboot-required") or os.path.exists("/var/run/reboot-required"),
            "kernel": self.info["kernel"], "os": self.info["os"], "agent_version": VERSION,
        })
        if self.docker.available():
            try:
                slow.update(self.docker.images(self.container_data))
            except (DockerError, ValueError, KeyError) as e:
                log.warning("Docker image-lista sikertelen: %s", e)
            slow["containers_running"] = sum(1 for c in self.container_data.values() if c["running"])
        self.publish(f"{self.base}/system_slow", slow)
        self.build_disk_discovery()
        self.collect_services(refresh_units=True)
        self.sync_discovery()

    def collect_updates(self):
        upd = self.system.collect_updates()
        if upd:
            self.last_slow_extra = upd
        if self.docker.available() and self.cfg["docker"]["image_update_check"]:
            for key, ct in self.container_data.items():
                try:
                    info = self.docker.image_update(ct)
                except (DockerError, ValueError, KeyError) as e:
                    log.info("Image-frissítés ellenőrzés kihagyva (%s): %s", ct["name"], e)
                    continue
                if info:
                    self.publish(f"{self.base}/container/{key}/update", info)

    # --- Parancsok -------------------------------------------------------------
    def report(self, command, ok, detail=""):
        state = f"{command} – {'ok' if ok else 'hiba'}"
        log.log(logging.INFO if ok else logging.ERROR, "Parancs: %s %s", state, detail)
        self.publish(f"{self.base}/last_command",
                     {"state": state[:250], "command": command, "ok": ok, "detail": detail[:500], "time": iso()},
                     force=True)

    def handle_command(self, parts):
        target = "/".join(parts)
        now = time.monotonic()
        if now - self.last_cmd.get(target, -1e9) < self.cfg["system"]["command_cooldown"]:
            log.warning("Parancs túl sűrűn, eldobva: %s", target)
            return
        self.last_cmd[target] = now
        sysc = self.cfg["system"]
        if parts in (["reboot"], ["shutdown"]):
            allowed = sysc["allow_reboot"] if parts[0] == "reboot" else sysc["allow_shutdown"]
            label = "Újraindítás" if parts[0] == "reboot" else "Leállítás"
            if not allowed:
                return self.report(label, False, "a config tiltja")
            if now - self.started < sysc["command_grace"]:
                return self.report(label, False, f"indulás utáni {sysc['command_grace']} mp-ben nem fogadom")
            self.report(label, True, "indul")
            self.publish(self.avail_topic, "offline", force=True)
            time.sleep(1)
            rc, out, err = run(["sudo", "-n", self.services.systemctl, "reboot" if parts[0] == "reboot" else "poweroff"])
            if rc != 0:
                self.publish(self.avail_topic, "online", force=True)
                self.report(label, False, (err or out).strip())
            return
        if parts == ["refresh"]:
            self.last_payload.clear()
            self.collect_fast()
            self.collect_slow()
            return self.report("Adatok frissítése", True)
        if len(parts) == 3 and parts[0] in ("service", "container") and parts[2] in ("restart", "log"):
            kind, key, action = parts
            if kind == "service":
                st = self.service_data.get(key)
                if not st:
                    return self.report(f"{key} {action}", False, "ismeretlen szolgáltatás")
                name = unit_name(st["unit"])
                if action == "log":
                    self.publish(f"{self.base}/service/{key}/log", self.services.logs(st["unit"], self.lines), force=True)
                    return
                if not self.cfg["services"]["restart"]:
                    return self.report(f"{name} újraindítás", False, "a config tiltja")
                ok, detail = self.services.restart(st["unit"])
                self.report(f"{name} újraindítás", ok, detail)
                time.sleep(2)
                self.collect_services()
                self.publish(f"{self.base}/service/{key}/log", self.services.logs(st["unit"], self.lines), force=True)
                return
            ct = self.container_data.get(key)
            if not ct:
                return self.report(f"{key} {action}", False, "ismeretlen konténer")
            try:
                if action == "log":
                    self.publish(f"{self.base}/container/{key}/log", self.docker.logs(ct, self.lines), force=True)
                    return
                if not self.cfg["docker"]["restart"]:
                    return self.report(f"Docker {ct['name']} újraindítás", False, "a config tiltja")
                ok, detail = self.docker.restart(ct)
                self.report(f"Docker {ct['name']} újraindítás", ok, detail)
                time.sleep(2)
                self.collect_containers()
                self.publish(f"{self.base}/container/{key}/log", self.docker.logs(ct, self.lines), force=True)
            except DockerError as e:
                self.report(f"Docker {ct['name']} {action}", False, str(e))
            return
        log.warning("Ismeretlen parancs: %s", target)

    # --- Fő ciklus -------------------------------------------------------------
    def on_connected(self):
        self.publish(self.avail_topic, "online", force=True)
        self.last_payload = {self.avail_topic: "online"}
        self.sync_discovery(force=True)
        self.next_fast = self.next_slow = 0

    def run(self):
        m = self.cfg["mqtt"]
        self.build_system_discovery()
        self.client.connect_async(m["host"], int(m["port"]), int(m["keepalive"]))
        self.client.loop_start()
        iv = self.cfg["intervals"]
        self.next_fast = self.next_slow = self.next_updates = 0
        try:
            while self.running:
                now = time.monotonic()
                if self.connected:
                    try:
                        if now >= self.next_fast:
                            self.next_fast = now + iv["fast"]
                            self.collect_fast()
                        if now >= self.next_slow:
                            self.next_slow = now + iv["slow"]
                            self.collect_slow()
                        if now >= self.next_updates:
                            self.next_updates = now + iv["updates"]
                            self.collect_updates()
                            self.next_slow = 0  # a frissítésszám a lassú témában megy ki
                    except Exception:  # egy hibás gyűjtés ne állítsa le az ügynököt
                        log.exception("Gyűjtési hiba")
                wait = max(0.5, min(self.next_fast, self.next_slow, self.next_updates) - time.monotonic())
                try:
                    event, arg = self.queue.get(timeout=wait)
                except queue.Empty:
                    continue
                try:
                    if event == "connected":
                        self.on_connected()
                    elif event == "cmd":
                        self.handle_command(arg)
                except Exception:
                    log.exception("Hiba az esemény feldolgozásában: %s %s", event, arg)
        finally:
            self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
            time.sleep(0.5)
            self.client.loop_stop()
            self.client.disconnect()

    def dump(self):
        self.build_system_discovery()
        time.sleep(1)
        out = {"device": self.device(), "system": self.system.collect_fast()}
        self.units = self.services.expand()
        out["units"] = self.units
        out["services"] = {u: self.services.status(u) for u in self.units}
        mounts, disks = self.disks.collect()
        out["mounts"], out["smart"] = mounts, disks
        out["updates"] = self.system.collect_updates()
        if self.docker.available():
            try:
                cts = self.docker.containers()
                out["containers"] = {k: {kk: vv for kk, vv in v.items() if kk != "full_id"} for k, v in cts.items()}
                out["images"] = self.docker.images(cts)
                out["image_updates"] = {}
                for k, ct in cts.items():
                    try:
                        out["image_updates"][k] = self.docker.image_update(ct)
                    except (DockerError, ValueError, KeyError) as e:
                        out["image_updates"][k] = f"hiba: {e}"
            except DockerError as e:
                out["docker_error"] = str(e)
        else:
            out["docker"] = "nem elérhető (nincs socket vagy kikapcsolva)"
        print(json.dumps(out, ensure_ascii=False, indent=2))

    def remove_discovery(self):
        try:
            with open(self.state_file) as f:
                topics = [t for ts in json.load(f).values() for t in ts]
        except (OSError, ValueError):
            print("Nincs discovery-állapotfájl, nincs mit törölni.")
            return
        m = self.cfg["mqtt"]
        self.client.connect(m["host"], int(m["port"]), int(m["keepalive"]))
        self.client.loop_start()
        for topic in topics + [self.avail_topic]:
            self.client.publish(topic, "", qos=1, retain=True).wait_for_publish()
        self.client.loop_stop()
        self.client.disconnect()
        print(f"{len(topics)} entitás discovery-je törölve.")


def sudo_commands(cfg):
    """A telepítő ezekre ad szűkített sudo-jogot (pontosan így hívja őket az ügynök)."""
    systemctl = shutil.which("systemctl") or "/bin/systemctl"
    cmds = [f"{systemctl} reboot", f"{systemctl} poweroff"]
    for unit in Services(cfg["services"]).expand():
        if cfg["services"]["restart"]:
            cmds.append(f"{systemctl} restart {unit}")
        cmds.append(" ".join(Services.log_command(unit, int(cfg["logs"]["lines"]))))
    smartctl = shutil.which("smartctl")
    if smartctl and cfg["disks"]["smart"]:
        cmds.append(f"{smartctl} -n standby -j -H -A /dev/sd[a-z]")
        cmds.append(f"{smartctl} -n standby -j -H -A -d sat /dev/sd[a-z]")
    return cmds


def test_mqtt(cfg):
    """Próba-csatlakozás a brokerhez (a telepítő ezzel ellenőrzi a jelszót). 0 = rendben."""
    import paho.mqtt.client as mqtt
    m = cfg["mqtt"]
    result = {}
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"rpi-mqtt-monitor-{cfg['device']['id']}-test")
    except AttributeError:
        client = mqtt.Client(client_id=f"rpi-mqtt-monitor-{cfg['device']['id']}-test")
    if m["username"]:
        client.username_pw_set(m["username"], m["password"])
    if m["tls"]:
        client.tls_set(ca_certs=m["ca_certs"] or None)
        client.tls_insecure_set(bool(m["tls_insecure"]))
    client.on_connect = lambda c, u, f, rc, *a: result.setdefault("rc", rc)
    try:
        client.connect(m["host"], int(m["port"]), 10)
    except (OSError, ValueError) as e:
        print(f"A broker nem érhető el ({m['host']}:{m['port']}): {e}")
        return 2
    client.loop_start()
    deadline = time.monotonic() + 10
    while "rc" not in result and time.monotonic() < deadline:
        time.sleep(0.1)
    client.loop_stop()
    client.disconnect()
    rc = result.get("rc")
    if rc is None:
        print("Nem jött válasz a brokertől 10 mp alatt.")
        return 2
    if getattr(rc, "is_failure", rc != 0):
        print(f"A broker elutasította a bejelentkezést: {rc}")
        return 1
    print(f"MQTT-bejelentkezés rendben ({m['username']}@{m['host']}:{m['port']}).")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi -> MQTT -> Home Assistant monitor")
    ap.add_argument("--config", default="/etc/rpi-mqtt-monitor/config.toml")
    ap.add_argument("--dump", action="store_true", help="adatok kiírása MQTT nélkül")
    ap.add_argument("--print-units", action="store_true", help="a figyelt systemd unitok listája")
    ap.add_argument("--remove-discovery", action="store_true", help="HA-entitások eltávolítása")
    ap.add_argument("--test-mqtt", action="store_true", help="próba-bejelentkezés a brokerhez")
    ap.add_argument("--print-sudo", action="store_true", help="a sudo-val futtatott parancsok (telepítőnek)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.print_sudo:
        print("\n".join(sudo_commands(cfg)))
        return
    if args.test_mqtt:
        return test_mqtt(cfg)
    if args.print_units:
        print("\n".join(Services(cfg["services"]).expand()))
        return
    monitor = Monitor(cfg, dry_run=args.dump)
    if args.dump:
        return monitor.dump()
    if args.remove_discovery:
        return monitor.remove_discovery()

    def stop(*_):
        monitor.running = False
        monitor.queue.put(("stop", None))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("custom-rpi-mqtt-monitor %s indul (%s)", VERSION, cfg["device"]["name"])
    monitor.run()


if __name__ == "__main__":
    sys.exit(main())
