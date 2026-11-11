"""Security card: updates & reboot, firewall & open ports, logins & SSH, systemd
exposure scores, file permissions and risky container settings.

Read-only like everything else: files under /etc, /proc and /var, the journal,
`systemd-analyze security`, `fail2ban-client status` and GET requests to the
Docker API. Slow checks are cached in the collector's state.
"""

import glob
import os
import re
import shutil
import stat
import time

from .util import CHILD_ENV, Card, CheckError, read_text, run_cmd, unix_http_get_json

WELL_KNOWN = {22: "ssh", 53: "dns", 80: "http", 443: "https", 139: "samba", 445: "samba",
              631: "printing", 3001: "uptime-kuma", 5353: "mdns", 8096: "jellyfin",
              8920: "jellyfin https", 32400: "plex", 25565: "minecraft", 2049: "nfs",
              111: "rpcbind", 9090: "cockpit/prometheus", 3000: "grafana", 1883: "mqtt",
              8123: "home-assistant", 51820: "wireguard", 41641: "tailscale"}
RISKY_CAPS = {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH", "SYS_RAWIO", "ALL"}
DASHBOARD_UNITS = {"homelab-dash-collector.service": 3.0, "homelab-dash-web.service": 1.5}


def _cached(state, key, max_age, func, *args):
    """Re-use a result for max_age seconds (slow checks)."""
    box = state.setdefault("security_cache", {})
    hit = box.get(key)
    now = time.time()
    if hit and now - hit.get("t", 0) < max_age:
        return dict(hit["v"], cached=True) if isinstance(hit["v"], dict) else hit["v"]
    val = func(*args)
    box[key] = {"t": now, "v": val}
    return val


def _root(cfg, path):
    return (cfg.fsroot or "") + path


def _which(cfg, name):
    path = (cfg.bin_path + ":" if cfg.bin_path else "") + CHILD_ENV["PATH"]
    return shutil.which(name, path=path)


# ------------------------------------------------------------ updates & reboot

def updates(cfg):
    out = {"manager": None, "pending": None, "security": None}
    apt_check = cfg.get("security", "apt_check", "/usr/lib/update-notifier/apt-check")
    if os.path.exists(apt_check):
        # prints "UPDATES;SECURITY" on stderr, from the local package cache (no network)
        text, _ = run_cmd([apt_check], timeout=60, ok_codes=None, path_extra=cfg.bin_path, merge_stderr=True)
        m = re.search(r"(\d+);(\d+)", text)
        if not m:
            raise CheckError("apt-check gave no answer")
        out.update(manager="apt", pending=int(m.group(1)), security=int(m.group(2)))
    elif _which(cfg, "apt-get"):
        text, _ = run_cmd(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=60,
                          ok_codes=None, path_extra=cfg.bin_path)
        inst = [l for l in text.splitlines() if l.startswith("Inst ")]
        out.update(manager="apt", pending=len(inst), security=sum(1 for l in inst if "-security" in l))
    elif _which(cfg, "dnf"):
        text, _ = run_cmd(["dnf", "-C", "-q", "updateinfo", "list", "--security"], timeout=60,
                          ok_codes=None, path_extra=cfg.bin_path)
        out.update(manager="dnf", security=len([l for l in text.splitlines() if l.strip()]))
    else:
        raise CheckError("no apt or dnf found")
    stamp = None
    for p in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists"):
        try:
            stamp = os.stat(_root(cfg, p)).st_mtime
            break
        except OSError:
            continue
    out["lists_age_days"] = round((time.time() - stamp) / 86400, 1) if stamp else None
    auto = read_text(os.path.join(cfg.etc, "apt/apt.conf.d/20auto-upgrades"), "")
    out["auto_updates"] = bool(re.search(r'Unattended-Upgrade\s+"1"', auto)) if out["manager"] == "apt" else None
    return out


def reboot_needed(cfg):
    out = {"required": False, "packages": [], "kernel_running": None, "kernel_newest": None}
    flag = _root(cfg, "/run/reboot-required")
    if os.path.exists(flag):
        out["required"] = True
        out["packages"] = read_text(flag + ".pkgs", "").split()[:10]
    running = read_text(os.path.join(cfg.proc, "sys/kernel/osrelease"), "").strip()
    mods = [os.path.basename(p) for p in glob.glob(_root(cfg, "/lib/modules/*")) if os.path.isdir(p)]

    def key(v):
        return [int(x) if x.isdigit() else x for x in re.split(r"[.\-+~]", v)]
    if running and mods:
        newest = sorted(mods, key=key)[-1]
        out.update(kernel_running=running, kernel_newest=newest)
        if newest != running and key(newest) > key(running):
            out["required"] = True
    return out


# ---------------------------------------------------------- firewall & ports

def firewall(cfg):
    ufw_conf = read_text(os.path.join(cfg.etc, "ufw/ufw.conf"), "")
    if re.search(r"^ENABLED=yes", ufw_conf, re.M):
        rules = 0
        for f in ("ufw/user.rules", "ufw/user6.rules"):
            rules += read_text(os.path.join(cfg.etc, f), "").count("### tuple ###")
        return {"name": "ufw", "active": True, "rules": rules}
    if _which(cfg, "firewall-cmd"):
        text, code = run_cmd(["firewall-cmd", "--state"], timeout=cfg.check_timeout, ok_codes=None,
                             path_extra=cfg.bin_path)
        if text.strip() == "running":
            return {"name": "firewalld", "active": True, "rules": None}
    if ufw_conf:
        return {"name": "ufw", "active": False, "rules": None}
    return {"name": None, "active": False, "rules": None}


def listening_ports(cfg):
    return listening_ports_in(cfg.proc)


def listening_ports_in(proc="/proc"):
    """TCP ports listening on a non-loopback address, from /proc/net/tcp{,6}."""
    ports = {}
    for f, v6 in (("net/tcp", False), ("net/tcp6", True)):
        for line in read_text(os.path.join(proc, f), "").splitlines()[1:]:
            p = line.split()
            if len(p) < 4 or p[3] != "0A":
                continue
            addr, port = p[1].rsplit(":", 1)
            port = int(port, 16)
            loop = addr in ("0100007F", "00000000000000000000000001000000") or (not v6 and addr.endswith("7F"))
            if loop:
                continue
            ports.setdefault(port, set()).add("IPv6" if v6 else "IPv4")
    return {port: sorted(v) for port, v in sorted(ports.items())}


def docker_published(cfg):
    sock = cfg.get("docker", "socket", "/run/docker.sock")
    if not (os.path.exists(sock) and cfg.tristate("docker", "enabled") != "no"):
        return {}
    out = {}
    for c in unix_http_get_json(sock, "/containers/json", timeout=cfg.check_timeout):
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        for p in c.get("Ports") or []:
            if p.get("PublicPort") and p.get("IP") in (None, "", "0.0.0.0", "::"):
                # str keys: this dict is cached in the JSON state file, which turns keys into text
                out.setdefault(str(int(p["PublicPort"])), name)
    return out


def expected_ports(cfg, ssh_ports):
    exp = set(ssh_ports) | {cfg.getint("web", "port", 8088)}
    raw = cfg.getlist("security", "expected_ports")
    for item in raw:
        if item.isdigit():
            exp.add(int(item))
    for _name, sec in cfg.sections("service"):
        if sec.get("type", "").strip() == "tcp" and sec.get("port", "").strip().isdigit():
            exp.add(int(sec["port"]))
        m = re.search(r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?", sec.get("url", ""))
        if m:
            exp.add(int(m.group(1) or 80))
        if "smbd" in sec.get("unit", ""):
            exp.update({139, 445})
    for _name, sec in cfg.sections("game"):
        if sec.get("port", "").strip().isdigit():
            exp.add(int(sec["port"]))
    return exp


# --------------------------------------------------------------- logins & SSH

def ssh_config(cfg):
    """Effective global sshd settings (first value wins; Match blocks ignored)."""
    base = os.path.join(cfg.etc, "ssh")
    main = os.path.join(base, "sshd_config")
    if not os.path.exists(main):
        raise CheckError("no sshd_config")
    vals = {}

    def parse(path, depth=0):
        for line in read_text(path, "").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split(None, 1)
            key, val = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
            if key == "match":
                return False  # stop: the rest of the file is conditional
            if key == "include" and depth < 4:
                for pat in val.split():
                    pat = pat if pat.startswith("/") else os.path.join(base, pat)
                    pat = os.path.join(cfg.etc, os.path.relpath(pat, "/etc")) if pat.startswith("/etc/") else pat
                    for inc in sorted(glob.glob(pat)):
                        if parse(inc, depth + 1) is False:
                            return False
                continue
            if key == "port":
                vals.setdefault("port", []).append(val)
            else:
                vals.setdefault(key, val.lower())
        return True

    parse(main)
    root = vals.get("permitrootlogin", "prohibit-password")
    pw = vals.get("passwordauthentication", "yes")
    empty = vals.get("permitemptypasswords", "no")
    findings = []
    status = "ok"
    if empty == "yes":
        findings.append("PermitEmptyPasswords yes")
        status = "crit"
    if root == "yes":
        findings.append("root can log in" + (" with a password" if pw == "yes" else ""))
        status = "crit" if pw == "yes" else "warn"
    if pw == "yes" and status == "ok":
        findings.append("password logins allowed (keys only is safer)")
        status = "warn"
    ports = [int(p) for p in vals.get("port", ["22"]) if p.isdigit()] or [22]
    return {"permit_root_login": root, "password_auth": pw, "ports": ports,
            "findings": findings, "status": status}


def ssh_logins(cfg):
    text, _ = run_cmd(["journalctl", "-q", "--no-pager", "-o", "cat", "--since", "-24h",
                       "SYSLOG_IDENTIFIER=sshd", "+", "SYSLOG_IDENTIFIER=sshd-session"],
                      timeout=cfg.check_timeout * 2, ok_codes=(0, 1), path_extra=cfg.bin_path)
    failed, ok, sources, last_ok = 0, 0, {}, None
    for line in text.splitlines():
        if re.search(r"Failed password|Invalid user|authentication failure|maximum authentication attempts", line):
            failed += 1
            m = re.search(r"from (\S+)", line)
            if m:
                sources[m.group(1)] = sources.get(m.group(1), 0) + 1
        elif line.startswith("Accepted "):
            ok += 1
            m = re.search(r"Accepted \S+ for (\S+) from (\S+)", line)
            if m:
                last_ok = "%s from %s" % (m.group(1), m.group(2))
    top = sorted(sources.items(), key=lambda kv: -kv[1])[:3]
    return {"failed_24h": failed, "accepted_24h": ok, "last_accepted": last_ok,
            "top_sources": [{"ip": ip, "count": n} for ip, n in top]}


def fail2ban(cfg):
    if not _which(cfg, "fail2ban-client"):
        return None
    text, _ = run_cmd(["fail2ban-client", "status"], timeout=cfg.check_timeout, path_extra=cfg.bin_path)
    m = re.search(r"Jail list:\s*(.*)", text)
    jails = [j.strip() for j in (m.group(1) if m else "").split(",") if j.strip()]
    banned = {}
    for j in jails[:10]:
        t, _ = run_cmd(["fail2ban-client", "status", j], timeout=cfg.check_timeout, path_extra=cfg.bin_path)
        mm = re.search(r"Currently banned:\s*(\d+)", t)
        banned[j] = int(mm.group(1)) if mm else None
    return {"jails": banned}


# ----------------------------------------------------- exposure (systemd-analyze)

def exposure(cfg, units):
    out = []
    for unit in units:
        text, _ = run_cmd(["systemd-analyze", "security", "--no-pager", unit], timeout=30,
                          ok_codes=None, path_extra=cfg.bin_path)
        m = re.search(r"Overall exposure level for \S+:\s*([\d.]+)\s+(\S+)", text)
        out.append({"unit": unit, "score": float(m.group(1)) if m else None,
                    "label": m.group(2) if m else "unknown"})
    return out


def monitored_units(cfg):
    units = list(DASHBOARD_UNITS)
    for _name, sec in cfg.sections("service"):
        if sec.get("type", "").strip() == "systemd":
            for u in re.split(r"[\s,]+", sec.get("unit", "").strip()):
                if u:
                    u = u if "." in u else u + ".service"
                    if u not in units:
                        units.append(u)
    return units[:25]


# ---------------------------------------------------------------- permissions

def _perm(path, want_mode=None, max_mode=None, owner=0, kind="file"):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"path": path, "status": "unknown", "detail": "missing"}
    except PermissionError:
        return {"path": path, "status": "unknown", "detail": "cannot check"}
    mode = stat.S_IMODE(st.st_mode)
    problems = []
    if stat.S_ISLNK(st.st_mode):
        problems.append("is a symlink")
    if owner is not None and st.st_uid != owner:
        problems.append("owner uid %d, expected root" % st.st_uid)
    if want_mode is not None and mode != want_mode:
        problems.append("mode %o, expected %o" % (mode, want_mode))
    if max_mode is not None and mode & ~max_mode:
        problems.append("mode %o is too open (at most %o)" % (mode, max_mode))
    return {"path": path, "mode": "%o" % mode, "status": "crit" if problems else "ok",
            "detail": "; ".join(problems) or "mode %o" % mode}


def permissions(cfg):
    root_uid = cfg.getint("general", "root_uid", 0)
    rows = []
    if cfg.path:
        rows.append(dict(_perm(cfg.path, max_mode=0o644, owner=root_uid), what="config"))
        rows.append(dict(_perm(os.path.dirname(cfg.path), max_mode=0o755, owner=root_uid), what="config folder"))
    files = []
    for sec in cfg.cp.sections():
        for key in ("password_file", "key_file", "session_file"):
            v = cfg.get(sec, key, "").strip()
            if v and v not in files:
                files.append(v)
    for f in files:
        r = _perm(f, want_mode=0o600, owner=root_uid)
        if r["detail"] == "missing" and f.startswith("/run/"):
            r["detail"] = "not loaded (RAM secrets are locked)"
        rows.append(dict(r, what="secret"))
    dirs = sorted({os.path.dirname(f) for f in files})
    for d in dirs:
        rows.append(dict(_perm(d, max_mode=0o700, owner=root_uid), what="secrets folder"))
    data = cfg.data_dir
    rows.append(dict(_perm(os.path.join(data, "state.json"), want_mode=0o600, owner=root_uid), what="collector state"))
    code = cfg.get("security", "code_dir", "") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loose = []
    for dirpath, dirnames, filenames in os.walk(code):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", "tests")]
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if st.st_mode & 0o022 or st.st_uid != root_uid:
                loose.append(os.path.relpath(p, code))
    rows.append({"path": code, "what": "dashboard code", "status": "crit" if loose else "ok",
                 "detail": ("writable by non-root: " + ", ".join(loose[:5])) if loose else "root-owned, read-only"})
    # Docker: the socket is root-equivalent, and so is everyone in the docker group
    sock = cfg.get("docker", "socket", "/run/docker.sock")
    if os.path.exists(sock):
        r = _perm(sock, max_mode=0o660, owner=root_uid)
        members = []
        for line in read_text(os.path.join(cfg.etc, "group"), "").splitlines():
            p = line.split(":")
            if len(p) >= 4 and p[0] == "docker":
                members = [m for m in p[3].split(",") if m]
        r["detail"] += "; docker group: %s" % (", ".join(members) or "nobody")
        if members and r["status"] == "ok":
            r["status"] = "warn"
            r["detail"] += " (root-equivalent)"
        rows.append(dict(r, what="Docker socket"))
    return rows


def container_risks(cfg):
    sock = cfg.get("docker", "socket", "/run/docker.sock")
    if not (os.path.exists(sock) and cfg.tristate("docker", "enabled") != "no"):
        return []
    out = []
    for c in unix_http_get_json(sock, "/containers/json?all=1", timeout=cfg.check_timeout):
        cid = c.get("Id", "")
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        try:
            info = unix_http_get_json(sock, "/containers/%s/json" % cid, timeout=cfg.check_timeout)
        except CheckError:
            continue
        hc = info.get("HostConfig") or {}
        risks = []
        if hc.get("Privileged"):
            risks.append(("crit", "privileged"))
        mounts = [m.get("Source", "") for m in info.get("Mounts") or []] + \
                 [b.split(":")[0] for b in hc.get("Binds") or []]
        if any(m.endswith("docker.sock") for m in mounts):
            risks.append(("warn", "Docker socket mounted (root-equivalent)"))
        if hc.get("PidMode") == "host":
            risks.append(("warn", "host PID namespace"))
        caps = {x.upper().replace("CAP_", "") for x in hc.get("CapAdd") or []}
        bad = sorted(caps & RISKY_CAPS)
        if bad:
            risks.append(("warn", "extra capabilities: " + ", ".join(bad)))
        if risks:
            level = "crit" if any(r[0] == "crit" for r in risks) else "warn"
            out.append({"name": name, "status": level, "risks": [r[1] for r in risks]})
    return out


# ----------------------------------------------------------------------- card

def collect(cfg, state):
    card = Card("security", "security", "Security")
    t = cfg.check_timeout
    d = card.data
    hour = cfg.getint("security", "slow_interval", 3600)

    upd = card.run("updates", _cached, 90, state, "updates", hour, updates, cfg)
    if upd:
        if upd.get("security"):
            card.status("warn")
        if upd.get("lists_age_days") is not None and upd["lists_age_days"] > 7:
            card.status("warn")
    d["updates"] = upd
    rb = card.run("reboot", reboot_needed, t, cfg)
    if rb and rb["required"]:
        card.status("warn")
    d["reboot"] = rb

    fw = card.run("firewall", firewall, t, cfg)
    if fw is not None and not fw["active"]:
        card.status("crit")
    d["firewall"] = fw
    sshc = card.run("sshd_config", ssh_config, t, cfg)
    d["ssh_config"] = sshc
    if sshc:
        card.status(sshc["status"])
    ports = card.run("listening ports", listening_ports, t, cfg) or {}
    published = card.run("docker ports", _cached, t + 2, state, "docker_ports", 300, docker_published, cfg) or {}
    exp = expected_ports(cfg, (sshc or {}).get("ports") or [22])
    rows = []
    for port, fams in ports.items():
        row = {"port": port, "families": fams, "hint": WELL_KNOWN.get(port), "expected": port in exp}
        if str(port) in published:
            row.update(docker=published[str(port)], status="warn", expected=True)
        else:
            row["status"] = "ok" if row["expected"] else "warn"
        card.status(row["status"])
        rows.append(row)
    d["ports"] = rows
    d["unexpected_ports"] = [r["port"] for r in rows if not r["expected"]]
    d["docker_bypasses_firewall"] = bool(published) and bool(fw and fw.get("name") == "ufw")

    logins = card.run("ssh logins", _cached, t * 2 + 2, state, "logins", 300, ssh_logins, cfg)
    if logins and logins["failed_24h"] > cfg.getint("security", "failed_login_warn", 20):
        card.status("warn")
    d["logins"] = logins
    d["fail2ban"] = card.run("fail2ban", fail2ban, t * 3, cfg)

    units = monitored_units(cfg)
    exp_rows = card.run("exposure", _cached, 120, state, "exposure", hour, exposure, cfg, units) or []
    for r in exp_rows:
        limit = DASHBOARD_UNITS.get(r["unit"])
        if limit is not None:
            r["dashboard"] = True
            r["status"] = "warn" if (r["score"] is None or r["score"] > limit) else "ok"
            card.status(r["status"])
        else:
            r["status"] = "unknown"  # informational: most stock services score 9+
    d["exposure"] = exp_rows

    perms = card.run("permissions", permissions, t, cfg) or []
    for r in perms:
        card.status(r["status"])
    d["permissions"] = perms
    risks = card.run("containers", _cached, t * 4, state, "containers", 600, container_risks, cfg) or []
    for r in risks:
        card.status(r["status"])
    d["containers"] = risks
    return card.to_dict()
