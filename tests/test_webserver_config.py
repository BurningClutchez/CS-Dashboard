"""Offline tests for the web server, the config loader and detect.py's draft config."""

import http.client
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fakesys  # noqa: E402
from homelab_dash import config, detect, webserver  # noqa: E402


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.data = os.path.join(cls.dir, "data")
        os.makedirs(cls.data)
        with open(os.path.join(cls.data, "status.json"), "w") as fh:
            fh.write('{"schema":1,"cards":[]}')
        with open(os.path.join(cls.data, "state.json"), "w") as fh:
            fh.write('{"pihole":{"pihole_sid":"SECRET-SID"}}')
        with open(os.path.join(cls.dir, "secret.txt"), "w") as fh:
            fh.write("do not serve")
        cfg_path = os.path.join(cls.dir, "c.ini")
        with open(cfg_path, "w") as fh:
            fh.write("[web]\nbind = 127.0.0.1\nport = 8088\nweb_root = %s\ndata_dir = %s\n"
                     "allowed_hosts = auto dash.example.ts.net\n" % (os.path.join(ROOT, "web"), cls.data))
        cls.cfg = config.load(cfg_path)
        cls.srv = webserver.make_server(cls.cfg, port=0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.dir)

    def req(self, method, path, host=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_index_and_security_headers(self):
        r, body = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<title>Home lab</title>", body)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("Server"), "homelab-dash")

    def test_all_static_files_exist(self):
        for path in webserver.STATIC:
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 200, path)

    def test_json_not_cached_and_query_ignored(self):
        r, body = self.req("GET", "/status.json?t=123")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("Content-Type"), "application/json")

    def test_only_fixed_paths(self):
        for path in ("/state.json", "/../secret.txt", "/%2e%2e/secret.txt", "/web/", "/data/",
                     "//etc/passwd", "/index.html/", "/app.js.map", "/status.json/.."):
            r, body = self.req("GET", path)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn(b"SECRET-SID", body)
            self.assertNotIn(b"do not serve", body)

    def test_read_only_methods(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            r, _ = self.req(m, "/status.json", body=b"x")
            self.assertEqual(r.status, 405, m)
            self.assertEqual(r.getheader("Allow"), "GET, HEAD")

    def test_head(self):
        r, body = self.req("HEAD", "/status.json")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"")

    def test_dns_rebinding_blocked(self):
        r, _ = self.req("GET", "/status.json", host="evil.example.com")
        self.assertEqual(r.status, 421)
        r, _ = self.req("GET", "/status.json", host="dash.example.ts.net:8088")
        self.assertEqual(r.status, 200)
        r, _ = self.req("GET", "/status.json", host="[::1]:8088")
        self.assertEqual(r.status, 200)
        short = socket.gethostname().split(".")[0]
        r, _ = self.req("GET", "/", host=short + ".local")
        self.assertEqual(r.status, 200)

    def test_missing_data_is_503(self):
        os.rename(os.path.join(self.data, "status.json"), os.path.join(self.data, "x"))
        try:
            r, body = self.req("GET", "/status.json")
            self.assertEqual(r.status, 503)
            self.assertIn(b"no data yet", body)
        finally:
            os.rename(os.path.join(self.data, "x"), os.path.join(self.data, "status.json"))


