"""Disks card: free space per mount, SMART health per drive, mdadm / ZFS state."""

import fnmatch
import json
import os
import re
import stat
import time

from .config import REAL_FS
from .util import Card, CheckError, level, read_text, run_cmd

HIDDEN_PREFIXES = ("/home", "/root", "/run/user")


def _unescape(path):
    # /proc/mounts escapes spaces etc. as octal (\040)
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def list_mounts(cfg):
    wanted = cfg.getlist("disks", "mounts")
    excludes = cfg.getlist("disks", "exclude_mounts")
    text = read_text(os.path.join(cfg.proc, "self/mounts"))
    seen = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = _unescape(parts[0]), _unescape(parts[1]), parts[2]
        if wanted and wanted != ["auto"]:
            if mnt not in wanted:
                continue
        else:
            if fs not in REAL_FS:
                continue
            if any(fnmatch.fnmatch(mnt, pat) for pat in excludes):
                continue
        # The same filesystem can be mounted many times (bind mounts, btrfs
        # subvolumes, Docker volumes). Keep the shortest mount point per device.
        key = (dev, fs)
        if key not in seen or len(mnt) < len(seen[key]["mount"]):
            seen[key] = {"device": dev, "mount": mnt, "fs": fs}
    mounts = sorted(seen.values(), key=lambda m: m["mount"])
    if wanted and wanted != ["auto"]:
        found = {m["mount"] for m in mounts}
        for missing in [w for w in wanted if w not in found]:
            mounts.append({"device": None, "mount": missing, "fs": None, "missing": True})
    return mounts


def usage(cfg, mount):
    path = cfg.fsroot + mount["mount"]
    if mount.get("missing"):
        raise CheckError("%s is not mounted" % mount["mount"])
    try:
        st = os.stat(path)
        if mount["mount"].startswith(HIDDEN_PREFIXES) and stat.S_IMODE(st.st_mode) == 0:
            raise CheckError("%s is hidden by ProtectHome= (see INSTALL.md, 'separate /home')"
                             % mount["mount"])
        vfs = os.statvfs(path)
    except OSError as exc:
        raise CheckError("%s: %s" % (mount["mount"], exc.strerror or exc))
    size = vfs.f_blocks * vfs.f_frsize
    avail = vfs.f_bavail * vfs.f_frsize
    used = size - vfs.f_bfree * vfs.f_frsize
    pct = round(100.0 * used / (used + avail), 1) if (used + avail) else 0.0
    return {"size": size, "used": used, "avail": avail, "pct": pct}


# --------------------------------------------------------------------- SMART

def smart_devices(cfg):
    setting = cfg.getlist("disks", "smart")
    if setting == ["off"] or setting == ["no"]:
        return []
    if setting and setting != ["auto"]:
        out = []
        for item in setting:
            dev, _, dtype = item.partition(":")
            out.append({"name": dev, "type": dtype or None})
        return out
    text, _ = run_cmd(["smartctl", "--scan", "--json=c"], timeout=cfg.check_timeout,
                      ok_codes=None, path_extra=cfg.bin_path)
    try:
        data = json.loads(text)
    except ValueError:
        raise CheckError("smartctl --scan returned no JSON (smartmontools >= 7.0 needed)")
    return [{"name": d["name"], "type": d.get("type")} for d in data.get("devices", [])]


def _attr(table, attr_id):
    for row in table or []:
        if row.get("id") == attr_id:
            return (row.get("raw") or {}).get("value")
    return None


