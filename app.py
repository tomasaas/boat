"""Start the boat:  python app.py   then open http://<pi-hostname>.local:8000

Listens on every network interface (ethernet and wifi, IPv4 and IPv6), so the GUI
can be opened from a PC on the same network. --host 127.0.0.1 for this machine only.

Serves index.html (tabs: Styring / Konfigurasjon / Kobling) and a tiny JSON API:
    GET  /config         current config
    POST /config         save config.json and restart the motors
    POST /drive          {"surge": -1..1, "yaw": -1..1} -> status
The GUI posts /drive 10 times/s. If that stops for TIMEOUT seconds, the motors stop.
"""
import argparse
import json
import logging
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from boat import Boat, clean_config, load_config, save_config

TIMEOUT = 0.5
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
log = logging.getLogger("app")

boat = None
boat_lock = threading.Lock()
last_drive = 0.0  # time of the last /drive, 0 = stopped by watchdog


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            with open(INDEX, "rb") as f:
                self.reply(f.read(), "text/html; charset=utf-8")
        elif self.path == "/config":
            self.reply_json(boat.config)
        else:
            self.send_error(404)

    def do_POST(self):
        global boat, last_drive
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "{}")
        with boat_lock:
            if self.path == "/drive":
                boat.drive(float(body["surge"]), float(body["yaw"]))
                last_drive = time.monotonic()
                self.reply_json(boat.status())
            elif self.path == "/config":
                cfg = clean_config(body)
                save_config(cfg)
                log.info("New config: %s", cfg)
                boat.close()
                boat = Boat(cfg)
                last_drive = 0.0
                self.reply_json(cfg)
            else:
                self.send_error(404)

    def reply_json(self, data):
        self.reply(json.dumps(data).encode(), "application/json")

    def reply(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # don't log every request (10/s)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # IPv4 too
        super().server_bind()


def urls(host, port):
    """Addresses the GUI can be opened on, for the startup log."""
    if host not in ("::", "0.0.0.0"):
        return [f"http://{'localhost' if host == '127.0.0.1' else host}:{port}"]
    found = [f"http://{socket.gethostname()}.local:{port}"]
    try:  # Linux: every IPv4 address on every interface
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        found += [f"http://{ip}:{port}" for ip in out.stdout.split() if out.returncode == 0 and ":" not in ip]
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def watchdog():
    global last_drive
    while True:
        time.sleep(0.1)
        with boat_lock:
            if last_drive and time.monotonic() - last_drive > TIMEOUT:
                log.warning("No contact with GUI for %.1f s - stopping motors", TIMEOUT)
                boat.stop()
                last_drive = 0.0


def main():
    global boat
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="::", help="default: all interfaces")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="log every throttle change")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    boat = Boat(load_config())
    threading.Thread(target=watchdog, daemon=True).start()
    Server.address_family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    server = Server((args.host, args.port), Handler)
    log.info("Open %s", "  or  ".join(urls(args.host, args.port)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        boat.close()


if __name__ == "__main__":
    main()