class TestConfig(unittest.TestCase):
    def load(self, text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c.ini")
        with open(p, "w") as fh:
            fh.write(text)
        try:
            return config.load(p)
        finally:
            shutil.rmtree(d)

    def test_example_config_is_valid(self):
        cfg = config.load(os.path.join(ROOT, "config", "config.example.ini"))
        self.assertEqual(cfg.getint("web", "port"), 8088)

    def test_errors_are_clear(self):
        for text, msg in (("[service:x]\ntype = bogus\n", "type must be"),
                          ("[service:x]\ntype = http\n", "needs url"),
                          ("[web]\nport = 99999\n", "port"),
                          ("[media:x]\ntype = plex\n", "needs url"),
                          ("[game:x]\nprotocol = a2s\n", "needs port"),
                          ("[service:bad name!]\ntype = tcp\nport = 1\n", "bad section name"),
                          ("[thresholds]\ndisk_warn = lots\n", "number")):
            with self.assertRaises(config.ConfigError) as cm:
                self.load(text)
            self.assertIn(msg, str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load("/nonexistent/config.ini")


class TestDetectDraft(unittest.TestCase):
    """detect.py's draft config must always be a valid config."""

    def facts(self):
        return {
            "host": {"hostname": "homeserver", "os": "Ubuntu", "kernel": "6.8", "arch": "x86_64",
                     "cpu": "i5", "cores": 6, "ram": 16 << 30, "python": "3.12.3", "systemd": 255},
            "drives": {"disks": [], "smartctl": "7.4", "smart_devices": ["/dev/sda", "/dev/sdb:sat"],
                       "usb": ["/dev/sdb"], "nvme": True},
            "mounts": [{"mount": "/", "fs": "ext4", "device": "/dev/nvme0n1p2"},
                       {"mount": "/home", "fs": "ext4", "device": "/dev/nvme0n1p3"}],
            "sensors": 3,
            "network": {"iface": "enp3s0", "ip": "192.168.1.10", "subnet": "192.168.1.0/24",
                        "tailscale": {"state": "Running", "ips": ["100.1.2.3"], "dns_name": "homeserver.tail-x.ts.net"},
                        "wg": [], "macvlan_shim": []},
            "firewall": {"ufw": "active", "firewalld": None, "nft_rules": 10, "active": "ufw"},
            "port": 8088,
            "services": [{"key": "samba", "label": "Samba", "group": "File sharing", "units": ["smbd", "nmbd"], "active": True},
                         {"key": "uptime-kuma", "label": "Uptime Kuma", "group": "Monitoring", "units": ["uptime-kuma"], "active": False},
                         {"key": "wg-quick-wg0", "label": "wg-quick@wg0", "group": "Network", "units": ["wg-quick@wg0"], "active": True}],
            "failed_units": [],
            "docker": {"containers": [
                {"name": "jellyfin", "image": "jellyfin/jellyfin", "state": "running", "stack": "media", "role": "main",
                 "parent": None, "host_net": False, "macvlan_ips": [], "ports": {8096: 8096}},
                {"name": "pihole", "image": "pihole/pihole:latest", "state": "running", "stack": "dns", "role": "main",
                 "parent": None, "host_net": False, "macvlan_ips": ["192.168.1.53"], "ports": {}},
                {"name": "plex", "image": "plexinc/pms-docker", "state": "running", "stack": "media", "role": "main",
                 "parent": None, "host_net": True, "macvlan_ips": [], "ports": {}}],
                "vlan_networks": [{"name": "lan", "driver": "macvlan", "parent": "enp3s0", "subnet": "192.168.1.0/24"}]},
            "linuxgsm": [{"instance": "vhserver", "server": "vhserver", "user": "vhserver", "home": "/home/vhserver",
                          "game": "Valheim", "executable": "valheim_server.x86_64", "protocol": "a2s", "port": 2457},
                         {"instance": "mcbserver", "server": "mcbserver", "user": "mc", "home": "/home/mc",
                          "game": "Minecraft Bedrock", "executable": "bedrock_server", "protocol": "none", "port": None}],
            "home_separate": True,
            "local_domains": ["nas.home.arpa"],
        }

    def test_draft_parses_and_validates(self):
        f = self.facts()
        text = detect.draft_config(f)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "draft.ini")
        with open(p, "w") as fh:
            fh.write(text)
        cfg = config.load(p)
        self.assertEqual(cfg.get("disks", "smart"), "/dev/sda /dev/sdb:sat")
        self.assertEqual(cfg.get("docker", "must_run"), "jellyfin pihole plex")
        self.assertIn("homeserver.tail-x.ts.net", cfg.get("web", "allowed_hosts"))
        names = [n for n, _ in cfg.sections("service")]
        self.assertIn("samba", names)
        self.assertNotIn("uptime-kuma", names)        # inactive: commented out
        self.assertIn("# [service:uptime-kuma]", text)
        self.assertIn("vhserver", [n for n, _ in cfg.sections("game")])
        self.assertEqual(cfg.get("game:mcbserver", "protocol"), "none")
        self.assertIn("plex", [n for n, _ in cfg.sections("media")] + ["plex"])
        # LAN addresses card: sections present, macvlan network and its missing ip_range noted
        self.assertEqual(cfg.get("addresses", "enabled"), "auto")
        self.assertEqual(cfg.get("domains", "suffix"), "home.arpa")
        self.assertIn("lan (192.168.1.0/24, no ip_range)", text)
        self.assertIn("Found in /etc/hosts: nas.home.arpa", text)
        shutil.rmtree(d)

    def test_dropins_and_firewall(self):
        f = self.facts()
        self.assertIn("IPAddressAllow=192.168.1.0/24 100.64.0.0/10 fd7a:115c:a1e0::/48", detect.web_dropin(f))
        self.assertNotIn("SocketBindAllow", detect.web_dropin(f))
        self.assertIn("SocketBindAllow=tcp:9000", detect.web_dropin(f, 9000))
        fw = detect.firewall_commands(f)
        self.assertIn("ufw allow from 192.168.1.0/24 to any port 8088 proto tcp", fw)
        self.assertIn("ufw allow in on tailscale0", fw)
        f["firewall"] = {"ufw": None, "firewalld": "active", "nft_rules": 0, "active": "firewalld"}
        self.assertIn("firewall-cmd --reload", detect.firewall_commands(f))
        self.assertIn("ProtectHome=read-only", detect.collector_dropin(f))


if __name__ == "__main__":
    unittest.main()
