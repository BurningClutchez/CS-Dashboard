"""Service-specific stats: Pi-hole, Jellyfin/Emby/Plex streams, game server players.

Credentials are read from root-only files (see util.read_secret) and are only
ever sent in request headers/bodies to the configured local URL. They are
never written to status.json.
"""

import json
import socket
import struct
import time

from .services import find_processes
from .util import Card, CheckError, http_request, read_secret


def _base(url):
    return url.strip().rstrip("/")


# ----------------------------------------------------------------- Pi-hole

def _pihole_v6(cfg, url, state, timeout, verify):
    pw_file = cfg.get("pihole", "password_file", "").strip()

    def get(path, sid):
        headers = {"Accept": "application/json"}
        if sid:
            headers["X-FTL-SID"] = sid
        return http_request(url + path, timeout=timeout, headers=headers, verify_tls=verify)

    def login():
        pw = read_secret(pw_file) if pw_file else ""
        body = json.dumps({"password": pw}).encode()
        code, raw, _ = http_request(url + "/api/auth", method="POST", data=body, timeout=timeout,
                                    headers={"Content-Type": "application/json"}, verify_tls=verify)
        if code == 404:
            return None  # not v6
        if code == 429:
            raise CheckError("Pi-hole: too many sessions/logins (HTTP 429); wait a few minutes")
        if code in (400, 401):
            raise CheckError("Pi-hole rejected the password (check %s)" % pw_file)
        try:
            sess = json.loads(raw.decode()).get("session") or {}
        except ValueError:
            raise CheckError("Pi-hole /api/auth returned no JSON")
        if not sess.get("valid"):
            raise CheckError("Pi-hole login failed")
        return sess.get("sid") or ""

    # Re-use one session (kept in the root-only state file) so we never use up
    # Pi-hole's limited session slots or re-hash the password every minute.
    sid = state.get("pihole_sid")
    code, raw, _ = get("/api/stats/summary", sid)
    if code == 404 and sid is None:
        # Could be v5, or v6 that needs a login first; find out by logging in.
        sid = login()
        if sid is None:
            return None
        code, raw, _ = get("/api/stats/summary", sid)
    elif code == 401:
        sid = login()
        if sid is None:
            return None
        code, raw, _ = get("/api/stats/summary", sid)
    if code == 404:
        return None
    if code in (401, 403):
        raise CheckError("Pi-hole API needs a password: set password_file in [pihole]")
    if code >= 400:
        raise CheckError("Pi-hole HTTP %d" % code)
    state["pihole_sid"] = sid or ""
    s = json.loads(raw.decode())
    q, cl, gr = s.get("queries") or {}, s.get("clients") or {}, s.get("gravity") or {}
    blocking = None
    c2, raw2, _ = get("/api/dns/blocking", sid)
    if c2 == 200:
        blocking = json.loads(raw2.decode()).get("blocking")
    return {
        "version": 6,
        "queries": q.get("total"),
        "blocked": q.get("blocked"),
        "pct_blocked": round(q.get("percent_blocked") or 0, 1),
        "cached": q.get("cached"),
        "forwarded": q.get("forwarded"),
        "clients": cl.get("active"),
        "domains_blocked": gr.get("domains_being_blocked"),
        "gravity_updated": gr.get("last_update"),
        "blocking": blocking,
    }


def _pihole_v5(cfg, url, timeout, verify):
    tok_file = cfg.get("pihole", "password_file", "").strip()
    q = "?summaryRaw"
    if tok_file:
        q += "&auth=" + read_secret(tok_file)  # v5 only accepts the token in the URL
    code, raw, _ = http_request(url + "/admin/api.php" + q, timeout=timeout, verify_tls=verify)
    if code >= 400:
        raise CheckError("Pi-hole v5 API HTTP %d" % code)
    try:
        s = json.loads(raw.decode())
    except ValueError:
        raise CheckError("Pi-hole v5 API returned no JSON")
    if not isinstance(s, dict) or "dns_queries_today" not in s:
        raise CheckError("Pi-hole v5 API returned no stats (API token wrong or missing?)")
    return {
        "version": 5,
        "queries": s.get("dns_queries_today"),
        "blocked": s.get("ads_blocked_today"),
        "pct_blocked": round(float(s.get("ads_percentage_today") or 0), 1),
        "cached": s.get("queries_cached"),
        "forwarded": s.get("queries_forwarded"),
        "clients": s.get("unique_clients"),
        "domains_blocked": s.get("domains_being_blocked"),
        "gravity_updated": ((s.get("gravity_last_updated") or {}).get("absolute")),
        "blocking": s.get("status"),
    }


