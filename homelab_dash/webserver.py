"""Tiny read-only web server.

Serves a fixed list of files and nothing else: no directory listing, no
path handling, no query parsing, no request bodies, GET and HEAD only.
"""

import argparse
import ipaddress
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ConfigError, default_config_path, load

MAX_FILE = 8 << 20
MAX_CONCURRENT = 16

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("icon-192.png", "image/png"),
}
DATA = {
    "/status.json": "status.json",
    "/history.json": "history.json",
}


def build_routes(web_root, data_dir):
    routes = {}
    for url, (name, ctype) in STATIC.items():
        routes[url] = (os.path.join(web_root, name), ctype, "no-cache")
    for url, name in DATA.items():
        routes[url] = (os.path.join(data_dir, name), "application/json", "no-store")
    return routes


def host_allowed(host_header, names):
    """Defeat DNS-rebinding: only answer to IP literals and our own host names."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return host.rstrip(".") in names


def allowed_names(cfg):
    names = {"localhost"}
    raw = cfg.getlist("web", "allowed_hosts", "auto")
    if "auto" in raw or not raw:
        h = socket.gethostname().lower()
        short = h.split(".")[0]
        names.update({h, short})
        for suffix in (".local", ".lan", ".home", ".home.arpa", ".localdomain", ".internal"):
            names.add(short + suffix)
    names.update(n.lower().rstrip(".") for n in raw if n != "auto")
    return names


class Handler(BaseHTTPRequestHandler):
    server_version = "homelab-dash"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15  # seconds per request; slow clients get dropped
    routes = {}
    names = set()

    def version_string(self):
        return "homelab-dash"  # no Python version in the Server header

    def _headers(self, code, ctype, length, cache):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()

    def _plain(self, code, text, extra=None):
        body = (text + "\n").encode()
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, head):
        if not host_allowed(self.headers.get("Host"), self.names):
            return self._plain(421, "unknown host name; add it to allowed_hosts in [web]")
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        route = self.routes.get(path)
        if route is None:
            return self._plain(404, "not found")
        fpath, ctype, cache = route
        try:
            with open(fpath, "rb") as fh:
                body = fh.read(MAX_FILE + 1)
        except FileNotFoundError:
            if ctype == "application/json":
                return self._plain(503, "no data yet: the collector has not written %s"
                                   % os.path.basename(fpath))
            return self._plain(404, "not found")
        except OSError:
            return self._plain(500, "cannot read file")
        if len(body) > MAX_FILE:
            return self._plain(500, "file too large")
        self._headers(200, ctype, len(body), cache)
        if not head:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._serve(head=False)

    def do_HEAD(self):  # noqa: N802
        self._serve(head=True)

    def _refuse(self):
        # The request body (if any) is never read, so close the connection instead of
        # parsing the leftover body as a new request.
        self.close_connection = True
        self._plain(405, "read-only: only GET and HEAD are allowed",
                    {"Allow": "GET, HEAD", "Connection": "close"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _refuse

    def log_request(self, code="-", size="-"):
        # Successful requests are not logged (a phone polls every 30 s).
        try:
            if int(code) >= 400:
                super().log_request(code, size)
        except (TypeError, ValueError):
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__(*a, **kw)

    def server_bind(self):
        # HTTPServer.server_bind() does a reverse-DNS lookup (getfqdn); skip it.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name = "homelab-dash"
        self.server_port = self.server_address[1]

    def handle_error(self, request, client_address):
        # A phone that locks or a browser that gives up mid-answer is normal:
        # don't fill the journal with tracebacks for it.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # too many open connections: drop
            return
        try:
            super().process_request(request, client_address)
        except Exception:  # noqa: BLE001
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(cfg, bind=None, port=None):
    handler = type("BoundHandler", (Handler,), {
        "routes": build_routes(cfg.get("web", "web_root"), cfg.get("web", "data_dir")),
        "names": allowed_names(cfg),
    })
    return Server((bind or cfg.get("web", "bind"), cfg.getint("web", "port") if port is None else port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="homelab-dash read-only web server")
    ap.add_argument("--config", default=default_config_path())
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    srv = make_server(cfg)
    host, port = srv.server_address[:2]
    print("serving %d fixed paths on %s:%d" % (len(srv.RequestHandlerClass.routes), host, port))
    sys.stdout.flush()
    try:
        srv.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    return 0
