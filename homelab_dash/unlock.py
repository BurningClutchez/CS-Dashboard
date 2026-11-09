"""Load secrets from an Ansible vault into RAM only.

    sudo -v
    ansible-vault view /path/to/vault.yml | sudo python3 -I -B /opt/homelab-dash/unlock.py

Reads the *decrypted* vault (flat YAML `key: value`) from a pipe and writes the
keys listed in [vault] map to root-only files in a tmpfs folder (default
/run/homelab-dash-secrets). Nothing touches persistent storage, and everything
is gone after a reboot or `unlock.py --lock`.

It refuses to write to a folder that is not on tmpfs/ramfs, and never prints a
secret value (only key and file names).
"""

import argparse
import json
import os
import re
import stat
import sys

from .config import ConfigError, default_config_path, load

DEFAULT_DIR = "/run/homelab-dash-secrets"
DEFAULT_MAP = "pihole_password=pihole.pass jellyfin_api_key=jellyfin.key plex_token=plex.token"
RAM_FS = {"tmpfs", "ramfs"}
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(?:\s+(.*))?$")
_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class UnlockError(Exception):
    pass


def parse_map(text):
    out = {}
    for item in re.split(r"[\s,]+", (text or "").strip()):
        if not item:
            continue
        key, sep, fname = item.partition("=")
        if not sep or not _FILE.match(fname) or fname.startswith("."):
            raise UnlockError("bad map entry %r: use vault_key=file.name" % item)
        out[key] = fname
    return out


def _scalar(key, raw):
    """One-line YAML scalar -> str. Raises (without the value) on anything fancier."""
    raw = raw.strip()
    if raw.startswith('"'):
        m = re.match(r'^("(?:[^"\\]|\\.)*")\s*(#.*)?$', raw)
        if not m:
            raise UnlockError("vault key %s: unterminated double-quoted value" % key)
        try:
            return json.loads(m.group(1))
        except ValueError:
            raise UnlockError("vault key %s: unsupported escape in double-quoted value" % key)
    if raw.startswith("'"):
        m = re.match(r"^'((?:[^']|'')*)'\s*(#.*)?$", raw)
        if not m:
            raise UnlockError("vault key %s: unterminated single-quoted value" % key)
        return m.group(1).replace("''", "'")
    if not raw or raw[0] in "|>[{&*!":
        raise UnlockError("vault key %s: use a single-line plain or quoted value "
                          "(block, list, anchor and tagged values are not supported)" % key)
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def parse_vault(text, wanted):
    """Return {key: value} for the wanted keys plus the count of other top-level keys."""
    found, others = {}, 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.strip() in ("---", "..."):
            continue
        if line[0] in " \t-":
            continue  # nested or list content of keys we don't use
        m = _KEY.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2) or ""
        if key not in wanted:
            others += 1
            continue
        value = _scalar(key, raw)
        if not value:
            raise UnlockError("vault key %s is empty" % key)
        if "\n" in value or len(value) > 4096:
            raise UnlockError("vault key %s: value must be one line under 4 KB" % key)
        found[key] = value
    return found, others


