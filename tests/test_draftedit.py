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
        self.assertIn("# Pi-hole (container pihole, macvlan IP, did not answer).", text)

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

    def test_placeholders_are_not_urls(self):
        # what the detector writes when it can't work out an address
        f = facts()
        f["docker"]["containers"][0]["macvlan_ips"] = []           # Pi-hole: no reachable address known
        f["docker"]["containers"].append(
            {"name": "jellyfin", "image": "jellyfin/jellyfin", "state": "running", "stack": "media", "role": "main",
             "parent": None, "host_net": False, "macvlan_ips": [], "ports": {}})
        text = detect.draft_config(f)
        self.assertIn("# url = http://PIHOLE-IP", text)
        self.assertIn("# url = http://JELLYFIN-IP:8096", text)        # not the word None
        self.assertNotIn("url = None", text)
        st = draftedit.scan(text)
        self.assertIsNone(st["pihole"]["url"])                       # never offered as a default
        self.assertIsNone(st["jellyfin"]["url"])
        for bad in ("http://PIHOLE-IP", "http://JELLYFIN-IP:8096", "None", ""):
            with self.assertRaises(ValueError):
                draftedit.enable(text, "pihole", bad, "/etc/homelab-dash")
        # a real host name that happens to contain "-ip" is fine
        self.assertIn("url = http://my-ip.home.arpa:8081", draftedit.enable(text, "pihole", "http://my-ip.home.arpa:8081", "/etc/x"))

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


class TestContainerURLs(unittest.TestCase):
    """How the detector finds a URL for Pi-hole / Jellyfin / ... in Docker."""

    def setUp(self):
        self.orig = detect.reachable
        self.answering = set()
        detect.reachable = lambda url, timeout=2: url in self.answering

    def tearDown(self):
        detect.reachable = self.orig

    @staticmethod
    def c(**kw):
        base = {"name": "app", "role": "main", "parent": None, "host_net": False, "ports": {},
                "macvlan_ips": [], "other_ips": [], "web_ports": []}
        base.update(kw)
        return base

    def test_web_ports_from_env(self):
        f = detect.web_ports_from_env
        self.assertEqual(f(["FTLCONF_webserver_port=8081o,[::]:8081o,443os"]), [8081])
        self.assertEqual(f(["FTLCONF_webserver_port=80"]), [80])
        self.assertEqual(f(["WEB_PORT=8080"]), [8080])                 # Pi-hole v5
        self.assertEqual(f(["PATH=/usr/bin", "TZ=UTC"]), [])

    def test_order_and_first_that_answers(self):
        c = self.c(ports={80: 8080}, macvlan_ips=["10.0.0.53"], other_ips=[{"network": "dns", "ip": "172.20.0.2"}])
        cands = [u for u, _ in detect.container_candidates(c, 80)]
        self.assertEqual(cands, ["http://127.0.0.1:8080", "http://10.0.0.53:80", "http://172.20.0.2:80"])
        self.answering = {"http://10.0.0.53:80"}                      # published port blocked, macvlan answers
        self.assertEqual(detect.container_url(c, 80), ("http://10.0.0.53:80", "macvlan IP"))
        self.answering = set()                                        # nothing answers: best guess, marked
        self.assertEqual(detect.container_url(c, 80), ("http://127.0.0.1:8080", "published port 80, did not answer"))

    def test_configured_pihole_port(self):
        # Pi-hole v6 serving on 8081 inside the container, published as 8081
        c = self.c(ports={8081: 8081, 53: 53}, web_ports=[8081], macvlan_ips=["10.0.0.53"])
        cands = [u for u, _ in detect.container_candidates(c, 80)]
        self.assertEqual(cands[:3], ["http://127.0.0.1:8081", "http://10.0.0.53:8081", "http://10.0.0.53:80"])

    def test_sidecar_uses_parent_network(self):
        caddy = self.c(name="caddy", macvlan_ips=["10.0.0.210"], ports={443: 443})
        pihole = self.c(name="pihole", role="sidecar", parent="caddy", web_ports=[8081])
        url, how = detect.container_url(pihole, 80, {"caddy": caddy, "pihole": pihole})
        self.assertEqual(url, "http://10.0.0.210:8081")
        self.assertIn("via caddy", how)

    def test_other_docker_network(self):
        jf = self.c(name="jellyfin", other_ips=[{"network": "media_default", "ip": "172.18.0.5"}])
        self.answering = {"http://172.18.0.5:8096"}
        self.assertEqual(detect.container_url(jf, 8096), ("http://172.18.0.5:8096", "Docker network media_default"))

    def test_guess_from_other_published_port(self):
        jf = self.c(name="jellyfin", ports={8097: 18097})
        self.assertEqual(detect.container_candidates(jf, 8096)[-1],
                         ("http://127.0.0.1:18097", "published port 8097 (a guess)"))

    def test_no_address_says_why(self):
        orphan = self.c(name="pihole", role="sidecar", parent="vpn")
        url, why = detect.container_url(orphan, 80, {})
        self.assertIsNone(url)
        self.assertIn("shares vpn's network", why)

    def test_reads_env_through_docker_api(self):
        sys.path.insert(0, HERE)
        import fakesys
        d = tempfile.mkdtemp()
        sock = os.path.join(d, "d.sock")
        srv = fakesys.start_docker(sock)
        try:
            rows = {r["name"]: r for r in detect.docker(sock)["containers"]}
            self.assertEqual(rows["pihole"]["web_ports"], [8081])
            self.assertEqual(rows["jellyfin"]["other_ips"], [{"network": "media_default", "ip": "172.18.0.2"}])
            self.assertEqual(rows["qbittorrent"]["role"], "sidecar")
            self.assertEqual(srv.posts, 0)                            # read-only
        finally:
            srv.shutdown()
            srv.server_close()
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