def pihole(cfg, state):
    url = _base(cfg.get("pihole", "url"))
    timeout = min(cfg.check_timeout, 6)
    verify = cfg.getbool("pihole", "verify_tls", True)
    version = (cfg.get("pihole", "version", "auto") or "auto").strip()
    pstate = state.setdefault("pihole", {})
    data = None
    if version in ("auto", "6"):
        data = _pihole_v6(cfg, url, pstate, timeout, verify)
        if data is None and version == "6":
            raise CheckError("no Pi-hole v6 API at %s" % url)
    if data is None:
        data = _pihole_v5(cfg, url, timeout, verify)
    data["status"] = "ok" if data.get("blocking") in ("enabled", None) else "warn"
    return data


def collect_pihole(cfg, state):
    card = Card("pihole", "pihole", cfg.get("pihole", "label", "Pi-hole"))
    res = card.run("pihole", pihole, cfg.check_timeout * 2 + 2, cfg, state)
    if res:
        card.data.update(res)
        card.status(res["status"])
    return card.to_dict()


# ------------------------------------------------------------ media servers

def jellyfin(cfg, sec):
    url = _base(sec["url"])
    key = read_secret(sec.get("key_file", "").strip())
    kind = sec.get("type").strip()
    headers = {"Accept": "application/json"}
    if kind == "jellyfin":
        headers["Authorization"] = 'MediaBrowser Token="%s"' % key
    else:
        headers["X-Emby-Token"] = key
    timeout = min(cfg.check_timeout, 6)
    verify = sec.get("verify_tls", "yes").lower() not in ("no", "false", "0")
    code, raw, _ = http_request(url + "/Sessions?activeWithinSeconds=960", headers=headers,
                                timeout=timeout, verify_tls=verify)
    if code in (401, 403):
        raise CheckError("%s rejected the API key (HTTP %d)" % (kind, code))
    if code >= 400:
        raise CheckError("%s HTTP %d" % (kind, code))
    sessions = json.loads(raw.decode())
    streams = []
    for s in sessions:
        item = s.get("NowPlayingItem")
        if not item:
            continue
        ti = s.get("TranscodingInfo") or {}
        streams.append({
            "title": (item.get("SeriesName") + " - " if item.get("SeriesName") else "") + (item.get("Name") or ""),
            "user": s.get("UserName"),
            "transcode": bool(ti) and not ti.get("IsVideoDirect", False),
            "paused": bool((s.get("PlayState") or {}).get("IsPaused")),
        })
    return streams


def plex(cfg, sec):
    url = _base(sec["url"])
    token = read_secret(sec.get("key_file", "").strip())
    verify = sec.get("verify_tls", "yes").lower() not in ("no", "false", "0")
    code, raw, _ = http_request(url + "/status/sessions", timeout=min(cfg.check_timeout, 6),
                                headers={"X-Plex-Token": token, "Accept": "application/json"},
                                verify_tls=verify)
    if code in (401, 403):
        raise CheckError("Plex rejected the token (HTTP %d)" % code)
    if code >= 400:
        raise CheckError("Plex HTTP %d" % code)
    mc = (json.loads(raw.decode()) or {}).get("MediaContainer") or {}
    streams = []
    for m in mc.get("Metadata") or []:
        title = m.get("title") or ""
        if m.get("grandparentTitle"):
            title = m["grandparentTitle"] + " - " + title
        streams.append({
            "title": title,
            "user": (m.get("User") or {}).get("title"),
            "transcode": "TranscodeSession" in m and
                         (m["TranscodeSession"].get("videoDecision") == "transcode"),
            "paused": (m.get("Player") or {}).get("state") == "paused",
        })
    return streams


def collect_media(cfg, state):
    cards = []
    for name, sec in cfg.sections("media"):
        kind = sec.get("type").strip()
        card = Card("media-" + name, "media", sec.get("label", kind.capitalize()))
        func = plex if kind == "plex" else jellyfin
        streams = card.run(kind, func, cfg.check_timeout + 2, cfg, sec)
        if streams is not None:
            show = sec.get("show_titles", "no").lower() in ("yes", "true", "1", "on")
            card.data = {
                "server": kind,
                "active": sum(1 for s in streams if not s["paused"]),
                "paused": sum(1 for s in streams if s["paused"]),
                "transcoding": sum(1 for s in streams if s["transcode"]),
                # Titles and user names are personal; only shown if you opt in.
                "streams": [{"title": s["title"], "user": s["user"], "transcode": s["transcode"],
                             "paused": s["paused"]} for s in streams] if show else [],
            }
            card.status("ok")
        cards.append(card.to_dict())
    return cards


