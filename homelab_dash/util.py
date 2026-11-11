"""Small helpers shared by the collector, the detector and the web server.

Everything here is defensive: every external call has a timeout, and
failures become exceptions with short, human-readable messages that the
collector puts on the card that owns the check.
"""

import http.client
import json
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

# A minimal, predictable environment for child processes (smartctl, systemctl, ...).
CHILD_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SYSTEMD_PAGER": "",
    "SYSTEMD_COLORS": "0",
}


class CheckError(Exception):
    """A check failed in an expected way; the message is shown on the card."""


def run_cmd(args, timeout=10, ok_codes=(0,), path_extra=None):
    """Run a command without a shell. Returns stdout as text.

    Raises CheckError on timeout, missing binary or an exit code that is not
    in ok_codes (pass ok_codes=None to accept any exit code).
    """
    env = dict(CHILD_ENV)
    if path_extra:
        env["PATH"] = path_extra + ":" + env["PATH"]
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        raise CheckError("command not found: %s" % args[0])
    except subprocess.TimeoutExpired:
        raise CheckError("%s timed out after %ss" % (os.path.basename(args[0]), timeout))
    except OSError as exc:
        raise CheckError("%s failed: %s" % (os.path.basename(args[0]), exc.strerror or exc))
    if ok_codes is not None and proc.returncode not in ok_codes:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = err[-1] if err else "exit code %d" % proc.returncode
        raise CheckError("%s: %s" % (os.path.basename(args[0]), msg[:200]))
    return proc.stdout.decode("utf-8", "replace"), proc.returncode


def read_text(path, default=None, limit=1 << 20):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        if default is not None:
            return default
        raise


def read_int(path, default=None):
    try:
        return int(read_text(path).strip())
    except (OSError, ValueError):
        return default