def fs_type(path, mounts_file="/proc/self/mounts"):
    """Filesystem type that holds `path` (longest matching mount point)."""
    path = os.path.realpath(path)
    best, best_fs = "", None
    with open(mounts_file, "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            mnt = parts[1].replace("\\040", " ")
            if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                best, best_fs = mnt, parts[2]
    return best_fs


def swap_warning(swaps_file="/proc/swaps"):
    try:
        with open(swaps_file, "r", encoding="utf-8") as fh:
            devs = [line.split()[0] for line in fh.readlines()[1:] if line.strip()]
    except OSError:
        return None
    disk = [d for d in devs if not d.startswith("/dev/zram")]
    if disk:
        return ("warning: swap on %s is active. Under memory pressure RAM pages (including "
                "these secrets) can be written there unless that swap is encrypted. "
                "zram swap or no swap avoids this." % ", ".join(disk))
    return None


def prepare_dir(path, check_fs=True, mounts_file="/proc/self/mounts"):
    parent = os.path.dirname(path.rstrip("/")) or "/"
    if check_fs:
        fs = fs_type(path if os.path.exists(path) else parent, mounts_file)
        if fs not in RAM_FS:
            raise UnlockError("%s is on %s, not RAM (tmpfs). Refusing: secrets must not "
                              "touch persistent storage. Use a folder on tmpfs (normally under /run)." % (path, fs))
    if not os.path.lexists(path):
        os.mkdir(path, 0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise UnlockError("%s exists and is not a directory" % path)
    if st.st_uid != os.geteuid():
        raise UnlockError("%s is not owned by root" % path)
    os.chmod(path, 0o700)


def write_secret(directory, fname, value):
    final = os.path.join(directory, fname)
    tmp = os.path.join(directory, ".%s.new" % fname)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.replace(tmp, final)


def lock(directory):
    removed = []
    if not os.path.isdir(directory):
        return removed
    for name in sorted(os.listdir(directory)):
        p = os.path.join(directory, name)
        if os.path.isfile(p) and not os.path.islink(p):
            size = os.path.getsize(p)
            with open(p, "r+b") as fh:  # overwrite before unlinking
                fh.write(b"\0" * size)
            os.unlink(p)
            removed.append(name)
    return removed


def settings(cfg):
    return (cfg.get("vault", "dir", DEFAULT_DIR) or DEFAULT_DIR).rstrip("/"), \
        parse_map(cfg.get("vault", "map", DEFAULT_MAP) or DEFAULT_MAP)


def main(argv=None, stdin=None, check_fs=True):
    ap = argparse.ArgumentParser(description="Load homelab-dash secrets from a decrypted "
                                 "Ansible vault on stdin into RAM (tmpfs) only.")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--lock", action="store_true", help="wipe the secrets from RAM")
    ap.add_argument("--status", action="store_true", help="list which secrets are loaded")
    args = ap.parse_args(argv)
    stdin = stdin or sys.stdin
    try:
        cfg = load(args.config)
        directory, mapping = settings(cfg)
        if os.geteuid() != 0 and check_fs:
            raise UnlockError("run with sudo: the files must be owned by root")
        if args.lock:
            removed = lock(directory)
            print("locked: removed %s" % (", ".join(removed) if removed else "nothing (already locked)"))
            return 0
        if args.status:
            for key, fname in mapping.items():
                p = os.path.join(directory, fname)
                print("  %-22s %-16s %s" % (key, fname, "loaded" if os.path.isfile(p) else "NOT loaded"))
            return 0
        if stdin.isatty():
            raise UnlockError("pipe the decrypted vault into this command, e.g.\n"
                              "  ansible-vault view /path/vault.yml | sudo python3 -I -B "
                              "/opt/homelab-dash/unlock.py")
        text = stdin.read(1 << 20)
        found, others = parse_vault(text, set(mapping))
        if not found:
            raise UnlockError("none of the mapped keys (%s) were found in the vault; "
                              "check [vault] map" % ", ".join(mapping))
        prepare_dir(directory, check_fs=check_fs)
        for key, value in found.items():
            write_secret(directory, mapping[key], value)
        del text, found  # drop our references to the plaintext
    except (UnlockError, ConfigError) as exc:
        print("unlock: %s" % exc, file=sys.stderr)
        return 1
    loaded = [k for k in mapping if os.path.isfile(os.path.join(directory, mapping[k]))]
    print("loaded into RAM (%s): %s" % (directory, ", ".join("%s -> %s" % (k, mapping[k]) for k in loaded)))
    missing = [k for k in mapping if k not in loaded]
    if missing:
        print("not in this vault: %s" % ", ".join(missing))
    if others:
        print("ignored %d other vault key(s)" % others)
    warn = swap_warning() if check_fs else None
    if warn:
        print(warn, file=sys.stderr)
    return 0