def parse_smart(data):
    """Turn smartctl --json output into a small, display-ready dict."""
    out = {
        "model": data.get("model_name") or data.get("model_family") or
                 (data.get("device") or {}).get("name"),
        "protocol": (data.get("device") or {}).get("protocol"),
        "capacity": (data.get("user_capacity") or {}).get("bytes"),
        "rotational": (data.get("rotation_rate") or 0) > 0,
    }
    exit_status = (data.get("smartctl") or {}).get("exit_status", 0)
    if exit_status & 1:
        msgs = [m.get("string", "") for m in (data.get("smartctl") or {}).get("messages", [])]
        raise CheckError("smartctl: %s" % ("; ".join(msgs) or "command line error"))
    if data.get("smart_support", {}).get("available") is False:
        out["status"] = "unknown"
        out["note"] = "SMART not supported (USB bridge? try smart = /dev/sdX:sat)"
        return out
    passed = (data.get("smart_status") or {}).get("passed")
    out["passed"] = passed
    out["temp"] = (data.get("temperature") or {}).get("current")
    out["hours"] = (data.get("power_on_time") or {}).get("hours")
    table = (data.get("ata_smart_attributes") or {}).get("table")
    out["reallocated"] = _attr(table, 5)
    out["pending"] = _attr(table, 197)
    out["uncorrectable"] = _attr(table, 198)
    nv = data.get("nvme_smart_health_information_log")
    if nv:
        out["pct_used"] = nv.get("percentage_used")
        out["media_errors"] = nv.get("media_errors")
        out["critical_warning"] = nv.get("critical_warning")
        out["spare"] = nv.get("available_spare")
        if out["hours"] is None:
            out["hours"] = nv.get("power_on_hours")
        if out["temp"] is None:
            out["temp"] = nv.get("temperature")
    if passed is None and (exit_status & 2 or exit_status & 4):
        msgs = [m.get("string", "") for m in (data.get("smartctl") or {}).get("messages", [])]
        raise CheckError("smartctl could not read the drive: %s" % ("; ".join(msgs)[:150] or exit_status))
    return out


def smart_status(info, cfg):
    s = "ok"
    reasons = []
    if info.get("passed") is False:
        return "crit", ["SMART overall health FAILED"]
    if info.get("critical_warning"):
        s = "crit"
        reasons.append("NVMe critical warning %s" % info["critical_warning"])
    for key, label in (("reallocated", "reallocated sectors"), ("pending", "pending sectors"),
                       ("uncorrectable", "uncorrectable sectors"), ("media_errors", "media errors")):
        if info.get(key):
            s = "crit" if (s == "crit" or key in ("pending", "uncorrectable")) else "warn"
            reasons.append("%s %s" % (info[key], label))
    if info.get("pct_used") is not None and info["pct_used"] >= 90:
        s = "warn" if s == "ok" else s
        reasons.append("%s%% of rated life used" % info["pct_used"])
    t = level(info.get("temp"), cfg.threshold("disk_temp_warn"), cfg.threshold("disk_temp_crit"))
    if t in ("warn", "crit"):
        reasons.append("hot: %s C" % info["temp"])
        if t == "crit" or s == "ok":
            s = t
    if info.get("status") == "unknown":
        s = "unknown"
    return s, reasons


def smart_one(cfg, dev, cache):
    """Check one drive. Re-uses the cached result for smart_interval seconds and
    never spins up a sleeping HDD (-n standby)."""
    now = time.time()
    interval = cfg.getint("disks", "smart_interval", 1800)
    cached = cache.get(dev["name"])
    if cached and now - cached.get("t", 0) < interval:
        return dict(cached["info"], cached=True, checked_at=cached["t"])
    args = ["smartctl", "--json=c", "-i", "-H", "-A"]
    if cfg.getbool("disks", "smart_skip_standby", True):
        args += ["-n", "standby"]
    if dev.get("type"):
        args += ["-d", dev["type"]]
    args.append(dev["name"])
    text, code = run_cmd(args, timeout=cfg.check_timeout * 2, ok_codes=None,
                         path_extra=cfg.bin_path)
    try:
        data = json.loads(text)
    except ValueError:
        raise CheckError("smartctl returned no JSON (exit %d)" % code)
    power = (data.get("power_mode") or "").upper() if isinstance(data.get("power_mode"), str) else ""
    msgs = " ".join(m.get("string", "") for m in (data.get("smartctl") or {}).get("messages", []))
    if "STANDBY" in msgs.upper() or "SLEEP" in msgs.upper() or power in ("STANDBY", "SLEEP"):
        if cached:
            return dict(cached["info"], standby=True, cached=True, checked_at=cached["t"])
        return {"model": None, "standby": True, "status": "ok",
                "note": "drive asleep; not woken up for SMART", "checked_at": now}
    info = parse_smart(data)
    info["status"], info["reasons"] = smart_status(info, cfg)
    cache[dev["name"]] = {"t": now, "info": info}
    return dict(info, checked_at=now)