def run_with_timeout(func, timeout, *args, **kwargs):
    """Run func in a daemon thread; give up after `timeout` seconds.

    A hung thread is abandoned (daemon threads do not block exit) and the
    systemd TimeoutStartSec is the final backstop that kills the process.
    Returns (ok, result_or_error_message, elapsed_ms).
    """
    box = {}

    def target():
        try:
            box["result"] = func(*args, **kwargs)
        except CheckError as exc:
            box["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - a check must never crash the collector
            box["error"] = "%s: %s" % (type(exc).__name__, exc)

    start = time.monotonic()
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    elapsed = int((time.monotonic() - start) * 1000)
    if th.is_alive():
        return False, "timed out after %ss" % timeout, elapsed
    if "error" in box:
        return False, box["error"], elapsed
    return True, box.get("result"), elapsed


def atomic_write_json(path, obj, mode=0o644):
    """Write JSON so readers never see a half-written file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"), allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Secrets under these folders live in RAM (see unlock.py): a missing file there
# means "not unlocked since the last boot", not a mistake in the config.
RAM_SECRET_PREFIXES = ("/run/",)


def read_secret(path):
    """Read a secret file, refusing it unless it is a root-only regular file.

    The value is never logged or returned in an error message.
    """
    if not path:
        raise CheckError("no secret file configured")
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        if path.startswith(RAM_SECRET_PREFIXES):
            raise CheckError("secrets are locked (not in RAM since the last boot): "
                             "run the unlock command from docs/SECRETS-FROM-VAULT.md")
        raise CheckError("secret file missing: %s" % path)
    except PermissionError:
        raise CheckError("cannot read secret file: %s" % path)
    if not stat.S_ISREG(st.st_mode):
        raise CheckError("secret is not a regular file: %s" % path)
    if st.st_mode & 0o077:
        raise CheckError("secret file %s is mode %o; run: sudo chmod 600 %s"
                         % (path, stat.S_IMODE(st.st_mode), path))
    if st.st_uid != os.geteuid():
        raise CheckError("secret file %s must be owned by root" % path)
    with open(path, "r", encoding="utf-8") as fh:
        value = fh.read(4096).strip()
    if not value:
        raise CheckError("secret file is empty: %s" % path)
    return value


# ---------------------------------------------------------------- HTTP helpers

_NO_PROXY = urllib.request.ProxyHandler({})


def http_request(url, timeout=5, headers=None, method="GET", data=None,
                 verify_tls=True, max_bytes=2 << 20):
    """Small HTTP client. Ignores proxy env vars. Returns (status, body_bytes, headers)."""
    handlers = [_NO_PROXY]
    if url.startswith("https://") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers or {}))
    req.add_header("User-Agent", "homelab-dash")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(max_bytes), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(max_bytes)
        except Exception:  # noqa: BLE001
            pass
        return exc.code, body, dict(exc.headers or {})
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            raise CheckError("timed out after %ss" % timeout)
        raise CheckError("unreachable: %s" % (getattr(reason, "strerror", None) or reason))
    except socket.timeout:
        raise CheckError("timed out after %ss" % timeout)
    except (ConnectionError, OSError) as exc:
        raise CheckError("connection failed: %s" % (exc.strerror or exc))


def http_json(url, **kwargs):
    status, body, _ = http_request(url, **kwargs)
    if status == 401 or status == 403:
        raise CheckError("HTTP %d (check the API key/password)" % status)
    if status >= 400:
        raise CheckError("HTTP %d" % status)
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise CheckError("response was not JSON")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def unix_http_get_json(sock_path, path, timeout=5):
    """GET a JSON document over a Unix socket (used for the Docker Engine API).

    Only GET is ever sent: this module has no way to change containers.
    """
    conn = _UnixHTTPConnection(sock_path, timeout)
    try:
        conn.request("GET", path, headers={"Host": "docker"})
        resp = conn.getresponse()
        body = resp.read(8 << 20)
    except FileNotFoundError:
        raise CheckError("socket not found: %s" % sock_path)
    except PermissionError:
        raise CheckError("permission denied on %s" % sock_path)
    except socket.timeout:
        raise CheckError("timed out after %ss" % timeout)
    except OSError as exc:
        raise CheckError("%s: %s" % (sock_path, exc.strerror or exc))
    finally:
        conn.close()
    if resp.status >= 400:
        raise CheckError("HTTP %d from %s" % (resp.status, sock_path))
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise CheckError("invalid JSON from %s" % sock_path)


def tcp_probe(host, port, timeout=3):
    start = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except socket.timeout:
        raise CheckError("%s:%s timed out" % (host, port))
    except OSError as exc:
        raise CheckError("%s:%s %s" % (host, port, exc.strerror or exc))
    return int((time.monotonic() - start) * 1000)


def worst(statuses):
    order = {"ok": 0, "unknown": 1, "warn": 2, "error": 3, "crit": 4}
    result = "ok"
    for s in statuses:
        if order.get(s, 1) > order.get(result, 0):
            result = s
    return result


def level(value, warn, crit):
    if value is None:
        return "unknown"
    if crit is not None and value >= crit:
        return "crit"
    if warn is not None and value >= warn:
        return "warn"
    return "ok"


class Card:
    """One dashboard card made of several sub-checks.

    Each sub-check runs with its own timeout. A failing sub-check adds an
    error line to this card only; the card is marked "error" only if every
    sub-check failed.
    """

    def __init__(self, card_id, card_type, title):
        self.id = card_id
        self.type = card_type
        self.title = title
        self.data = {}
        self.errors = []
        self.statuses = []
        self._ran = 0
        self._failed = 0

    def run(self, name, func, timeout, *args, **kwargs):
        self._ran += 1
        ok, result, _ms = run_with_timeout(func, timeout, *args, **kwargs)
        if not ok:
            self._failed += 1
            self.errors.append({"check": name, "error": str(result)[:300]})
            return None
        return result

    def status(self, s):
        self.statuses.append(s)

    def to_dict(self):
        if self._ran and self._failed == self._ran:
            status = "error"
        else:
            status = worst(self.statuses + (["warn"] if self.errors else []))
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "status": status,
            "errors": self.errors,
            "data": self.data,
        }
