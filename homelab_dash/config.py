"""Load /etc/homelab-dash/config.ini (INI format, comments allowed)."""

import configparser
import os
import re

DEFAULTS = {
    "general": {
        "hostname": "",
        "data_dir": "/var/lib/homelab-dash",
        "interval": "60",
        "history_hours": "24",
        "check_timeout": "8",
        "deadline": "30",
        "stale_after": "180",
        # Overrides used only by the offline tests:
        "proc": "/proc",
        "sys": "/sys",
        "etc": "/etc",
        "fsroot": "",
        "bin_path": "",
    },
    "web": {
        "bind": "0.0.0.0",
        "port": "8088",
        "web_root": "/opt/homelab-dash/web",
        "data_dir": "/var/lib/homelab-dash",
        "allowed_hosts": "auto",
    },
    "thresholds": {
        "disk_warn": "80", "disk_crit": "90",
        "temp_warn": "75", "temp_crit": "85",
        "disk_temp_warn": "50", "disk_temp_crit": "60",
        "mem_warn": "90", "mem_crit": "97",
        "cpu_warn": "90", "cpu_crit": "98",
        "load_warn": "1.5", "load_crit": "3.0",
    },
    "system": {
        "failed_units": "yes",
        "journal_errors": "yes",
    },
    "disks": {
        "mounts": "auto",
        "exclude_mounts": "/boot/efi /snap/* /var/lib/docker/* /run/* /sys/* /proc/* /dev/*",
        "smart": "auto",
        "smart_interval": "1800",
        "smart_skip_standby": "yes",
        "raid": "auto",
    },
    "network": {
        "interfaces": "auto",
        "dns_test": "example.com",
        "tailscale": "auto",
    },
    "docker": {
        "enabled": "auto",
        "socket": "/run/docker.sock",
        "hide": "",
    },
}

REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "fuse.mergerfs", "fuse.shfs",
    "bcachefs", "nfs", "nfs4", "cifs", "smb3",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, path):
        self.path = path
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
        cp.read_dict(DEFAULTS)
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cp.read_file(fh)
            except FileNotFoundError:
                raise ConfigError("config file not found: %s" % path)
            except configparser.Error as exc:
                raise ConfigError("config file %s: %s" % (path, exc))
        self.cp = cp

    # ---- typed accessors
    def get(self, section, key, fallback=None):
        return self.cp.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=0):
        try:
            return self.cp.getint(section, key, fallback=fallback)
        except ValueError:
            raise ConfigError("[%s] %s must be a whole number" % (section, key))

    def getfloat(self, section, key, fallback=0.0):
        try:
            return self.cp.getfloat(section, key, fallback=fallback)
        except ValueError:
            raise ConfigError("[%s] %s must be a number" % (section, key))

    def getbool(self, section, key, fallback=False):
        try:
            return self.cp.getboolean(section, key, fallback=fallback)
        except ValueError:
            raise ConfigError("[%s] %s must be yes or no" % (section, key))

    def getlist(self, section, key, fallback=""):
        raw = self.cp.get(section, key, fallback=fallback) or ""
        return [x for x in re.split(r"[\s,]+", raw.strip()) if x]

    def tristate(self, section, key):
        """auto / yes / no."""
        v = (self.get(section, key, "auto") or "auto").strip().lower()
        if v in ("1", "true", "on", "yes"):
            return "yes"
        if v in ("0", "false", "off", "no"):
            return "no"
        return "auto"

    def sections(self, prefix):
        out = []
        for name in self.cp.sections():
            if name.startswith(prefix + ":"):
                short = name.split(":", 1)[1].strip()
                if not _NAME_RE.match(short):
                    raise ConfigError("bad section name [%s]: use letters, digits, - _ . @" % name)
                out.append((short, self.cp[name]))
        return out

    def has(self, section):
        return self.cp.has_section(section)

    # ---- paths
    @property
    def proc(self):
        return self.get("general", "proc")

    @property
    def sys(self):
        return self.get("general", "sys")

    @property
    def etc(self):
        return self.get("general", "etc")

    @property
    def fsroot(self):
        return self.get("general", "fsroot") or ""

    @property
    def data_dir(self):
        return self.get("general", "data_dir")

    @property
    def bin_path(self):
        return self.get("general", "bin_path") or None

    @property
    def check_timeout(self):
        return max(1, self.getint("general", "check_timeout", 8))

    def threshold(self, key):
        return self.getfloat("thresholds", key)

    def validate(self):
        """Fail early with a clear message instead of halfway through a run."""
        self.getint("general", "interval")
        self.getint("general", "history_hours")
        port = self.getint("web", "port")
        if not 1 <= port <= 65535:
            raise ConfigError("[web] port must be 1-65535")
        for key in DEFAULTS["thresholds"]:
            self.threshold(key)
        for name, sec in self.sections("service"):
            t = sec.get("type", "").strip()
            if t not in ("systemd", "docker", "http", "tcp", "process"):
                raise ConfigError("[service:%s] type must be systemd, docker, http, tcp or process" % name)
            need = {"systemd": "unit", "docker": "container", "http": "url",
                    "tcp": "port", "process": "match"}[t]
            if not sec.get(need, "").strip():
                raise ConfigError("[service:%s] needs %s =" % (name, need))
        for name, sec in self.sections("media"):
            if sec.get("type", "").strip() not in ("jellyfin", "emby", "plex"):
                raise ConfigError("[media:%s] type must be jellyfin, emby or plex" % name)
            if not sec.get("url", "").strip():
                raise ConfigError("[media:%s] needs url =" % name)
        for name, sec in self.sections("game"):
            if sec.get("protocol", "").strip() not in ("a2s", "minecraft", "tcp", "none"):
                raise ConfigError("[game:%s] protocol must be a2s, minecraft, tcp or none" % name)
            if sec.get("protocol").strip() != "none" and not sec.get("port", "").strip():
                raise ConfigError("[game:%s] needs port =" % name)
        if self.has("pihole") and not self.get("pihole", "url", ""):
            raise ConfigError("[pihole] needs url =")
        return self


def load(path):
    return Config(path).validate()


def default_config_path():
    return os.environ.get("HLDASH_CONFIG", "/etc/homelab-dash/config.ini")
