"""Offline tests for the collector. Run from the repo root:

    python3 -m unittest discover -s tests -v

Everything runs against a fake system (tests/fakesys.py): nothing reads your
real disks, services or Docker, and nothing needs root.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fakesys  # noqa: E402
from homelab_dash import apps, collector, config, disks, services, system  # noqa: E402
from homelab_dash.util import CheckError, atomic_write_json, read_secret, run_with_timeout  # noqa: E402


_SHARED = {}


def setUpModule():
    """Build one fake server and run the collector once for the whole module."""
    fakesys.make_fakebin_executable()
    _SHARED["obj"] = type("Shared", (), {})
    FakeServerTest._build(_SHARED["obj"])


def tearDownModule():
    obj = _SHARED["obj"]
    for name, srv in obj.servers.items():
        if hasattr(srv, "serve_forever"):
            srv.shutdown()
            srv.server_close()
        else:
            srv.close()
    obj.docker.shutdown()
    obj.docker.server_close()
    shutil.rmtree(obj.base, ignore_errors=True)


class FakeServerTest(unittest.TestCase):
    """Tests that read the shared fake server and its first collector run."""

    @classmethod
    def setUpClass(cls):
        for k, v in vars(_SHARED["obj"]).items():
            if not k.startswith("__"):
                setattr(cls, k, v)

    @staticmethod
    def _build(cls):
        cls.base = tempfile.mkdtemp(prefix="hldash-test-")
        cls.paths = fakesys.build_root(cls.base)
        cls.secrets = fakesys.write_secrets(cls.base)
        cls.sock = os.path.join(cls.base, "docker.sock")
        cls.docker = fakesys.start_docker(cls.sock)
        cls.servers = fakesys.start_all()
        cls.cfg_path = fakesys.write_config(cls.base, cls.paths, cls.sock, cls.servers, cls.secrets,
                                            check_timeout=2)
        cls.cfg = config.load(cls.cfg_path)
        t0 = time.monotonic()
        cls.status, cls.hist, cls.state = collector.collect_once(cls.cfg)
        cls.elapsed = time.monotonic() - t0
        cls.cards = {c["id"]: c for c in cls.status["cards"]}


class TestFullRun(FakeServerTest):
    def test_all_expected_cards(self):
        self.assertEqual(
            [c["id"] for c in self.status["cards"]],
            ["system", "disks", "services", "docker", "network", "pihole",
             "media-jellyfin", "media-plex", "games", "security", "addresses"])

    def test_json_is_serialisable_and_has_header(self):
        text = json.dumps(self.status, allow_nan=False)
        self.assertIn('"schema": 1', text)
        self.assertEqual(self.status["hostname"], "homeserver")
        self.assertEqual(self.status["stale_after"], 180)

    def test_hung_checks_are_contained(self):
        # smartctl on /dev/sdd hangs and one HTTP service never answers;
        # the run still finishes quickly and only those items show errors.
        self.assertLess(self.elapsed, 15)
        errs = {c["id"]: [e["check"] for e in c["errors"]] for c in self.status["cards"]}
        self.assertEqual(errs["disks"], ["SMART /dev/sdd"])
        self.assertEqual(errs["services"], ["App that hangs"])
        for cid in ("system", "network", "pihole", "media-jellyfin", "media-plex", "docker", "addresses", "security"):
            self.assertEqual(errs[cid], [], cid)

    def test_secrets_never_in_output(self):
        blob = json.dumps(self.status) + json.dumps(self.hist)
        for secret in (fakesys.PIHOLE_PASSWORD, fakesys.JELLYFIN_KEY, fakesys.PLEX_TOKEN):
            self.assertNotIn(secret, blob)

    def test_docker_never_receives_writes(self):
        self.assertEqual(self.docker.posts, 0)


class TestSystemCard(FakeServerTest):
    def test_values(self):
        d = self.cards["system"]["data"]
        self.assertEqual(d["os"], "Ubuntu 24.04.1 LTS")
        self.assertEqual(d["cpu_model"], "Intel(R) Core(TM) i5-12400")
        self.assertEqual(d["cores"], 6)
        self.assertAlmostEqual(d["uptime_s"], 1234567.89)
        self.assertEqual(d["load"], [0.42, 0.55, 0.61])
        self.assertAlmostEqual(d["mem"]["pct"], 44.8, places=1)
        self.assertEqual(d["failed_units"], ["jellyfin.service"])
        self.assertEqual(d["journal_errors_1h"], 3)
        self.assertEqual(self.cards["system"]["status"], "warn")  # because of the failed unit

    def test_temperatures_skip_bogus_sensor(self):
        temps = self.cards["system"]["data"]["temps"]
        self.assertEqual([t["label"] for t in temps], ["Package id 0", "Core 0", "Composite"])
        self.assertEqual(self.cards["system"]["data"]["temp_max"], 52.0)

    def test_cpu_percent_uses_previous_sample(self):
        total, idle = system._cpu_times(self.cfg)
        # 2000 jiffies passed since the last run, 1000 of them idle -> 50 % busy
        state = {"cpu": {"t": time.time() - 60, "total": total - 2000, "idle": idle - 1000}}
        pct = system.cpu_percent(self.cfg, state)
        self.assertEqual(pct, 50.0)


class TestDisksCard(FakeServerTest):
    def test_mounts_filtered_and_deduplicated(self):
        mounts = [m["mount"] for m in self.cards["disks"]["data"]["mounts"]]
        self.assertEqual(mounts, ["/", "/mnt/backup", "/mnt/media"])

    def test_smart(self):
        smart = {s["device"]: s for s in self.cards["disks"]["data"]["smart"]}
        self.assertEqual(smart["/dev/nvme0"]["status"], "ok")
        self.assertEqual(smart["/dev/nvme0"]["pct_used"], 3)
        self.assertEqual(smart["/dev/sda"]["status"], "ok")
        self.assertEqual(smart["/dev/sdb"]["status"], "warn")
        self.assertIn("16 reallocated sectors", smart["/dev/sdb"]["reasons"])
        self.assertTrue(smart["/dev/sdc"]["standby"])  # asleep: not woken up
        self.assertEqual(smart["/dev/sdd"]["status"], "error")

    def test_smart_cache_is_used(self):
        cache = {"/dev/sda": {"t": time.time(), "info": {"model": "cached!", "status": "ok"}}}
        info = disks.smart_one(self.cfg, {"name": "/dev/sda", "type": "sat"}, cache)
        self.assertEqual(info["model"], "cached!")
        self.assertTrue(info["cached"])

    def test_degraded_raid_is_critical(self):
        raid = self.cards["disks"]["data"]["raid"]
        self.assertEqual(raid[0]["name"], "md0")
        self.assertEqual(raid[0]["state"], "degraded")
        self.assertEqual(raid[0]["status"], "crit")
        self.assertEqual(self.cards["disks"]["status"], "crit")

    def test_failed_smart(self):
        info = disks.parse_smart({"smart_status": {"passed": False}, "model_name": "x"})
        status, reasons = disks.smart_status(info, self.cfg)
        self.assertEqual(status, "crit")


class TestServices(FakeServerTest):
    def items(self):
        return {i["name"]: i for i in self.cards["services"]["data"]["items"]}

    def test_systemd(self):
        i = self.items()
        self.assertEqual(i["samba"]["status"], "ok")
        self.assertEqual(len(i["samba"]["units"]), 2)
        self.assertEqual(i["jellyfin-pkg"]["status"], "crit")

    def test_http_tcp_process(self):
        i = self.items()
        self.assertEqual(i["uptime-kuma"]["status"], "ok")
        self.assertEqual(i["uptime-kuma"]["link"], "http://homeserver:3001/")
        self.assertEqual(i["ssh"]["status"], "ok")
        self.assertEqual(i["valheim-proc"]["status"], "ok")
        self.assertEqual(i["hung-app"]["status"], "error")

    def test_process_user_filter(self):
        self.assertEqual(services.find_processes(self.cfg, "valheim_server", "1001"), [4242])
        self.assertEqual(services.find_processes(self.cfg, "valheim_server", "0"), [])

    def test_unknown_unit(self):
        sec = {"unit": "does-not-exist"}
        res = services.check_systemd(self.cfg, sec)
        self.assertEqual(res["status"], "error")


class TestDocker(FakeServerTest):
    def stacks(self):
        return {g["name"]: g for g in self.cards["docker"]["data"]["stacks"]}

    def test_grouping(self):
        names = [g["name"] for g in self.cards["docker"]["data"]["stacks"]]
        self.assertEqual(names, ["dns", "Home Assistant add-ons", "Home Assistant Supervisor",
                                 "media", "monitoring", "Standalone containers"])

    def test_sidecars(self):
        media = {c["name"]: c for c in self.stacks()["media"]["containers"]}
        self.assertEqual(media["qbittorrent"]["role"], "sidecar")
        self.assertEqual(media["qbittorrent"]["network_via"], "gluetun")
        self.assertEqual(media["prowlarr"]["network_via"], "gluetun")  # short id prefix
        self.assertEqual(sorted(media["gluetun"]["provides_network_to"]), ["prowlarr", "qbittorrent"])

    def test_addons_and_macvlan(self):
        s = self.stacks()
        self.assertEqual(s["Home Assistant add-ons"]["containers"][0]["role"], "add-on")
        pihole = next(c for c in s["dns"]["containers"] if c["name"] == "pihole")
        self.assertEqual(pihole["macvlan"], [{"network": "lan_macvlan", "driver": "macvlan", "ip": "192.168.1.53"}])
        self.assertEqual(self.cards["docker"]["data"]["vlan_networks"][0]["parent"], "enp3s0")

    def test_states(self):
        s = self.stacks()
        st = {c["name"]: c for c in s["Standalone containers"]["containers"]}
        self.assertEqual(st["broken-app"]["status"], "crit")
        self.assertEqual(st["watchtower"]["status"], "unknown")  # stopped on purpose: grey
        self.assertEqual(s["monitoring"]["status"], "warn")      # health: starting
        self.assertEqual(self.cards["docker"]["data"]["running"], 9)

    def test_crashed_vs_stopped(self):
        status, short = services.container_state("exited", "Exited (1) 1 minute ago")
        self.assertEqual((status, short), ("crit", "crashed (exit 1)"))
        self.assertEqual(services.container_state("exited", "Exited (143) 1 day ago")[0], "unknown")
        self.assertEqual(services.container_state("running", "Up 1 h (unhealthy)")[0], "crit")

    def test_single_container_check(self):
        res = services.check_container(self.cfg, {"container": "jellyfin"})
        self.assertEqual(res["status"], "ok")
        with self.assertRaises(CheckError):
            services.check_container(self.cfg, {"container": "../../etc"})


class TestNetwork(FakeServerTest):
    def test_interfaces(self):
        d = self.cards["network"]["data"]
        self.assertEqual([i["name"] for i in d["interfaces"]], ["enp3s0", "tailscale0"])
        self.assertEqual(d["interfaces"][0]["addrs"], ["192.168.1.10/24"])
        self.assertEqual(d["gateway"], {"iface": "enp3s0", "gateway": "192.168.1.1"})
        self.assertTrue(d["dns"]["ok"])

    def test_tailscale(self):
        t = self.cards["network"]["data"]["tailscale"]
        self.assertEqual(t["state"], "Running")
        self.assertEqual(t["peers_online"], 2)
        self.assertEqual(t["dns_name"], "homeserver.tailnet-1234.ts.net")

    def test_rates_on_second_run(self):
        state = dict(self.state)
        fakesys.bump_counters(self.paths)
        from homelab_dash import network
        card = network.collect(self.cfg, state)
        self.assertGreater(card["data"]["rx_bps"], 0)


class TestApps(FakeServerTest):
    def test_pihole_v6_and_session_reuse(self):
        d = self.cards["pihole"]["data"]
        self.assertEqual(d["version"], 6)
        self.assertEqual(d["queries"], 48211)
        self.assertEqual(d["blocking"], "enabled")
        logins = self.servers["pihole"].state["logins"]
        state = json.loads(json.dumps(self.state))
        apps.pihole(self.cfg, state)
        apps.pihole(self.cfg, state)
        self.assertEqual(self.servers["pihole"].state["logins"], logins,
                         "the saved session must be re-used, not a new login each run")

    def test_pihole_v5(self):
        srv = fakesys.start_app("pihole5")
        cfg = config.Config(None)
        cfg.cp.read_dict({"pihole": {"url": fakesys.url_of(srv), "password_file": self.secrets["pihole.pass"]}})
        d = apps.pihole(cfg, {})
        self.assertEqual(d["version"], 5)
        self.assertEqual(d["pct_blocked"], 15.0)
        srv.shutdown()

    def test_media(self):
        jf = self.cards["media-jellyfin"]["data"]
        self.assertEqual((jf["active"], jf["paused"], jf["transcoding"]), (1, 1, 1))
        self.assertEqual(jf["streams"], [])  # show_titles = no: no titles or names
        px = self.cards["media-plex"]["data"]
        self.assertEqual(px["streams"][0]["title"], "Show - Episode 3")

    def test_games(self):
        g = {s["name"]: s for s in self.cards["games"]["data"]["servers"]}
        self.assertEqual((g["valheim"]["players"], g["valheim"]["max_players"]), (3, 10))
        self.assertEqual(g["minecraft"]["players"], 2)
        self.assertEqual(g["ark"]["status"], "crit")
        self.assertEqual(self.cards["games"]["data"]["players"], 5)


class TestSecrets(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_loose_permissions_refused_without_leaking(self):
        p = os.path.join(self.dir, "k")
        fakesys.write(p, "supersecret\n", mode=0o644)
        with self.assertRaises(CheckError) as cm:
            read_secret(p)
        self.assertIn("chmod 600", str(cm.exception))
        self.assertNotIn("supersecret", str(cm.exception))

    def test_ok_and_missing(self):
        p = os.path.join(self.dir, "k")
        fakesys.write(p, "  s3cret \n", mode=0o600)
        self.assertEqual(read_secret(p), "s3cret")
        with self.assertRaises(CheckError):
            read_secret(os.path.join(self.dir, "nope"))

    def test_symlink_refused(self):
        p = os.path.join(self.dir, "k")
        fakesys.write(p, "x", mode=0o600)
        os.symlink(p, os.path.join(self.dir, "link"))
        with self.assertRaises(CheckError):
            read_secret(os.path.join(self.dir, "link"))


class TestAddresses(FakeServerTest):
    def nets(self):
        return {n["name"]: n for n in self.cards["addresses"]["data"]["networks"]}

    def test_macvlan_free_ips(self):
        n = self.nets()["lan_macvlan"]
        # pool 192.168.1.48/28 -> .49-.62; minus .53 (pihole), .54 (stopped, static),
        # .62 (reserved aux address), .50 (seen on the LAN). .57 is an incomplete ARP entry: still free.
        self.assertEqual(n["pool"], "192.168.1.48/28")
        self.assertEqual(n["free_ranges"], ["192.168.1.49", "192.168.1.51-192.168.1.52", "192.168.1.55-192.168.1.61"])
        self.assertEqual(n["free_count"], 10)
        self.assertEqual(n["lan_seen"], 1)
        self.assertEqual([(u["ip"], u["name"], u["note"]) for u in n["used"]],
                         [("192.168.1.53", "pihole", ""), ("192.168.1.54", "adguard", "stopped, IP reserved")])
        self.assertEqual(n["status"], "ok")
        self.assertFalse(any("no ip_range" in x for x in n["notes"]))

    def test_small_ipvlan_warns(self):
        n = self.nets()["iot_ipvlan"]
        self.assertEqual(n["free_ranges"], ["10.20.0.3-10.20.0.6"])  # .1 gateway, .2 zigbee2mqtt
        self.assertEqual(n["status"], "warn")                       # fewer than warn_free (5)
        self.assertTrue(any("no ip_range" in x for x in n["notes"]))
        self.assertEqual(self.cards["addresses"]["status"], "warn")

    def test_domains_are_links(self):
        d = {x["name"]: x for x in self.cards["addresses"]["data"]["domains"]}
        self.assertEqual(sorted(d), ["grafana.home.arpa", "homeserver.home.arpa", "jellyfin.home.arpa",
                                     "nas.home.arpa", "pihole.home.arpa", "printer.home.arpa"])
        self.assertEqual(d["nas.home.arpa"]["url"], "https://nas.home.arpa:5001/")   # [domains] links
        self.assertEqual(d["pihole.home.arpa"]["url"], "http://pihole.home.arpa/")
        self.assertEqual(d["jellyfin.home.arpa"]["sources"], ["CNAME"])
        self.assertEqual(d["printer.home.arpa"]["targets"], ["192.168.1.20"])
        self.assertNotIn("old.home.arpa", d)  # commented out in /etc/hosts

    def test_compress(self):
        from homelab_dash import addresses
        import ipaddress
        ips = [ipaddress.ip_address("10.0.0.%d" % i) for i in (1, 2, 3, 5, 7, 8)]
        self.assertEqual(addresses.compress(ips), ["10.0.0.1-10.0.0.3", "10.0.0.5", "10.0.0.7-10.0.0.8"])

    def test_no_card_when_nothing_to_show(self):
        from homelab_dash import addresses
        cfg = config.Config(None)
        cfg.cp.read_dict({"general": {"etc": "/nonexistent"}})
        self.assertIsNone(addresses.build(cfg, [], {}))


class TestHistoryAndFiles(unittest.TestCase):
    def test_history_prunes_to_window(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "history.json")
        now = 1_000_000
        old = {"fields": collector.HISTORY_FIELDS,
               "samples": [[now - 90000, 1, 1, 1, 1, 1, 1, 1], [now - 3600, 2, 2, 2, 2, 2, 2, 2]]}
        atomic_write_json(path, old)
        h = collector.update_history(path, [now, 3, 3, 3, 3, 3, 3, 3], 24)
        self.assertEqual([s[0] for s in h["samples"]], [now - 3600, now])
        shutil.rmtree(d)

    def test_corrupt_history_starts_over(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "history.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        h = collector.update_history(path, [5, 1, 1, 1, 1, 1, 1, 1], 24)
        self.assertEqual(len(h["samples"]), 1)
        shutil.rmtree(d)

    def test_atomic_write_modes(self):
        d = tempfile.mkdtemp()
        atomic_write_json(os.path.join(d, "a.json"), {"x": 1}, mode=0o600)
        self.assertEqual(os.stat(os.path.join(d, "a.json")).st_mode & 0o777, 0o600)
        self.assertEqual([f for f in os.listdir(d)], ["a.json"])  # no temp files left
        shutil.rmtree(d)

    def test_run_with_timeout(self):
        ok, res, _ = run_with_timeout(time.sleep, 0.2, 5)
        self.assertFalse(ok)
        self.assertIn("timed out", res)
        ok, res, _ = run_with_timeout(lambda: 1 / 0, 1)
        self.assertFalse(ok)
        self.assertIn("ZeroDivisionError", res)


class TestCrashingCard(FakeServerTest):
    def test_a_crashing_card_does_not_blank_the_page(self):
        orig = system.collect
        try:
            system.collect = lambda cfg, state: 1 / 0
            cards = collector.run_cards(self.cfg, {}, deadline=20)
        finally:
            system.collect = orig
        by = {c["id"]: c for c in cards}
        self.assertEqual(by["system"]["status"], "error")
        self.assertIn("ZeroDivisionError", by["system"]["errors"][0]["error"])
        self.assertEqual(by["network"]["status"], "ok")



class TestCommandLine(FakeServerTest):
    def test_dry_run_writes_nothing(self):
        import contextlib
        import io
        data = self.cfg.data_dir
        before = sorted(os.listdir(data))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = collector.main(["--config", self.cfg_path, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["schema"], 1)
        self.assertIn("nothing written", err.getvalue())
        self.assertEqual(sorted(os.listdir(data)), before)

    def test_real_run_then_summary(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(collector.main(["--config", self.cfg_path]), 0)
            self.assertEqual(collector.main(["--config", self.cfg_path, "--summary"]), 0)
        data = self.cfg.data_dir
        self.assertEqual(os.stat(os.path.join(data, "status.json")).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(os.path.join(data, "state.json")).st_mode & 0o777, 0o600)
        self.assertIn("SMART /dev/sdd", out.getvalue())

    def test_bad_config_exit_code(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(collector.main(["--config", "/nonexistent.ini"]), 2)



class TestSecurity(FakeServerTest):
    def d(self):
        return self.cards["security"]["data"]

    def test_updates_and_reboot(self):
        self.assertEqual((self.d()["updates"]["pending"], self.d()["updates"]["security"]), (12, 3))
        self.assertTrue(self.d()["updates"]["auto_updates"])
        rb = self.d()["reboot"]
        self.assertTrue(rb["required"])
        self.assertEqual((rb["kernel_running"], rb["kernel_newest"]), ("6.8.0-45-generic", "6.8.0-50-generic"))

    def test_firewall_and_ports(self):
        self.assertEqual(self.d()["firewall"], {"name": "ufw", "active": True, "rules": 3})
        ports = {p["port"]: p for p in self.d()["ports"]}
        self.assertEqual(sorted(ports), [22, 6379, 8096])          # 631 is loopback only, not listed
        self.assertTrue(ports[22]["expected"])
        self.assertEqual(ports[6379]["status"], "warn")
        self.assertEqual(ports[8096]["docker"], "jellyfin")         # published by Docker: bypasses ufw
        self.assertTrue(self.d()["docker_bypasses_firewall"])

    def test_ssh(self):
        c = self.d()["ssh_config"]
        self.assertEqual(c["password_auth"], "yes")                 # from the Include, first value wins
        self.assertEqual(c["status"], "warn")
        lg = self.d()["logins"]
        self.assertEqual((lg["failed_24h"], lg["accepted_24h"]), (4, 1))
        self.assertEqual(lg["top_sources"][0], {"ip": "203.0.113.9", "count": 3})
        self.assertEqual(self.d()["fail2ban"], {"jails": {"sshd": 2}})

    def test_ssh_config_rules(self):
        from homelab_dash import security
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "ssh"))
        cfg = config.Config(None)
        cfg.cp.read_dict({"general": {"etc": d}})
        for text, status in (("PermitRootLogin yes\n", "crit"),            # root + passwords (default yes)
                             ("PermitRootLogin yes\nPasswordAuthentication no\n", "warn"),
                             ("PasswordAuthentication no\nPermitEmptyPasswords yes\n", "crit"),
                             ("PasswordAuthentication no\nMatch all\nPermitRootLogin yes\n", "ok")):
            with open(os.path.join(d, "ssh", "sshd_config"), "w") as fh:
                fh.write(text)
            self.assertEqual(security.ssh_config(cfg)["status"], status, text)
        shutil.rmtree(d)

    def test_exposure(self):
        e = {r["unit"]: r for r in self.d()["exposure"]}
        self.assertEqual(e["homelab-dash-collector.service"]["status"], "ok")
        self.assertEqual(e["smbd.service"]["score"], 9.6)
        self.assertEqual(e["smbd.service"]["status"], "unknown")    # other apps: shown, not alarmed

    def test_permissions_and_containers(self):
        p = {r["what"]: r for r in self.d()["permissions"]}
        self.assertEqual(p["secret"]["status"], "ok")
        self.assertEqual(p["dashboard code"]["status"], "ok")
        self.assertIn("alice (root-equivalent)", p["Docker socket"]["detail"])
        c = {r["name"]: r for r in self.d()["containers"]}
        self.assertEqual(c["broken-app"]["status"], "crit")         # privileged
        self.assertIn("Docker socket mounted", c["watchtower"]["risks"][0])
        self.assertIn("NET_ADMIN", c["qbittorrent"]["risks"][0])
        self.assertEqual(self.cards["security"]["status"], "crit")

    def test_second_run_from_saved_state(self):
        # The timer runs the collector every minute with state saved as JSON in between;
        # cached results must still work after that round trip.
        from homelab_dash import security
        state = json.loads(json.dumps(self.state))
        for _ in range(2):
            card = security.collect(self.cfg, state)
            state = json.loads(json.dumps(state))
        ports = {p["port"]: p for p in card["data"]["ports"]}
        self.assertEqual(ports[8096].get("docker"), "jellyfin")
        self.assertTrue(card["data"]["updates"].get("cached"))

    def test_loose_secret_is_flagged(self):
        from homelab_dash import security
        f = os.path.join(self.base, "secrets", "pihole.pass")
        os.chmod(f, 0o644)
        try:
            rows = {r["path"]: r for r in security.permissions(self.cfg)}
            self.assertEqual(rows[f]["status"], "crit")
            self.assertIn("expected 600", rows[f]["detail"])
        finally:
            os.chmod(f, 0o600)


if __name__ == "__main__":
    unittest.main()
