"""A fake server for offline tests and the demo: fake /proc, /sys, /etc,
fake smartctl/systemctl/journalctl/ip/tailscale binaries, and fake Docker,
Pi-hole, Jellyfin, Plex, Steam-query and Minecraft servers on localhost.

Nothing here touches the real system.
"""

import json
import os
import socket
import socketserver
import stat
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
FAKEBIN = os.path.join(HERE, "fakebin")

PIHOLE_PASSWORD = "correct-horse-battery-staple"
JELLYFIN_KEY = "jf-0123456789abcdef"
PLEX_TOKEN = "plex-abcdefghijklmnop"


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, mode)


def build_root(base):
    """Create proc/, sys/, etc/, fs/ under base. Returns a dict of paths."""
    proc, sysd, etc, fs = (os.path.join(base, x) for x in ("proc", "sys", "etc", "fs"))
    write(os.path.join(proc, "uptime"), "1234567.89 4000000.00\n")
    write(os.path.join(proc, "loadavg"), "0.42 0.55 0.61 2/345 6789\n")
    write(os.path.join(proc, "stat"), "cpu  10000 0 5000 80000 1000 0 100 0 0 0\ncpu0 1 2 3 4\n")
    write(os.path.join(proc, "meminfo"),
          "MemTotal:       16303552 kB\nMemFree:         1024000 kB\nMemAvailable:    9000000 kB\n"
          "SwapTotal:       2097148 kB\nSwapFree:        2000000 kB\n")
    write(os.path.join(proc, "cpuinfo"),
          "".join("processor\t: %d\nmodel\t\t: 154\nmodel name\t: Intel(R) Core(TM) i5-12400\n\n" % i for i in range(6)))
    write(os.path.join(proc, "sys/kernel/osrelease"), "6.8.0-45-generic\n")
    write(os.path.join(proc, "sys/kernel/hostname"), "homeserver\n")
    write(os.path.join(proc, "net/route"),
          "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
          "enp3s0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
          "enp3s0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
    write(os.path.join(proc, "mdstat"),
          "Personalities : [raid1]\nmd0 : active raid1 sdb1[1] sdc1[0]\n"
          "      3906886464 blocks super 1.2 [2/1] [U_]\n      bitmap: 2/30 pages [8KB], 65536KB chunk\n\n"
          "unused devices: <none>\n")
    # mounts: the paths are mapped under fs/ by fsroot
    for m in ("", "mnt/media", "mnt/backup"):
        os.makedirs(os.path.join(fs, m), exist_ok=True)
    write(os.path.join(proc, "self/mounts"),
          "/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n"
          "/dev/nvme0n1p1 /boot/efi vfat rw 0 0\n"
          "tmpfs /run tmpfs rw 0 0\n"
          "/dev/md0 /mnt/media ext4 rw 0 0\n"
          "/dev/md0 /var/lib/docker/volumes/x ext4 rw 0 0\n"
          "/dev/sdd1 /mnt/backup xfs rw 0 0\n"
          "overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0\n")
    # processes: a LinuxGSM Valheim server owned by uid 1001, and our own pid
    write(os.path.join(proc, "4242/cmdline"), "./valheim_server.x86_64\0-name\0Vikings\0-port\0002456\0")
    write(os.path.join(proc, "4242/status"), "Name:\tvalheim_server\nUid:\t1001\t1001\t1001\t1001\n")
    write(os.path.join(proc, "4243/cmdline"), "/usr/bin/python3\0-m\0http.server\0")
    write(os.path.join(proc, "4243/status"), "Name:\tpython3\nUid:\t0\t0\t0\t0\n")
    # hwmon sensors
    hw = os.path.join(sysd, "class/hwmon")
    write(os.path.join(hw, "hwmon0/name"), "coretemp\n")
    write(os.path.join(hw, "hwmon0/temp1_input"), "52000\n")
    write(os.path.join(hw, "hwmon0/temp1_label"), "Package id 0\n")
    write(os.path.join(hw, "hwmon0/temp2_input"), "49000\n")
    write(os.path.join(hw, "hwmon0/temp2_label"), "Core 0\n")
    write(os.path.join(hw, "hwmon1/name"), "nvme\n")
    write(os.path.join(hw, "hwmon1/temp1_input"), "41850\n")
    write(os.path.join(hw, "hwmon1/temp1_label"), "Composite\n")
    write(os.path.join(hw, "hwmon2/name"), "acpitz\n")
    write(os.path.join(hw, "hwmon2/temp1_input"), "-273000\n")  # bogus, must be skipped
    # network interfaces
    net = os.path.join(sysd, "class/net")
    for name, rx, tx, state, speed, phys in (("enp3s0", 9000000000, 1200000000, "up", "1000", True),
                                             ("tailscale0", 50000000, 40000000, "unknown", "-1", False),
                                             ("docker0", 1, 1, "up", "-1", False),
                                             ("veth1234", 1, 1, "up", "10000", False),
                                             ("lo", 1, 1, "unknown", "-1", False)):
        write(os.path.join(net, name, "statistics/rx_bytes"), "%d\n" % rx)
        write(os.path.join(net, name, "statistics/tx_bytes"), "%d\n" % tx)
        write(os.path.join(net, name, "operstate"), state + "\n")
        write(os.path.join(net, name, "speed"), speed + "\n")
        if phys:
            os.makedirs(os.path.join(net, name, "device"), exist_ok=True)
    write(os.path.join(etc, "os-release"), 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\n')
    write(os.path.join(etc, "hosts"),
          "127.0.0.1 localhost\n192.168.1.10 homeserver homeserver.home.arpa\n"
          "192.168.1.20 printer.home.arpa   # office printer\n# 192.168.1.99 old.home.arpa\n")
    # ---- Security card
    write(os.path.join(etc, "ufw/ufw.conf"), "# /etc/ufw/ufw.conf\nENABLED=yes\nLOGLEVEL=low\n")
    write(os.path.join(etc, "ufw/user.rules"), "*filter\n" + "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n" * 3)
    write(os.path.join(etc, "ssh/sshd_config"),
          "Include /etc/ssh/sshd_config.d/*.conf\nPort 22\nPermitRootLogin prohibit-password\n"
          "Match User backup\n    PasswordAuthentication no\n")
    write(os.path.join(etc, "ssh/sshd_config.d/50-cloud-init.conf"), "PasswordAuthentication yes\n")
    write(os.path.join(etc, "group"), "root:x:0:\nsudo:x:27:alice\ndocker:x:999:alice\n")
    write(os.path.join(etc, "apt/apt.conf.d/20auto-upgrades"),
          'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n')
    write(os.path.join(fs, "run/reboot-required"), "*** System restart required ***\n")
    write(os.path.join(fs, "run/reboot-required.pkgs"), "linux-image-6.8.0-50-generic\n")
    for k in ("6.8.0-45-generic", "6.8.0-50-generic"):
        os.makedirs(os.path.join(fs, "lib/modules", k), exist_ok=True)
    write(os.path.join(fs, "var/lib/apt/periodic/update-success-stamp"), "")
    code = os.path.join(fs, "opt/homelab-dash")
    write(os.path.join(code, "collect.py"), "# fake\n")
    os.chmod(code, 0o755)
    # listening sockets: 22 (v4+v6), 8096 (Docker-published), 6379 (unexpected), 631 on loopback only
    write(os.path.join(proc, "net/tcp"),
          "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
          "   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1\n"
          "   1: 00000000:1FA0 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2\n"
          "   2: 00000000:18EB 00000000:0000 0A 00000000:00000000 00:00000000 00000000   999        0 3\n"
          "   3: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4\n"
          "   4: 0A00A8C0:0016 0100A8C0:C350 01 00000000:00000000 00:00000000 00000000     0        0 5\n")
    write(os.path.join(proc, "net/tcp6"),
          "  sl  local_address remote_address st\n"
          "   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A 0 0 0 0 0 6\n")
    # kernel neighbor table: one LAN device inside the macvlan pool, one outside, one incomplete
    write(os.path.join(proc, "net/arp"),
          "IP address       HW type     Flags       HW address            Mask     Device\n"
          "192.168.1.50     0x1         0x2         aa:bb:cc:00:00:50     *        enp3s0\n"
          "192.168.1.20     0x1         0x2         aa:bb:cc:00:00:20     *        enp3s0\n"
          "192.168.1.57     0x1         0x0         00:00:00:00:00:00     *        enp3s0\n")
    return {"proc": proc, "sys": sysd, "etc": etc, "fs": fs}


def bump_counters(paths, seconds=60):
    """Simulate one minute of activity (for rate calculations)."""
    p = os.path.join(paths["sys"], "class/net/enp3s0/statistics/rx_bytes")
    with open(p) as fh:
        v = int(fh.read())
    write(p, "%d\n" % (v + 125000 * seconds * 8))  # ~8 Mbit/s
    st = os.path.join(paths["proc"], "stat")
    with open(st) as fh:
        parts = fh.read().split("\n")[0].split()
    vals = [int(x) for x in parts[1:]]
    vals[0] += 1500
    vals[3] += 4500
    write(st, "cpu  " + " ".join(map(str, vals)) + "\n")


def write_secrets(base):
    sec = os.path.join(base, "secrets")
    os.makedirs(sec, exist_ok=True)
    os.chmod(sec, 0o700)
    paths = {}
    for name, value in (("pihole.pass", PIHOLE_PASSWORD), ("jellyfin.key", JELLYFIN_KEY),
                        ("plex.token", PLEX_TOKEN)):
        p = os.path.join(sec, name)
        write(p, value + "\n", mode=0o600)
        paths[name] = p
    return paths


# --------------------------------------------------------------- fake Docker

class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _DockerHandler(BaseHTTPRequestHandler):
    def address_string(self):
        return "unix"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        with open(os.path.join(FIX, "docker-containers.json")) as fh:
            containers = json.load(fh)
        if self.path.startswith("/containers/json"):
            body = containers
        elif self.path == "/networks":
            with open(os.path.join(FIX, "docker-networks.json")) as fh:
                body = json.load(fh)
        elif self.path.startswith("/containers/") and self.path.endswith("/json"):
            name = self.path.split("/")[2]
            c = next((c for c in containers if c["Names"][0] == "/" + name or c["Id"] == name), None)
            if not c:
                self.send_response(404)
                self.end_headers()
                return
            st = {"Status": c["State"], "ExitCode": 0}
            if "(healthy)" in c["Status"]:
                st["Health"] = {"Status": "healthy"}
            body = {"State": st, "Config": {"Env": c.get("Env") or []},
                    "HostConfig": c.get("HostConfig") or {}, "Mounts": c.get("Mounts") or []}
        else:
            self.send_response(404)
            self.end_headers()
            return
        if self.command == "POST":
            raise AssertionError("collector must never POST to Docker")
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self.server.posts += 1
        self.send_response(403)
        self.end_headers()


def start_docker(sock_path):
    srv = _UnixHTTPServer(sock_path, _DockerHandler)
    os.chmod(sock_path, 0o660)  # like the real /run/docker.sock (root:docker 660)
    srv.posts = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ----------------------------------------------- fake Pi-hole/Jellyfin/Plex

class _AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        st = self.server.state
        if self.path == "/api/auth" and st["mode"] == "pihole6":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            st["logins"] += 1
            if body.get("password") != PIHOLE_PASSWORD:
                return self._json(401, {"session": {"valid": False}})
            st["sid"] = "sid-%d" % st["logins"]
            return self._json(200, {"session": {"valid": True, "sid": st["sid"], "validity": 1800}})
        self._json(404, {})

    def do_GET(self):  # noqa: N802
        st = self.server.state
        mode = st["mode"]
        if mode == "hang":
            import time
            time.sleep(30)
            return
        if mode == "pihole6":
            if st.get("sid") is None or self.headers.get("X-FTL-SID") != st.get("sid"):
                return self._json(401, {"error": {"key": "unauthorized"}})
            if self.path == "/api/stats/summary":
                return self._json(200, {"queries": {"total": 48211, "blocked": 9120, "percent_blocked": 18.92,
                                                    "cached": 20000, "forwarded": 19000},
                                        "clients": {"active": 23, "total": 30},
                                        "gravity": {"domains_being_blocked": 312455, "last_update": int(__import__("time").time()) - 3 * 3600}})
            if self.path == "/api/dns/blocking":
                return self._json(200, {"blocking": "enabled"})
            if self.path == "/api/config/dns/hosts":
                return self._json(200, {"config": {"dns": {"hosts": [
                    "192.168.1.53 pihole.home.arpa", "192.168.1.30 nas.home.arpa nas.lan"]}}})
            if self.path == "/api/config/dns/cnameRecords":
                return self._json(200, {"config": {"dns": {"cnameRecords": [
                    "jellyfin.home.arpa,homeserver.home.arpa", "media.example.com,homeserver.home.arpa"]}}})
        if mode == "pihole5" and self.path.startswith("/admin/api.php"):
            if "auth=" + PIHOLE_PASSWORD not in self.path:
                return self._json(200, [])
            return self._json(200, {"dns_queries_today": 1000, "ads_blocked_today": 150,
                                    "ads_percentage_today": 15.0, "unique_clients": 9,
                                    "domains_being_blocked": 100000, "status": "enabled"})
        if mode == "jellyfin" and self.path.startswith("/Sessions"):
            if self.headers.get("Authorization") != 'MediaBrowser Token="%s"' % JELLYFIN_KEY:
                return self._json(401, {})
            return self._json(200, [
                {"UserName": "alex", "NowPlayingItem": {"Name": "Pilot", "SeriesName": "Some Show"},
                 "PlayState": {"IsPaused": False}, "TranscodingInfo": {"IsVideoDirect": False}},
                {"UserName": "sam", "NowPlayingItem": {"Name": "A Movie"}, "PlayState": {"IsPaused": True}},
                {"UserName": "kid", "PlayState": {}},
            ])
        if mode == "plex" and self.path == "/status/sessions":
            if self.headers.get("X-Plex-Token") != PLEX_TOKEN:
                return self._json(401, {})
            return self._json(200, {"MediaContainer": {"size": 1, "Metadata": [
                {"title": "Episode 3", "grandparentTitle": "Show", "User": {"title": "alex"},
                 "Player": {"state": "playing"}, "TranscodeSession": {"videoDecision": "transcode"}}]}})
        if mode == "ok":
            return self._json(200, {"ok": True})
        self._json(404, {})


def start_app(mode):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AppHandler)
    srv.daemon_threads = True
    srv.state = {"mode": mode, "logins": 0, "sid": None}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def url_of(srv):
    return "http://127.0.0.1:%d" % srv.server_address[1]


# ------------------------------------------------------- fake game servers

def start_a2s(players=3, max_players=10, with_challenge=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def loop():
        while True:
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                return
            if with_challenge and not data.endswith(b"\x01\x02\x03\x04"):
                s.sendto(b"\xff\xff\xff\xffA\x01\x02\x03\x04", addr)
                continue
            reply = (b"\xff\xff\xff\xffI\x11" + b"Vikings\0" + b"Midgard\0" + b"valheim\0" + b"Valheim\0" +
                     struct.pack("<H", 0) + bytes([players, max_players, 0]) + b"dl\0\x00\x01")
            s.sendto(reply, addr)

    threading.Thread(target=loop, daemon=True).start()
    return s


def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def start_minecraft(online=2, maximum=20):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                c.settimeout(2)
                try:
                    c.recv(1024)
                    payload = json.dumps({"version": {"name": "1.21.3"},
                                          "players": {"online": online, "max": maximum}}).encode()
                    pkt = b"\x00" + _varint(len(payload)) + payload
                    c.sendall(_varint(len(pkt)) + pkt)
                except OSError:
                    pass

    threading.Thread(target=loop, daemon=True).start()
    return srv


def start_silent_tcp():
    """Accepts connections and never answers (a hung web service)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    return srv


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------- config file

def write_config(base, paths, docker_sock, servers, secrets, check_timeout=2, extra=""):
    cfg = """
[general]
data_dir = {data}
proc = {proc}
sys = {sys}
etc = {etc}
fsroot = {fs}
bin_path = {bin}
check_timeout = {ct}
deadline = 20
root_uid = {uid}

[web]
bind = 127.0.0.1
port = {webport}
web_root = {web}
data_dir = {data}
allowed_hosts = auto homeserver.tailnet-1234.ts.net

[disks]
mounts = auto
smart = auto
smart_interval = 1800

[network]
dns_test = localhost
tailscale = yes

[docker]
enabled = yes
socket = {sock}
must_run = jellyfin pihole uptime-kuma

[service:samba]
type = systemd
unit = smbd nmbd
label = Samba
group = File sharing

[service:jellyfin-pkg]
type = systemd
unit = jellyfin
label = Jellyfin (package)
group = Media

[service:uptime-kuma]
type = http
url = {ok_url}/
label = Uptime Kuma
group = Monitoring
link = http://homeserver:3001/

[service:hung-app]
type = http
url = http://127.0.0.1:{silent}/
label = App that hangs
timeout = 1

[service:ssh]
type = tcp
host = 127.0.0.1
port = {okport}
label = SSH

[service:valheim-proc]
type = process
match = valheim_server
user = 1001
label = Valheim process

[security]
apt_check = {bin}/apt-check
code_dir = {fs}/opt/homelab-dash

[domains]
extra = grafana.home.arpa
links = nas.home.arpa=https://nas.home.arpa:5001/

[pihole]
url = {pihole}
password_file = {pw}

[media:jellyfin]
type = jellyfin
url = {jellyfin}
key_file = {jfkey}
show_titles = no

[media:plex]
type = plex
url = {plex}
key_file = {plexkey}
show_titles = yes

[game:valheim]
label = Valheim
protocol = a2s
host = 127.0.0.1
port = {a2s}
process = valheim_server
user = 1001

[game:minecraft]
label = Minecraft
protocol = minecraft
host = 127.0.0.1
port = {mc}

[game:ark]
label = ARK (offline)
protocol = a2s
host = 127.0.0.1
port = {deadudp}
timeout = 1
{extra}
""".format(data=os.path.join(base, "data"), proc=paths["proc"], sys=paths["sys"], etc=paths["etc"],
           fs=paths["fs"], bin=FAKEBIN, ct=check_timeout, webport=servers.get("webport", 18088),
           web=os.path.join(os.path.dirname(HERE), "web"), sock=docker_sock,
           ok_url=url_of(servers["ok"]), okport=servers["ok"].server_address[1],
           silent=servers["silent"].getsockname()[1],
           pihole=url_of(servers["pihole"]), pw=secrets["pihole.pass"],
           jellyfin=url_of(servers["jellyfin"]), jfkey=secrets["jellyfin.key"],
           plex=url_of(servers["plex"]), plexkey=secrets["plex.token"],
           a2s=servers["a2s"].getsockname()[1], mc=servers["mc"].getsockname()[1],
           deadudp=free_port(), uid=os.getuid(), extra=extra)
    path = os.path.join(base, "config.ini")
    write(path, cfg)
    os.makedirs(os.path.join(base, "data"), exist_ok=True)
    return path


def start_all():
    return {
        "ok": start_app("ok"),
        "pihole": start_app("pihole6"),
        "jellyfin": start_app("jellyfin"),
        "plex": start_app("plex"),
        "silent": start_silent_tcp(),
        "a2s": start_a2s(),
        "mc": start_minecraft(),
    }


def make_fakebin_executable():
    """git already stores the fake programs as executable; only fix them if a
    copy lost that, and never fail just because the files belong to someone
    else (e.g. root after a `sudo git pull`)."""
    for name in os.listdir(FAKEBIN):
        p = os.path.join(FAKEBIN, name)
        if os.access(p, os.X_OK):
            continue
        try:
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except PermissionError:
            raise RuntimeError("%s is not executable and belongs to another user; fix with: "
                               "sudo chown -R $USER: %s" % (p, os.path.dirname(HERE)))


if __name__ == "__main__":
    print("fakesys is a helper module; run tests with: python3 -m unittest discover -s tests -v", file=sys.stderr)
