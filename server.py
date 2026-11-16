"""Local web dashboard for an MKS WiFi printer (Marlin MKS_WIFI_MODULE).

The browser can't open the module's raw TCP port, so this server keeps the
TCP connection, polls the printer and exposes a small JSON API plus the page.

Usage: python server.py [--printer 192.168.100.116] [--port 8000]
Then open http://localhost:8000
"""

import argparse
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CACHE_DIR = os.path.join(BASE_DIR, "gcode_cache")
MKS_TCP_PORT = 8080
POLL_INTERVAL = 2.0
TEMP_HISTORY_SECONDS = 15 * 60

TEMP_RE = re.compile(r"\b(T0|T1|T|B):\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)")
FILE_LINE_RE = re.compile(r"^([^\s;]+\.[A-Za-z0-9]{1,8}) 0(?: (.+))?$")
POLL_CODES = ("M105", "M997", "M27", "M992", "M994", "M991", "M20", "M115", "M114", "M211", "M360")
# Relayed-client requests answered by our own polling (see serve_client)
DEDUP_CODES = ("M105", "M997", "M27", "M992", "M994", "M991")
# M211 report: software endstops = the real travel limits of this printer
LIMITS_RE = re.compile(r"Min:\s*X(-?[\d.]+)\s+Y(-?[\d.]+)\s+Z(-?[\d.]+)\s+Max:\s*X(-?[\d.]+)\s+Y(-?[\d.]+)\s+Z(-?[\d.]+)")
POS_RE = re.compile(r"^X:(-?[\d.]+) Y:(-?[\d.]+) Z:(-?[\d.]+) E:(-?[\d.]+)")
FAN_RE = re.compile(r"^M106 P0 S(\d+)")
# Per-printer settings, kept in printer.json next to this file:
#   limits: travel per axis [min, max]; read from the printer with M211 (software endstops)
#   home:   where G28 leaves each axis; learned from the printer after a home
PRINTER_FILE = os.path.join(BASE_DIR, "printer.json")
DEFAULT_PRINTER = {"limits": {"x": [0, 230], "y": [0, 230], "z": [0, 250]},
                   "home": {"x": None, "y": None, "z": None}}