# ---------------------------------------------------------------------- RAID

def mdstat(cfg):
    path = os.path.join(cfg.proc, "mdstat")
    if not os.path.exists(path):
        return []
    arrays = []
    cur = None
    for line in read_text(path).splitlines():
        m = re.match(r"^(md\S+)\s*:\s*(\S+)\s+(\S+)?", line)
        if m:
            cur = {"name": m.group(1), "type": "mdadm", "state": m.group(2),
                   "level": m.group(3) or "", "status": "ok", "detail": ""}
            if m.group(2) != "active":
                cur["status"] = "crit"
            arrays.append(cur)
            continue
        if cur is None:
            continue
        mm = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", line)
        if mm:
            cur["detail"] = "%s/%s disks [%s]" % (mm.group(2), mm.group(1), mm.group(3))
            if "_" in mm.group(3):
                cur["status"] = "crit"
                cur["state"] = "degraded"
        mm = re.search(r"(resync|recovery|reshape|check)\s*=\s*([\d.]+%)", line)
        if mm:
            cur["detail"] += " %s %s" % (mm.group(1), mm.group(2))
            if cur["status"] == "ok":
                cur["status"] = "warn" if mm.group(1) != "check" else "ok"
    return arrays


def zpools(cfg):
    if not os.path.exists(os.path.join(cfg.proc, "spl/kstat/zfs")):
        return []
    text, _ = run_cmd(["zpool", "list", "-H", "-p", "-o", "name,health,size,alloc,capacity"],
                      timeout=cfg.check_timeout, path_extra=cfg.bin_path)
    pools = []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 5:
            continue
        health = p[1]
        pools.append({
            "name": p[0], "type": "zfs", "state": health,
            "detail": "%s%% full" % p[4].rstrip("%"),
            "status": {"ONLINE": "ok", "DEGRADED": "warn"}.get(health, "crit"),
        })
    return pools


def collect(cfg, state):
    card = Card("disks", "disks", "Disks")
    t = cfg.check_timeout
    mounts = card.run("mounts", list_mounts, t, cfg) or []
    rows = []
    for m in mounts:
        u = card.run("df " + m["mount"], usage, t, cfg, m)
        row = {"mount": m["mount"], "fs": m["fs"], "device": m["device"]}
        if u:
            row.update(u)
            row["status"] = level(u["pct"], cfg.threshold("disk_warn"), cfg.threshold("disk_crit"))
        else:
            row["status"] = "error"
        card.status(row["status"])
        rows.append(row)
    card.data["mounts"] = rows

    devs = card.run("smart scan", smart_devices, t, cfg) if cfg.tristate("disks", "smart") != "no" else []
    cache = state.setdefault("smart", {})
    smart = []
    for dev in devs or []:
        info = card.run("SMART " + dev["name"], smart_one, t * 2 + 2, cfg, dev, cache)
        row = {"device": dev["name"]}
        if info:
            row.update(info)
        else:
            row["status"] = "error"
            row["error"] = card.errors[-1]["error"]
        card.status(row.get("status", "unknown"))
        smart.append(row)
    card.data["smart"] = smart
    # forget drives that disappeared
    for name in list(cache):
        if name not in {d["name"] for d in devs or []}:
            cache.pop(name, None)

    raid = []
    if cfg.tristate("disks", "raid") != "no":
        raid += card.run("mdadm", mdstat, t, cfg) or []
        raid += card.run("zfs", zpools, t, cfg) or []
    for r in raid:
        card.status(r["status"])
    card.data["raid"] = raid
    if rows:
        card.data["max_pct"] = max((r.get("pct") or 0) for r in rows)
    return card.to_dict()