# -------------------------------------------------------------- game servers

def _cstr(buf, i):
    end = buf.index(b"\0", i)
    return buf[i:end].decode("utf-8", "replace"), end + 1


def a2s_info(host, port, timeout=3):
    """Steam A2S_INFO query (Valheim, CS2, Rust, ARK, 7DTD, TF2, ...)."""
    req = b"\xff\xff\xff\xffTSource Engine Query\x00"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(req, (host, int(port)))
            data, _ = s.recvfrom(4096)
            if data[4:5] == b"A":  # challenge (anti-spoofing), resend with it
                s.sendto(req + data[5:9], (host, int(port)))
                data, _ = s.recvfrom(4096)
        except socket.timeout:
            raise CheckError("no reply to Steam query on %s:%s/udp" % (host, port))
        except OSError as exc:
            raise CheckError("query %s:%s failed: %s" % (host, port, exc.strerror or exc))
    if data[:4] != b"\xff\xff\xff\xff" or data[4:5] != b"I":
        raise CheckError("unexpected Steam query reply")
    i = 6
    name, i = _cstr(data, i)
    game_map, i = _cstr(data, i)
    _folder, i = _cstr(data, i)
    game, i = _cstr(data, i)
    i += 2  # app id
    players, max_players, bots = data[i], data[i + 1], data[i + 2]
    return {"server_name": name, "map": game_map, "game": game,
            "players": players - bots if players >= bots else players,
            "max_players": max_players}


def _varint(n):
    n &= 0xFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += struct.pack("B", b | (0x80 if n else 0))
        if not n:
            return out


def _read_varint(sock):
    num = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise CheckError("connection closed")
        num |= (b[0] & 0x7F) << (7 * i)
        if not b[0] & 0x80:
            return num
    raise CheckError("bad varint")


def minecraft_status(host, port, timeout=3):
    """Minecraft Java 'server list ping' (the same thing the multiplayer menu does)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            h = host.encode()
            body = b"\x00" + _varint(765) + _varint(len(h)) + h + struct.pack(">H", int(port)) + b"\x01"
            s.sendall(_varint(len(body)) + body + b"\x01\x00")
            _length = _read_varint(s)
            _pid = _read_varint(s)
            n = _read_varint(s)
            if n > 1 << 20:
                raise CheckError("reply too large")
            buf = b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    break
                buf += chunk
    except socket.timeout:
        raise CheckError("Minecraft server on %s:%s did not answer" % (host, port))
    except OSError as exc:
        raise CheckError("Minecraft %s:%s: %s" % (host, port, exc.strerror or exc))
    st = json.loads(buf.decode("utf-8", "replace"))
    pl = st.get("players") or {}
    return {"players": pl.get("online"), "max_players": pl.get("max"),
            "game": "Minecraft", "version": (st.get("version") or {}).get("name")}


def game(cfg, sec):
    proto = sec.get("protocol").strip()
    host = sec.get("host", "127.0.0.1").strip()
    port = int(sec.get("port") or 0)
    timeout = float(sec.get("timeout", 3))
    out = {}
    match = sec.get("process", "").strip()
    if match:
        pids = find_processes(cfg, match, sec.get("user", "").strip() or None)
        out["process"] = bool(pids)
        if not pids:
            out.update({"status": "crit", "online": False, "detail": "process not running"})
            return out
    start = time.monotonic()
    if proto == "a2s":
        out.update(a2s_info(host, port, timeout))
    elif proto == "minecraft":
        out.update(minecraft_status(host, port, timeout))
    elif proto == "none":
        if not match:
            raise CheckError("protocol = none needs process =")
    else:
        from .util import tcp_probe
        tcp_probe(host, port, timeout)
    out["ms"] = int((time.monotonic() - start) * 1000)
    out["online"] = True
    out["status"] = "ok"
    return out


def collect_games(cfg, state):
    games = cfg.sections("game")
    card = Card("games", "games", "Game servers")
    rows = []
    for name, sec in games:
        res = card.run(sec.get("label", name), game, float(sec.get("timeout", 3)) * 2 + 2, cfg, sec)
        row = {"name": name, "label": sec.get("label", name)}
        if res:
            row.update(res)
        else:
            row.update({"status": "crit", "online": False, "detail": card.errors[-1]["error"]})
            card.errors.pop()  # shown on the row itself; one dead server isn't a card error
            card._failed -= 1
        card.status(row["status"])
        rows.append(row)
    card.data["servers"] = rows
    card.data["players"] = sum(r.get("players") or 0 for r in rows)
    return card.to_dict()
