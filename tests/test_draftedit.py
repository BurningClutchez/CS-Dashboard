"""Offline tests for switching stats cards on in a draft config (installer step 3)."""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from homelab_dash import config, detect, draftedit  # noqa: E402


def facts(pihole_reachable=False):
    return {
        "host": {"hostname": "homeserver", "os": "Ubuntu", "kernel": "6.8", "arch": "x86_64", "cpu": "i5",
                 "cores": 4, "ram": 8 << 30, "python": "3.12.3", "systemd": 255},
        "drives": {"disks": [], "smartctl": None, "smart_devices": [], "usb": [], "nvme": False},
        "mounts": [], "sensors": 0,
        "network": {"iface": "enp3s0", "ip": "192.168.1.10", "subnet": "192.168.1.0/24",
                    "tailscale": None, "wg": [], "macvlan_shim": []},
        "firewall": {"ufw": "active", "firewalld": None, "nft_rules": 0, "active": "ufw"},
        "port": 8088, "services": [], "failed_units": [], "linuxgsm": [], "home_separate": False,
        "local_domains": [],
        "docker": {"containers": [
            {"name": "pihole", "image": "pihole/pihole:latest", "state": "running", "stack": "dns", "role": "main",
             "parent": None, "host_net": False, "macvlan_ips": ["192.168.1.53"], "ports": {}}],
            "vlan_networks": [{"name": "lan", "driver": "macvlan", "parent": "enp3s0",
                               "subnet": "192.168.1.0/24", "ip_range": None}]},
    }


class TestDraftEdit(unittest.TestCase):
    def setUp(self):
        self.orig = detect.reachable
        detect.reachable = lambda url, timeout=2: False   # Pi-hole on a macvlan IP: unreachable
        self.text = detect.draft_config(facts())
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        detect.reachable = self.orig
        shutil.rmtree(self.dir)

    def load(self, text):
        p = os.path.join(self.dir, "c.ini")
        with open(p, "w") as fh:
            fh.write(text)
        return config.load(p)

    def test_scan_reports_off_with_reason(self):
        st = draftedit.scan(self.text)
        self.assertEqual(st["pihole"]["state"], "off")
        self.assertEqual(st["pihole"]["url"], "http://192.168.1.53:80")
        self.assertIn("not reachable", st["pihole"]["reason"])
        self.assertEqual(st["plex"]["state"], "missing")
        self.assertFalse(self.load(self.text).has("pihole"))   # off = no card, no secret asked

    def test_enable_off_section(self):
        text = draftedit.enable(self.text, "pihole", "http://127.0.0.1:8081", "/etc/homelab-dash")
        cfg = self.load(text)
        self.assertEqual(cfg.get("pihole", "url"), "http://127.0.0.1:8081")
        self.assertEqual(cfg.get("pihole", "password_file"), "/etc/homelab-dash/secrets/pihole.pass")
        self.assertEqual(draftedit.scan(text)["pihole"]["state"], "on")
        # the explanation comments above the section stay comments
        self.assertIn("# Pi-hole (container pihole, macvlan IP).", text)

    def test_add_missing_media(self):
        text = draftedit.enable(self.text, "plex", "http://192.168.1.20:32400", "/srv/conf")
        cfg = self.load(text)
        self.assertEqual(cfg.get("media:plex", "type"), "plex")
        self.assertEqual(cfg.get("media:plex", "key_file"), "/srv/conf/secrets/plex.token")
        self.assertEqual(draftedit.scan(text)["plex"]["state"], "on")

    def test_rejects_bad_input(self):
        for kind, url in (("pihole", "not a url"), ("pihole", "ftp://x"), ("nextcloud", "http://x")):
            with self.assertRaises(ValueError):
                draftedit.enable(self.text, kind, url, "/etc/homelab-dash")

    def test_cli_round_trip(self):
        import contextlib
        import io
        p = os.path.join(self.dir, "d.ini")
        with open(p, "w") as fh:
            fh.write(self.text)
        self.assertEqual(detect.main(["--draft-enable", p, "pihole", "http://127.0.0.1:8081", "/etc/homelab-dash"]), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            detect.main(["--draft-stats", p])
        self.assertIn("pihole|on|http://127.0.0.1:8081|", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(detect.main(["--draft-enable", p, "pihole", "bad", "/etc/homelab-dash"]), 2)


if __name__ == "__main__":
    unittest.main()
