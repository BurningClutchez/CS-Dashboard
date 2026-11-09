"""System card: OS, uptime, CPU, RAM, temperatures, load, failed units, log errors."""

import glob
import os
import time

from .util import Card, CheckError, level, read_int, read_text, run_cmd, worst


def os_info(cfg):
    info = {}
    try:
        for line in read_text(os.path.join(cfg.etc, "os-release")).splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip().strip('"')
    except OSError:
        pass
    kernel = read_text(os.path.join(cfg.proc, "sys/kernel/osrelease"), "").strip()
    host = cfg.get("general", "hostname") or read_text(
        os.path.join(cfg.proc, "sys/kernel/hostname"), "").strip()
    return {"os": info.get("PRETTY_NAME", "Linux"), "kernel": kernel, "hostname": host}


def cpu_info(cfg):
    text = read_text(os.path.join(cfg.proc, "cpuinfo"), "")
    found, cores = {}, 0
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key == "processor":
            cores += 1
        elif key in ("model name", "hardware", "model") and key not in found:
            found[key] = val.strip()
    # x86 has "model name"; Raspberry Pi has "Model"; on x86 "model" is just a number
    model = found.get("model name") or found.get("hardware") or found.get("model")
    return {"model": model or "unknown", "cores": cores or (os.cpu_count() or 1)}


def uptime(cfg):
    return float(read_text(os.path.join(cfg.proc, "uptime")).split()[0])


def loadavg(cfg):
    parts = read_text(os.path.join(cfg.proc, "loadavg")).split()
    return [float(parts[0]), float(parts[1]), float(parts[2])]


def _cpu_times(cfg):
    line = read_text(os.path.join(cfg.proc, "stat")).splitlines()[0]
    vals = [int(x) for x in line.split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals[:8])  # exclude guest (already counted in user)
    return total, idle


def cpu_percent(cfg, state):
    """CPU busy % since the previous run (so it averages the last minute)."""
    total, idle = _cpu_times(cfg)
    prev = state.get("cpu")
    now = time.time()
    if not prev or now - prev.get("t", 0) > 600 or total <= prev["total"]:
        time.sleep(0.5)
        prev = {"total": total, "idle": idle}
        total, idle = _cpu_times(cfg)
    state["cpu"] = {"t": now, "total": total, "idle": idle}
    dt = total - prev["total"]
    if dt <= 0:
        return 0.0
    return round(100.0 * (1 - (idle - prev["idle"]) / dt), 1)


def meminfo(cfg):
    vals = {}
    for line in read_text(os.path.join(cfg.proc, "meminfo")).splitlines():
        k, _, rest = line.partition(":")
        try:
            vals[k] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            continue
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    if not total:
        raise CheckError("MemTotal missing from meminfo")
    swap_t = vals.get("SwapTotal", 0)
    swap_f = vals.get("SwapFree", 0)
    return {
        "total": total,
        "available": avail,
        "used": total - avail,
        "pct": round(100.0 * (total - avail) / total, 1),
        "swap_total": swap_t,
        "swap_used": swap_t - swap_f,
        "swap_pct": round(100.0 * (swap_t - swap_f) / swap_t, 1) if swap_t else 0.0,
    }


def temperatures(cfg):
    out = []
    base = os.path.join(cfg.sys, "class/hwmon")
    for hw in sorted(glob.glob(os.path.join(base, "hwmon*"))):
        chip = read_text(os.path.join(hw, "name"), "hwmon").strip()
        for inp in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            milli = read_int(inp)
            if milli is None or milli <= -40000 or milli >= 150000:
                continue  # unplugged sensor or bogus value
            stem = inp[: -len("_input")]
            label = read_text(stem + "_label", "").strip() or os.path.basename(stem)
            out.append({"chip": chip, "label": label, "c": round(milli / 1000.0, 1)})
    if not out:  # Raspberry Pi and some ARM boards only expose thermal zones
        for tz in sorted(glob.glob(os.path.join(cfg.sys, "class/thermal/thermal_zone*"))):
            milli = read_int(os.path.join(tz, "temp"))
            if milli is None:
                continue
            out.append({"chip": read_text(os.path.join(tz, "type"), "thermal").strip(),
                        "label": os.path.basename(tz), "c": round(milli / 1000.0, 1)})
    return out


def failed_units(cfg):
    text, _ = run_cmd(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
                      timeout=cfg.check_timeout, path_extra=cfg.bin_path)
    return [line.split()[0] for line in text.splitlines() if line.strip()]


def journal_errors(cfg):
    text, _ = run_cmd(["journalctl", "-p", "err", "--since", "-1h", "-q", "--no-pager",
                       "-o", "cat", "-n", "1000"],
                      timeout=cfg.check_timeout, ok_codes=(0, 1), path_extra=cfg.bin_path)
    return sum(1 for line in text.splitlines() if line.strip())


def collect(cfg, state):
    card = Card("system", "system", "System")
    t = cfg.check_timeout
    d = card.data
    d.update(card.run("os", os_info, t, cfg) or {})
    d["cpu_model"] = None
    info = card.run("cpuinfo", cpu_info, t, cfg)
    cores = 1
    if info:
        d["cpu_model"], d["cores"] = info["model"], info["cores"]
        cores = info["cores"]
    d["uptime_s"] = card.run("uptime", uptime, t, cfg)

    load = card.run("load", loadavg, t, cfg)
    d["load"] = load
    if load:
        d["load_status"] = worst([level(load[1] / cores, cfg.threshold("load_warn"),
                                        cfg.threshold("load_crit"))])
        card.status(d["load_status"])

    cpu = card.run("cpu", cpu_percent, t, cfg, state)
    d["cpu_pct"] = cpu
    if cpu is not None:
        d["cpu_status"] = level(cpu, cfg.threshold("cpu_warn"), cfg.threshold("cpu_crit"))
        card.status(d["cpu_status"])

    mem = card.run("memory", meminfo, t, cfg)
    d["mem"] = mem
    if mem:
        mem["status"] = level(mem["pct"], cfg.threshold("mem_warn"), cfg.threshold("mem_crit"))
        card.status(mem["status"])

    temps = card.run("temperatures", temperatures, t, cfg)
    d["temps"] = temps
    if temps:
        for s in temps:
            s["status"] = level(s["c"], cfg.threshold("temp_warn"), cfg.threshold("temp_crit"))
            card.status(s["status"])
        d["temp_max"] = max(s["c"] for s in temps)

    if cfg.getbool("system", "failed_units", True):
        failed = card.run("failed units", failed_units, t, cfg)
        d["failed_units"] = failed
        if failed:
            card.status("warn")
    if cfg.getbool("system", "journal_errors", True):
        d["journal_errors_1h"] = card.run("journal", journal_errors, t, cfg)
    return card.to_dict()
