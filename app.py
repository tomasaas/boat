"""Start the boat:  python app.py   then open http://<pi-hostname>.local:8000

Listens on every network interface (ethernet and wifi, IPv4 and IPv6), so the GUI
can be opened from a PC on the same network. --host 127.0.0.1 for this machine only.

Serves index.html (tabs: Styring / Konfigurasjon / Kobling) and a tiny JSON API:
    GET  /config         current config
    POST /config         save config.json and restart the motors
    POST /drive          {"surge": -1..1, "yaw": -1..1} -> status
    GET  /camera         camera status
    POST /camera         {"on": true/false}: off = no video over 4G until it's turned on again
    GET  /video          H.264 in fragmented MP4, endless, for MediaSource. 409 when the camera is off
The GUI posts /drive 10 times/s. If that stops for TIMEOUT seconds, the motors stop.
Connections are kept alive (HTTP/1.1): over 4G, a new TCP connection for every /drive costs data.
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
from camera import Camera, codec_of

TIMEOUT = 0.5
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
log = logging.getLogger("app")

boat = None
camera = None
boat_lock = threading.Lock()
last_drive = 0.0  # time of the last /drive, 0 = stopped by watchdog


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    timeout = 300  # close idle connections (Caddy closes its own after 2 min)

    def do_GET(self):
        if self.path == "/":
            with open(INDEX, "rb") as f:
                self.reply(f.read(), "text/html; charset=utf-8")
        elif self.path == "/config":
            self.reply_json(boat.config)
        elif self.path == "/camera":
            self.reply_json(camera.status())
        elif self.path == "/video":
            self.stream_video()
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
            elif self.path == "/camera":
                cfg = dict(boat.config, camera=dict(boat.config["camera"], on=bool(body["on"])))
                save_config(cfg)
                log.info("Camera %s", "on" if cfg["camera"]["on"] else "off")
                boat.config = cfg
                camera.configure(cfg["camera"])
                self.reply_json(camera.status())
            elif self.path == "/config":
                body.setdefault("camera", {})["on"] = boat.config["camera"]["on"]  # owned by /camera
                cfg = clean_config(body)
                save_config(cfg)
                log.info("New config: %s", cfg)
                boat.close()
                boat = Boat(cfg)
                last_drive = 0.0
                camera.configure(cfg["camera"])
                self.reply_json(cfg)
            else:
                self.send_error(404)

    def stream_video(self):
        if not camera.config["on"]:
            return self.send_error(409, "Camera is off")
        chunks = camera.subscribe()
        try:
            first = next(chunks, None)  # waits for ffmpeg to start
            if first is None:
                return self.send_error(503, "No video")
            self.close_connection = True  # no Content-Length: the stream ends when the connection does
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("X-Codec", codec_of(first))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(first)
            for chunk in chunks:
                self.wfile.write(chunk)
        except OSError:
            pass  # the browser left, or turned the camera off
        finally:
            chunks.close()

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
    global boat, camera
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="::", help="default: all interfaces")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="log every throttle change")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    boat = Boat(load_config())
    camera = Camera(boat.config["camera"])
    threading.Thread(target=watchdog, daemon=True).start()
    Server.address_family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    server = Server((args.host, args.port), Handler)
    log.info("Open %s", "  or  ".join(urls(args.host, args.port)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        camera.close()
        boat.close()


if __name__ == "__main__":
    main()
