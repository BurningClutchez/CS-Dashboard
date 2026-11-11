"""Small, safe edits to a draft config for the stats cards that need a secret
(Pi-hole, Jellyfin, Emby, Plex). Used by the quick installer through
detect.py --draft-stats / --draft-enable, so the editing is testable here.

detect.py comments a stats section out (``# [pihole]``) when the service
wasn't reachable from the server, e.g. a Pi-hole on a macvlan IP. These
helpers report that, and switch a section on with a URL you give.
"""

import re

KINDS = {
    #  kind       section           type        file key         file name
    "pihole":   ("pihole",         None,       "password_file", "pihole.pass"),
    "jellyfin": ("media:jellyfin", "jellyfin", "key_file",      "jellyfin.key"),
    "emby":     ("media:emby",     "emby",     "key_file",      "emby.key"),
    "plex":     ("media:plex",     "plex",     "key_file",      "plex.token"),
}
LABELS = {"pihole": "Pi-hole", "jellyfin": "Jellyfin", "emby": "Emby", "plex": "Plex"}
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d+)?(/[^\s]*)?$")
# what detect.py writes when it can't work out an address (http://PIHOLE-IP, None)

def is_placeholder(url):
    url = (url or "").strip()
    return bool(not url or url.lower() == "none" or re.search(r"[A-Z]+-IP\b", url))


def _header_re(section):
    return re.compile(r"^(# )?\[%s\]\s*$" % re.escape(section))


def _block(lines, section):
    """(start, end, commented) of the section's header..last line, or None."""
    rx = _header_re(section)
    for i, line in enumerate(lines):
        m = rx.match(line)
        if m:
            j = i + 1
            while j < len(lines) and lines[j].strip() and not re.match(r"^(# )?\[", lines[j]):
                j += 1
            return i, j, bool(m.group(1))
    return None


def scan(text):
    """{kind: {"state": on|off|missing, "url": str|None, "reason": str}}"""
    lines = text.splitlines()
    out = {}
    for kind, (section, _t, _k, _f) in KINDS.items():
        blk = _block(lines, section)
        if not blk:
            out[kind] = {"state": "missing", "url": None, "reason": "not found on this server"}
            continue
        start, end, commented = blk
        url = None
        for line in lines[start + 1:end]:
            m = re.match(r"^(# )?url\s*=\s*(\S*)", line)
            if m:
                url = None if is_placeholder(m.group(2)) else m.group(2)
        reason = ""
        k = start - 1
        while k >= 0 and lines[k].startswith("#") and not _header_re(section).match(lines[k]):
            if "NOT reachable" in lines[k] or "not reachable" in lines[k]:
                reason = "not reachable from this server (a macvlan IP?)"
            k -= 1
        if commented and not reason:
            reason = "switched off in the draft"
        out[kind] = {"state": "off" if commented else "on", "url": url, "reason": reason}
    return out


def enable(text, kind, url, conf_dir):
    """Switch a stats section on (uncomment it, or add it) with this URL."""
    if kind not in KINDS:
        raise ValueError("unknown stats card %r (use pihole, jellyfin, emby or plex)" % kind)
    if is_placeholder(url):
        raise ValueError("%r is a placeholder: give the real address, e.g. http://192.168.1.53:8081" % url)
    if not _URL_RE.match(url or ""):
        raise ValueError("%r is not a URL like http://192.168.1.53:8081" % url)
    section, mtype, key, fname = KINDS[kind]
    lines = text.splitlines()
    blk = _block(lines, section)
    if blk:
        start, end, _commented = blk
        new = []
        for line in lines[start:end]:
            if line.startswith("# "):
                line = line[2:]
            if re.match(r"^url\s*=", line):
                line = "url = %s" % url
            new.append(line)
        lines[start:end] = new
    else:
        lines += ["", "# %s: added by the installer" % LABELS[kind], "[%s]" % section]
        if mtype:
            lines.append("type = %s" % mtype)
        lines.append("url = %s" % url)
        if kind == "pihole":
            lines.append("version = auto")
        lines.append("%s = %s/secrets/%s" % (key, conf_dir.rstrip("/"), fname))
        if mtype:
            lines.append("show_titles = no")
    return "\n".join(lines) + "\n"
