"""Füstteszt Linux nélkül is: hamis /proc-adatokkal felépíti a discovery-t és a payloadokat,
és jinja2-vel (ha telepítve van) kiértékeli az összes value_template-et.

Futtatás:  python -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rpi_mqtt_monitor as rmm  # noqa: E402

FAKE_FILES = {
    "/proc/stat": "cpu  100 0 50 1000 10 0 0 0 0 0\nbtime 1760000000\n",
    "/proc/meminfo": "MemTotal: 3900000 kB\nMemAvailable: 2000000 kB\nSwapTotal: 100000 kB\nSwapFree: 50000 kB\n",
    "/sys/class/thermal/thermal_zone0/temp": "48312",
    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "1500000",
    "/proc/net/route": "Iface\tDestination\tGateway\neth0\t00000000\t0100A8C0\n",
    "/proc/net/dev": "h1\nh2\n  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n",
    rmm.System.THROTTLED_SYSFS: "50005",
    "/proc/device-tree/model": "Raspberry Pi 4 Model B Rev 1.4\x00",
    "/proc/device-tree/serial-number": "10000000abcdef\x00",
    "/etc/os-release": 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n',
    "/sys/class/net/eth0/address": "dc:a6:32:00:00:01",
}


def fake_read(path, default=None):
    value = FAKE_FILES.get(path)
    return value.strip().strip("\x00") if value is not None else default


class FakeUname:
    release = "6.6.51+rpt-rpi-v8"


class SmokeTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(rmm, "read", fake_read),
            mock.patch.object(rmm.os, "getloadavg", lambda: (0.5, 0.4, 0.3), create=True),
            mock.patch.object(rmm.os, "uname", lambda: FakeUname(), create=True),
            mock.patch.object(rmm.shutil, "which", lambda name: f"/usr/bin/{name}"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cfg = {s: dict(v) for s, v in rmm.DEFAULT_CONFIG.items()}
        cfg["mqtt"]["host"] = "127.0.0.1"
        self.m = rmm.Monitor(cfg, dry_run=True)
        self.m.docker.available = lambda: True

    def render(self, template, payload):
        try:
            import jinja2
        except ImportError:
            self.skipTest("jinja2 nincs telepítve")
        env = jinja2.Environment()
        env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)
        return env.from_string(template).render(value_json=payload)

    def test_discovery_and_templates(self):
        fast = self.m.system.collect_fast()
        self.assertEqual(fast["cpu_temp"], 48.3)
        self.assertTrue(fast["undervoltage"])          # 0x50005: 0. bit
        self.assertTrue(fast["throttling"])            # 2. bit
        self.assertTrue(fast["throttled_since_boot"])  # 16./18. bit
        slow = {"updates": 3, "updates_security": 1, "updates_list": ["a", "b", "c"], "reboot_required": False,
                "kernel": "k", "os": "o", "agent_version": rmm.VERSION, "images_count": 4, "images_dangling": 1,
                "images_unused": 2, "images_size_gb": 1.2, "containers_running": 2}
        self.m.disk_data = (
            {"root": {"mount": "/", "kind": "SSD", "usage": 41.0, "free_gb": 80.1},
             "mnt_hdd": {"mount": "/mnt/hdd", "kind": "HDD", "usage": 77.0, "free_gb": 300.0}},
            {"sda": {"kind": "SSD", "temperature": 35, "passed": True}},
        )
        svc = {"unit": "cloudflared.service", "description": "cloudflared", "active": "active",
               "sub": "running", "state_text": "fut"}
        ct = {"name": "cloudflared", "image": "cloudflare/cloudflared:latest", "running": False,
              "state_text": "leállt (1)", "status": "exited"}
        self.m.system.has_zram = True
        fast.update({"zram_usage": 20.8, "zram_ratio": 4.0, "zram_mem_mb": 27.0, "zram_size_mb": 480.0,
                     "zram_data_mb": 100.0, "zram_compr_mb": 25.0, "zram_algorithm": "lz4", "zram_devices": []})
        procs = {"top": "node (293 MB)", "updated": "x",
                 "processes": [{"name": "node", "pid": 20, "rss_mb": 293.0, "container": "gtfs-manager"}]}
        self.m.build_system_discovery()
        self.m.build_disk_discovery()
        self.m.build_unit_discovery("services", "svc", {"cloudflared": svc})
        self.m.build_unit_discovery("containers", "ct", {"cloudflared": ct})

        payloads = {
            f"{self.m.base}/system": fast, f"{self.m.base}/system_slow": slow,
            f"{self.m.base}/disk/root": self.m.disk_data[0]["root"],
            f"{self.m.base}/disk/mnt_hdd": self.m.disk_data[0]["mnt_hdd"],
            f"{self.m.base}/smart/sda": self.m.disk_data[1]["sda"],
            f"{self.m.base}/service/cloudflared": svc, f"{self.m.base}/container/cloudflared": ct,
            f"{self.m.base}/service/cloudflared/log": rmm.log_payload(["x"], 40),
            f"{self.m.base}/container/cloudflared/log": rmm.log_payload([], 40),
            f"{self.m.base}/last_command": {"state": "ok"},
            f"{self.m.base}/processes": procs,
        }
        rendered, ids = {}, set()
        for group in self.m.groups.values():
            for topic, cfg in group.items():
                self.assertRegex(topic, r"^homeassistant/[a-z_]+/pi4b/[a-z0-9_]+/config$")
                self.assertNotIn(cfg["unique_id"], ids)
                ids.add(cfg["unique_id"])
                json.dumps(cfg)
                if "value_template" in cfg:
                    rendered[cfg["default_entity_id"]] = self.render(cfg["value_template"],
                                                                     payloads[cfg["state_topic"]])
                if "json_attributes_template" in cfg:
                    json.loads(self.render(cfg["json_attributes_template"], payloads[cfg["json_attributes_topic"]]))
        self.assertEqual(rendered["sensor.pi4b_cpu_homerseklet"], "48.3")
        self.assertEqual(rendered["binary_sensor.pi4b_alulfeszultseg"], "ON")
        self.assertEqual(rendered["binary_sensor.pi4b_svc_cloudflared_fut"], "ON")
        self.assertEqual(rendered["binary_sensor.pi4b_ct_cloudflared_fut"], "OFF")
        self.assertEqual(rendered["binary_sensor.pi4b_sda_smart"], "OFF")
        self.assertEqual(rendered["sensor.pi4b_tarhely_mnt_hdd"], "77.0")
        self.assertEqual(rendered["sensor.pi4b_zram_tomorites"], "4.0")
        self.assertEqual(rendered["sensor.pi4b_memoria_top"], "node (293 MB)")
        self.assertIn("button.pi4b_ujrainditas", {c["default_entity_id"] for g in self.m.groups.values()
                                                   for c in g.values()})

    def test_commands(self):
        calls = []
        self.m.services.restart = lambda unit: (calls.append(unit), (True, ""))[1]
        self.m.services.logs = lambda unit, n: rmm.log_payload(["log"], n)
        self.m.collect_services = lambda *a, **k: None
        self.m.service_data = {"ssh": {"unit": "ssh.service", "active": "active"}}
        published = []
        self.m.publish = lambda topic, payload, **k: published.append((topic, payload))
        with mock.patch.object(rmm.time, "sleep", lambda s: None):
            self.m.handle_command(["service", "ssh", "restart"])
            self.m.handle_command(["service", "ssh", "restart"])  # cooldown: eldobva
            self.m.handle_command(["service", "nincs", "restart"])
            with mock.patch.object(rmm, "run") as run:
                self.m.handle_command(["reboot"])  # indulás utáni türelmi időben: nem fut
                run.assert_not_called()
        self.assertEqual(calls, ["ssh.service"])
        states = [p["state"] for t, p in published if t.endswith("last_command")]
        self.assertEqual(states[0], "ssh újraindítás – ok")
        self.assertIn("hiba", states[1])
        self.assertIn("hiba", states[2])

    def test_docker_log_demux(self):
        frame = b"2026-10-19T08:00:00.123456789Z hello\n"
        body = bytes([1, 0, 0, 0]) + len(frame).to_bytes(4, "big") + frame
        d = rmm.Docker(rmm.DEFAULT_CONFIG["docker"])
        d.call = lambda *a, **k: (200, body)
        out = d.logs({"full_id": "x", "tty": False}, 40)
        self.assertEqual(out["lines"], ["2026-10-19 08:00:00 hello"])


if __name__ == "__main__":
    unittest.main()