def load_printer_cfg():
    try:
        with open(PRINTER_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    out = json.loads(json.dumps(DEFAULT_PRINTER))
    for k in out:
        out[k].update(cfg.get(k, {}))
    return out


def save_printer_cfg(cfg):
    with open(PRINTER_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


PRINTER_CFG = load_printer_cfg()
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(name):
    base = os.path.basename(name.replace("\\", "/"))
    return SAFE_NAME_RE.sub("_", base) or "upload.gcode"


class Printer:
    """Owns the TCP link to the MKS WiFi module and the parsed printer state."""

    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.sock = None
        self.send_queue = deque()
        self.connected = False
        self.last_rx = 0.0
        self.state = "UNKNOWN"
        self.progress = None
        self.elapsed = None
        self.file_name = None
        self.file_size = None
        self.temps = {}
        self.pos = None            # last commanded position, from M114 (needs the WiFi M114 firmware fix)
        self.fan = None            # part cooling fan PWM 0-255
        self.info = {}             # firmware/machine info from M360 (needs the WiFi M360 firmware fix)
        self.info_at = None
        self.temp_history = deque()
        self.console = deque(maxlen=3000)
        self.console_seq = 0
        self.file_list = []
        self._listing = None
        self._list_event = threading.Event()
        self._rx_buf = b""
        self.uploading = None
        self.clients = []          # Cura (MKS plugin) connections relayed through us
        threading.Thread(target=self._run, daemon=True).start()

    # ---------- public API ----------

    def send(self, line, src="panel", poll=False):
        """Queue a line for the printer. src: who sent it (panel/cura); poll: status polling."""
        line = line.strip()
        if not line:
            return
        with self.lock:
            if self.uploading and poll:
                return  # Status polls during an upload would be stale (and are held anyway)
            self.send_queue.append(line)
        self._log(line, kind="tx", src=src, poll=poll)

    def refresh_files(self, timeout=6.0):
        self._list_event.clear()
        self.send("M20 1:/ L", poll=True)  # SD card, with long names
        self._list_event.wait(timeout)
        return list(self.file_list)

    def snapshot(self):
        with self.lock:
            # The ESP answers M27/M992/M994 from a cache it refreshes every ~5 s, and
            # Marlin stops refreshing them when idle, so they are stale unless printing.
            active = self.state in ("PRINTING", "PAUSE")
            return {
                "connected": self.connected,
                "host": self.host,
                "state": self.state,
                "progress": self.progress if active else None,
                "elapsed": self.elapsed if active else None,
                "file_name": self.file_name if active else None,
                "file_size": self.file_size if active else None,
                "temps": self.temps,
                "pos": self.pos,
                "fan": self.fan,
                "info": dict(self.info),
                "info_at": self.info_at,
                "cura_clients": len(self.clients),
                "limits": PRINTER_CFG["limits"],
                "home": PRINTER_CFG["home"],
                "uploading": dict(self.uploading) if self.uploading else None,
            }

    def history(self):
        with self.lock:
            return list(self.temp_history)

    def console_since(self, seq):
        with self.lock:
            return [c for c in self.console if c["seq"] > seq]

    # ---------- internals ----------

    def _log(self, text, kind="sys", src="printer", poll=False):
        """Console entry. kind: tx (sent), rx (from printer), sys (dashboard messages)."""
        with self.lock:
            self.console_seq += 1
            self.console.append({"seq": self.console_seq, "t": time.time(), "text": text,
                                 "kind": kind, "src": src, "poll": poll})

    def _connect(self):
        try:
            s = socket.create_connection((self.host, MKS_TCP_PORT), timeout=5)
            s.settimeout(0.2)
            self.sock = s
            self.connected = True
            self.last_rx = time.time()
            self._log("connected to %s:%d" % (self.host, MKS_TCP_PORT))
        except OSError as e:
            self.connected = False
            self._log("connect failed: %s (is Cura connected?)" % e)
            time.sleep(5)

    def _drop(self, why):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        if self.connected:
            self._log("disconnected: %s" % why)
        self.connected = False
        with self.lock:
            self.state = "UNKNOWN"

    def _run(self):
        next_poll = 0.0
        next_limits = 0.0
        while True:
            if not self.sock:
                self._connect()
                next_poll = next_limits = 0.0
                continue
            now = time.time()
            if now >= next_poll and not self.uploading:
                next_poll = now + POLL_INTERVAL
                for cmd in ("M105", "M997", "M27", "M992", "M994", "M114"):
                    self.send(cmd, poll=True)
                if now >= next_limits:   # limits and info rarely change: on connect, then every minute
                    next_limits = now + 60
                    self.send("M211", poll=True)
                    self.send("M360", poll=True)
            try:
                with self.lock:
                    # Traffic on 8080 during an HTTP upload stalls the module and breaks
                    # the transfer, so hold every line until the upload ends.
                    if self.uploading:
                        pending = []
                    else:
                        pending = list(self.send_queue)
                        self.send_queue.clear()
                for line in pending:
                    self.sock.sendall(line.encode("utf-8", "replace") + b"\r\n")
                    time.sleep(0.02)
                data = self.sock.recv(4096)
                if not data:
                    self._drop("closed by module")
                    time.sleep(2)
                    continue
                self.last_rx = time.time()
                self._relay(data)
                self._feed(data)
            except socket.timeout:
                if self.uploading:
                    self.last_rx = time.time()  # The module is quiet while it transfers
                elif time.time() - self.last_rx > 20:
                    self._drop("no data for 20 s")
            except OSError as e:
                self._drop(str(e))
                time.sleep(2)

    def _feed(self, data):
        self._rx_buf += data
        while b"\n" in self._rx_buf:
            raw, self._rx_buf = self._rx_buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._parse(line)

    def _parse(self, line):
        quiet = False
        if self._listing is not None:
            if line.startswith("End file list"):
                with self.lock:
                    self.file_list = self._listing
                self._listing = None
                self._list_event.set()
                return
            # Poll replies (ok, M997, M27, temperatures...) arrive mixed into the listing.
            # File lines look like "SHORT~1.GCO 0 Long name.gcode" (size is always 0).
            m = FILE_LINE_RE.match(line)
            if m:
                short, long_name = m.group(1), m.group(2) or m.group(1)
                if not short.upper().endswith(".DIR"):
                    self._listing.append({"short": short, "long": long_name})
                return
        if line.startswith("Begin file list"):
            self._listing = []
            return
        if line.startswith("Config:"):
            key, _, value = line[7:].partition(":")
            with self.lock:
                self.info[key] = value.strip()
                self.info_at = time.time()
            quiet = True
        elif LIMITS_RE.search(line):
            v = [float(x) for x in LIMITS_RE.search(line).groups()]
            limits = {"x": [v[0], v[3]], "y": [v[1], v[4]], "z": [v[2], v[5]]}
            if PRINTER_CFG["limits"] != limits:
                PRINTER_CFG["limits"] = limits
                save_printer_cfg(PRINTER_CFG)
            quiet = True
        elif POS_RE.match(line):
            m = POS_RE.match(line)
            with self.lock:
                self.pos = {k: float(v) for k, v in zip("xyze", m.groups())}
            quiet = True
        elif FAN_RE.match(line):
            with self.lock:
                self.fan = int(FAN_RE.match(line).group(1))
            quiet = True
        elif line.startswith("M997 "):
            st = line[5:].strip()
            with self.lock:
                self.state = st
            quiet = True
        elif line.startswith("M27 "):
            try:
                with self.lock:
                    self.progress = int(line[4:].strip())
            except ValueError:
                pass
            quiet = True
        elif line.startswith("M992 "):
            with self.lock:
                self.elapsed = line[5:].strip()
            quiet = True
        elif line.startswith("M994 "):
            body = line[5:].strip()
            name, _, size = body.rpartition(";")
            if not name.strip("/ "):
                return  # The ESP's cache before its first poll: ";0"
            with self.lock:
                self.file_name = (name or body).lstrip("/")
                try:
                    self.file_size = int(size)
                except ValueError:
                    pass
            quiet = True
        elif "T:" in line or "T0:" in line:
            temps = {}
            for key, cur, tgt in TEMP_RE.findall(line):
                temps[key] = {"cur": float(cur), "target": float(tgt)}
            if temps:
                if "T0" not in temps and "T" in temps:
                    temps["T0"] = temps["T"]
                temps.pop("T", None)
                now = time.time()
                with self.lock:
                    self.temps = temps
                    self.temp_history.append({"t": now, **{k: v["cur"] for k, v in temps.items()}})
                    while self.temp_history and now - self.temp_history[0]["t"] > TEMP_HISTORY_SECONDS:
                        self.temp_history.popleft()
                quiet = True
        elif line == "ok":
            quiet = True
        self._log(line, kind="rx", poll=quiet)

    def _relay(self, data):
        """Pass everything the module sends on to relayed clients, as the module would."""
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            try:
                c.sendall(data)
            except OSError:
                self._drop_client(c)

    def _drop_client(self, c):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
        try:
            c.close()
        except OSError:
            pass

    def serve_client(self, conn, addr):
        """One relayed client (e.g. Cura): its lines go to the printer through our link."""
        conn.settimeout(None)
        with self.lock:
            self.clients.append(conn)
        self._log("Cura connected (%s)" % addr[0])
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        code = line.split(" ")[0]
                        # Our own polling already asks for these every few seconds and every
                        # reply is relayed to the client, so don't ask the module twice.
                        if code in DEDUP_CODES:
                            continue
                        self.send(line, src="cura", poll=code in POLL_CODES)
        except OSError:
            pass
        self._drop_client(conn)
        self._log("Cura disconnected")

    def upload(self, name, data):
        """Forward a file to the module's HTTP upload endpoint (same as the Cura plugin).

        Sent in chunks so the page can show real progress: the module reads the body
        at the pace it passes data to the board, so bytes sent track the transfer."""
        path = "/upload?X-Filename=%s" % urllib.parse.quote(name)
        total = len(data)
        with self.lock:
            self.uploading = {"name": name, "sent": 0, "total": total, "phase": "sending", "started": time.time()}
        conn = http.client.HTTPConnection(self.host, 80, timeout=60)
        try:
            conn.connect()
            # A small send buffer keeps "bytes sent" close to what the module has read
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            conn.putrequest("POST", path)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(total))
            conn.endheaders()
            chunk = 4096
            for i in range(0, total, chunk):
                conn.send(data[i:i + chunk])
                with self.lock:
                    self.uploading["sent"] = min(total, i + chunk)
            with self.lock:
                self.uploading["phase"] = "finishing"
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        except OSError as e:
            return 0, "connection error: %s" % e
        finally:
            conn.close()
            with self.lock:
                self.uploading = None


printer = None


SDMAP_FILE = "_sdmap.json"


def cached_files():
    """G-code copies kept on this PC for the preview (the module can't send SD files back)."""
    out = []
    for n in sorted(os.listdir(CACHE_DIR)):
        p = os.path.join(CACHE_DIR, n)
        if os.path.isfile(p) and n != SDMAP_FILE:
            out.append({"name": n, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    return out


def load_sdmap():
    """SD short name (e.g. PAUSE~1.GCO) -> cached copy name (e.g. PAUSETEST.gcode)."""
    try:
        with open(os.path.join(CACHE_DIR, SDMAP_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sdmap(m):
    with open(os.path.join(CACHE_DIR, SDMAP_FILE), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)


DOS_ILLEGAL = set('|<>^+=?/[];,*"') | {chr(92)}


def dos_name(name):
    """The 8.3 name the board stores an upload under (Marlin's longName2DosName):
    the first 5 characters, upper-cased, plus "~1.GCO", or NAME.GCO if the dot comes first.
    So "HM2230_cube.gcode" and "HM2230_benchy.gcode" both become HM223~1.GCO."""
    out = ""
    for c in name:
        if c == ".":
            return out + ".GCO"
        o = ord(c)
        if o < 0x21 or o == 0x7F or c in DOS_ILLEGAL:
            c = "_"
        out += c.upper()
        if len(out) >= 5:
            return out + "~1.GCO"
    return out


def files_with_copies(files):
    """Attach the cached copy (if any) to each SD file."""
    sdmap, have = load_sdmap(), {c["name"] for c in cached_files()}
    return [dict(f, copy=sdmap.get(f["short"]) if sdmap.get(f["short"]) in have else None) for f in files]


class Handler(BaseHTTPRequestHandler):
    server_version = "MKSWeb/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _file(self, path, ctype):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _module_upload(self, name):
        """The MKS module's own upload endpoint, for Cura: keep a copy, forward, answer as the module."""
        data = self._body()
        cache_name = safe_name(name)
        with open(os.path.join(CACHE_DIR, cache_name), "wb") as f:
            f.write(data)
        printer._log("Cura upload: %s (%d bytes)" % (name, len(data)))
        status, body = printer.upload(name, data)
        if status == 200:
            sdmap = {k: v for k, v in load_sdmap().items() if v != cache_name}
            sdmap[dos_name(name)] = cache_name
            save_sdmap(sdmap)
        out = body.encode() if body else b""
        self.send_response(status or 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._json(printer.snapshot())
        if u.path == "/api/history":
            return self._json(printer.history())
        if u.path == "/api/console":
            since = int(q.get("since", ["0"])[0])
            return self._json(printer.console_since(since))
        if u.path == "/api/files":
            files = files_with_copies(printer.refresh_files())
            return self._json({"files": files, "cached": cached_files()})
        if u.path == "/api/cached":
            return self._json(cached_files())
        if u.path == "/api/gcode":
            name = safe_name(q.get("name", [""])[0])
            return self._file(os.path.join(CACHE_DIR, name), "text/plain; charset=utf-8")
        self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/upload" and "X-Filename" in q:
            return self._module_upload(q["X-Filename"][0])
        if u.path == "/api/upload":
            name = safe_name(q.get("name", ["upload.gcode"])[0])
            data = self._body()
            if len(data) < 1024:
                return self._json({"ok": False, "error": "File under 1 KB: the module drops these (known bug)."}, 400)
            with open(os.path.join(CACHE_DIR, name), "wb") as f:
                f.write(data)
            status, body = printer.upload(name, data)
            ok = status == 200 and '"err":0' in body.replace(" ", "")
            if status == 409:
                return self._json({"ok": False, "error": "Printer busy (409): uploads only work while idle."}, 409)
            short = dos_name(name)
            if ok:
                sdmap = {k: v for k, v in load_sdmap().items() if v != name}
                sdmap[short] = name  # Replaces an older upload that had the same 8.3 name
                save_sdmap(sdmap)
            return self._json({"ok": ok, "status": status, "body": body, "name": name, "short": short},
                              200 if ok else 502)
        try:
            payload = json.loads(self._body() or b"{}")
        except ValueError:
            payload = {}
        if u.path == "/api/home-learned":
            # The page saw where a G28 left these axes: remember it for this printer
            changed = False
            for ax, v in payload.items():
                if ax in ("x", "y", "z") and isinstance(v, (int, float)) and PRINTER_CFG["home"].get(ax) != v:
                    PRINTER_CFG["home"][ax] = round(float(v), 2)
                    changed = True
            if changed:
                save_printer_cfg(PRINTER_CFG)
                printer._log("Learned home position: %s" % PRINTER_CFG["home"])
            return self._json({"ok": True, "home": PRINTER_CFG["home"]})
        if u.path == "/api/cmd":
            for line in str(payload.get("cmd", "")).splitlines():
                printer.send(line)
            return self._json({"ok": True})
        if u.path == "/api/print":
            name = str(payload.get("name", "")).strip()
            if not name:
                return self._json({"ok": False, "error": "no file"}, 400)
            printer.send("M23 " + name)
            printer.send("M24")
            return self._json({"ok": True})
        actions = {"/api/pause": "M25", "/api/resume": "M24", "/api/stop": "M26"}
        if u.path in actions:
            printer.send(actions[u.path])
            return self._json({"ok": True})
        self.send_error(404)


def start_proxy(args):
    """Let Cura's MKS plugin talk to 127.0.0.1 instead of the printer: TCP 8080 for
    commands and HTTP 80 for uploads, both relayed over this server's single link."""
    def accept_loop(ls):
        while True:
            conn, addr = ls.accept()
            threading.Thread(target=printer.serve_client, args=(conn, addr), daemon=True).start()
    try:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.bind(("127.0.0.1", MKS_TCP_PORT))
        ls.listen(4)
        threading.Thread(target=accept_loop, args=(ls,), daemon=True).start()
        http80 = ThreadingHTTPServer(("127.0.0.1", 80), Handler)
        threading.Thread(target=http80.serve_forever, daemon=True).start()
        print("Cura relay: set the printer address in Cura to 127.0.0.1")
    except OSError as e:
        print("Cura relay disabled (%s)" % e)


def main():
    global printer
    ap = argparse.ArgumentParser()
    ap.add_argument("--printer", default="192.168.100.116", help="MKS WiFi module IP")
    ap.add_argument("--port", type=int, default=8000, help="local web port")
    ap.add_argument("--no-proxy", action="store_true", help="don't relay Cura on 127.0.0.1:8080 and :80")
    args = ap.parse_args()
    os.makedirs(CACHE_DIR, exist_ok=True)
    if not os.path.exists(PRINTER_FILE):
        save_printer_cfg(PRINTER_CFG)
    printer = Printer(args.printer)
    if not args.no_proxy:
        start_proxy(args)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # Windows resolves "localhost" to ::1 first; without an IPv6 listener every
    # request waits ~2 s for the IPv4 fallback.
    try:
        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        srv6 = V6Server(("::1", args.port), Handler)
        threading.Thread(target=srv6.serve_forever, daemon=True).start()
    except OSError:
        pass
    print("Dashboard: http://localhost:%d  (printer %s)" % (args.port, args.printer))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
