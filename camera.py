"""The USB camera: H.264 video for the GUI, shared by every viewer.

    camera = Camera(load_config()["camera"])
    for chunk in camera.subscribe():   # init segment + keyframe, then one MP4 fragment per frame
        send(chunk)
    camera.configure(new_config)       # ffmpeg restarts with the new settings within 0.5 s
    camera.close()

ffmpeg runs only while the camera is on and somebody watches, and stops IDLE_STOP s after the
last viewer leaves, so no video crosses 4G unless it is shown. The Pi 5 has no H.264 encoder in
hardware, so libx264 encodes in software, at the lowest CPU priority: the motors come first.
- PIXEL_BUDGET caps the fps per resolution (1440p: 15 fps), so the Pi isn't asked for too much.
- The bitrate follows from resolution and fps (kbps()), so the picture stays sharp without tuning.
- If too few frames arrive while the CPU is full, the resolution steps down one notch.
- If the Pi gets hot, the video falls back to 480p until it has cooled down.
- If ffmpeg dies it is restarted; the motors don't notice, it's a separate process.
Without a USB camera it shows ffmpeg's test pattern (simulated). Without ffmpeg there's no video.
"""
import glob
import logging
import os
import queue
import shutil
import struct
import subprocess
import threading
import time

# Height -> size. All four are MJPEG modes of the camera, which sends 15 or 30 fps.
RESOLUTIONS = {480: (640, 480), 720: (1280, 720), 1080: (1920, 1080), 1440: (2560, 1440)}
PIXEL_BUDGET = 1920 * 1080 * 30  # pixels/s the Pi decodes and encodes with CPU to spare
BASE_KBPS = 450                  # bitrate at 480p 15 fps; the rest scales from this
IDLE_STOP = 5                    # s without viewers before ffmpeg stops
WINDOW = 10                      # s between fps measurements
SLOW = 0.8                       # less than this share of the frames = too slow...
BUSY = 0.15                      # ...and the CPU is to blame if less than this share was idle
HOT, COOL = 80, 70               # °C: 480p from HOT until below COOL (the Pi 5 throttles at 85)
FFMPEG = shutil.which("ffmpeg")
NICE = shutil.which("nice") if os.name == "posix" else None
log = logging.getLogger("camera")


def max_fps(height):
    """Highest fps within PIXEL_BUDGET. Over 15 means decoding all 30 frames the camera sends,
    so anything between 15 and 30 rounds down to 15."""
    w, h = RESOLUTIONS[height]
    fps = PIXEL_BUDGET // (w * h)
    return 30 if fps >= 30 else min(fps, 15)


def kbps(height, fps):
    """Bitrate for a sharp picture. Grows with pixels/s to the power 0.75, since bigger pictures
    compress better per pixel: 480p 15 fps 450 kbit/s, 1080p 30 fps 3170, 1440p 15 fps 2900."""
    w, h = RESOLUTIONS[height]
    return round(BASE_KBPS * (w * h * fps / (640 * 480 * 15)) ** 0.75)


def find_device(device=""):
    """The configured device, else the first USB camera, else None (test pattern)."""
    if device:
        return device
    found = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    return found[0] if found else None


def codec_of(init):
    """'avc1.PPCCLL' (profile, constraints, level) for MediaSource, from the avcC box."""
    i = init.find(b"avcC") + 5  # skip the box type and the version byte
    return "avc1." + init[i:i + 3].hex()


def is_keyframe(mdat):
    """True if the frame in this mdat box has an IDR slice (H.264 NAL unit type 5)."""
    pos = 8
    while pos + 5 <= len(mdat):
        if mdat[pos + 4] & 0x1F == 5:
            return True
        pos += 4 + int.from_bytes(mdat[pos:pos + 4], "big")
    return False


def read_temp():
    """CPU temperature in °C, or None (not a Pi)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def cpu_times():
    """(idle, total) CPU time since boot, or None (not Linux)."""
    try:
        with open("/proc/stat") as f:
            t = [int(x) for x in f.readline().split()[1:]]
        return t[3] + t[4], sum(t)  # idle + iowait
    except (OSError, ValueError, IndexError):
        return None


def halt(proc):
    proc.terminate()
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Viewer:
    """One /video connection. Holds about 1 s of frames; a slower connection skips ahead."""

    def __init__(self):
        self.queue = queue.Queue(maxsize=30)
        self.need_key = True  # nothing is sent until a keyframe, which goes with the init segment

    def send(self, init, fragment, key):
        if self.need_key:
            if not key:
                return
            fragment = init + fragment
        try:
            self.queue.put_nowait(fragment)
            self.need_key = False
        except queue.Full:  # 4G too slow: drop the backlog and go on from the next keyframe
            self.clear()
            self.need_key = True

    def end(self):
        self.clear()
        self.queue.put_nowait(None)

    def clear(self):
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass


class Camera:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.viewers = set()
        self.proc = None         # the running ffmpeg
        self.args = None         # its command line
        self.init = None         # its init segment (ftyp + moov)
        self.started = 0.0
        self.next_start = 0.0    # no start before this, if ffmpeg keeps failing
        self.last_viewer = 0.0
        self.step_down = 0       # resolution notches down because the Pi was too slow
        self.slow = None         # why, for the GUI
        self.hot = False
        self.temp = None
        self.frames = 0          # frames since the measuring window started
        self.window = 0.0        # when it started, 0 = no video yet
        self.cpu = None          # cpu_times() then
        self.actual_fps = None
        self.closed = False
        threading.Thread(target=self._supervise, daemon=True).start()

    def configure(self, config):
        """New settings (dict from clean_config). The supervisor restarts ffmpeg if needed."""
        with self.lock:
            if config != self.config:
                self.step_down, self.slow = 0, None
            self.config = config

    def subscribe(self):
        """Yields the video for one viewer: init segment + keyframe, then one fragment per frame.
        Ends when the camera is turned off or restarts with new settings. Yields nothing if the
        camera is off or there's no ffmpeg."""
        if not (self.config["on"] and FFMPEG):
            return
        viewer = Viewer()
        with self.lock:
            self.viewers.add(viewer)
            self.last_viewer = time.monotonic()
        try:
            while True:
                chunk = viewer.queue.get(timeout=15)
                if chunk is None:
                    return
                yield chunk
        except queue.Empty:
            log.warning("No video from ffmpeg for 15 s")
        finally:
            with self.lock:
                self.viewers.discard(viewer)

    def mode(self):
        """(height, fps) to send now: the config, stepped down if the Pi is too slow or too hot."""
        fps = self.config["fps"]
        if self.hot:
            return 480, min(15, fps)
        heights = sorted(RESOLUTIONS)
        height = heights[max(0, heights.index(self.config["height"]) - self.step_down)]
        return height, min(fps, max_fps(height))

    def status(self):
        with self.lock:
            height, fps = self.mode()
            w, h = RESOLUTIONS[height]
            if self.hot:
                degraded = f"Pi-en er {self.temp:.0f} °C. Videoen er satt ned til 480p til den er under {COOL} °C."
            else:
                degraded = self.slow
            return {
                "on": self.config["on"],
                "available": FFMPEG is not None,
                "simulated": find_device(self.config["device"]) is None,
                "running": self.proc is not None,
                "viewers": len(self.viewers),
                "width": w, "height": h, "fps": fps, "kbps": kbps(height, fps),
                "actual_fps": None if self.actual_fps is None else round(self.actual_fps, 1),
                "temp": self.temp,
                "degraded": degraded,
                "max_fps": {height: max_fps(height) for height in RESOLUTIONS},
            }

    def close(self):
        self.closed = True
        with self.lock:
            proc = self._stop() if self.proc else None
        if proc:
            halt(proc)

    def command(self):
        height, fps = self.mode()
        w, h = RESOLUTIONS[height]
        rate = 15 if fps <= 15 else 30  # what the camera sends
        device = find_device(self.config["device"])
        if device:
            src = ["-f", "v4l2", "-input_format", "mjpeg", "-video_size", f"{w}x{h}",
                   "-framerate", str(rate), "-i", device]
        else:
            src = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={rate}"]
        rate_kbps = f"{kbps(height, fps)}k"
        return ([NICE, "-n", "19"] if NICE else []) + [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-threads", "2", *src,
            *(["-vf", f"fps={fps}"] if fps != rate else []),
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-threads", "2",
            "-pix_fmt", "yuv420p", "-g", str(2 * fps), "-b:v", rate_kbps, "-maxrate", rate_kbps, "-bufsize", rate_kbps,
            "-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_every_frame", "-"]

    def _supervise(self):
        """Every 0.5 s: start ffmpeg when wanted, stop it when not, restart it on new settings."""
        while not self.closed:
            time.sleep(0.5)
            stopped = None
            try:
                temp = read_temp()
                with self.lock:
                    self._check_temp(temp)
                    now = time.monotonic()
                    if self.viewers:
                        self.last_viewer = now
                    want = bool(FFMPEG) and self.config["on"] and now - self.last_viewer < IDLE_STOP
                    if self.proc and (not want or self.args != self.command()):
                        stopped = self._stop()
                    elif want and not self.proc and now >= self.next_start:
                        self._start()
                    elif self.proc:
                        self._measure(now)
                if stopped:
                    halt(stopped)
            except Exception:
                log.exception("Camera supervisor")

    def _start(self):
        self.args = self.command()
        height, fps = self.mode()
        log.info("Camera on: %dp %d fps %d kbit/s from %s", height, fps, kbps(height, fps),
                 find_device(self.config["device"]) or "test pattern")
        self.started = time.monotonic()
        self.init, self.window, self.frames, self.actual_fps = None, 0.0, 0, None
        try:
            self.proc = subprocess.Popen(self.args, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            log.error("Can't start ffmpeg: %s", e)
            self.next_start = self.started + 10
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._log, args=(self.proc,), daemon=True).start()

    def _stop(self):
        """Ends every viewer and returns the ffmpeg to halt() outside the lock."""
        log.info("Camera off")
        proc, self.proc, self.init, self.actual_fps = self.proc, None, None, None
        for viewer in self.viewers:
            viewer.end()
        return proc

    def _read(self, proc):
        """Splits ffmpeg's MP4 into boxes: ftyp + moov is the init segment, and each moof + mdat
        is one frame, which goes to every viewer."""
        head = moof = b""
        out = proc.stdout
        while True:
            box = out.read(8)
            if len(box) < 8:
                break
            size, kind = struct.unpack(">I4s", box)
            if size < 8:
                break
            box += out.read(size - 8)
            if kind in (b"ftyp", b"moov"):
                head += box
                if kind == b"moov":
                    with self.lock:
                        if self.proc is proc:
                            self.init, self.window, self.cpu = head, time.monotonic(), cpu_times()
            elif kind == b"moof":
                moof = box
            elif kind == b"mdat":
                key = is_keyframe(box)
                with self.lock:
                    if self.proc is proc and self.init:
                        self.frames += 1
                        for viewer in self.viewers:
                            viewer.send(self.init, moof + box, key)
        code = proc.wait()
        with self.lock:
            if self.proc is not proc:
                return  # stopped by _supervise
            ran = time.monotonic() - self.started
            delay = 2 if ran > 10 else 10
            log.warning("ffmpeg stopped (exit code %s) after %.0f s, restarting in %d s", code, ran, delay)
            self._stop()
            self.next_start = time.monotonic() + delay

    def _log(self, proc):
        for line in proc.stderr:
            log.warning("ffmpeg: %s", line.decode(errors="replace").rstrip())

    def _measure(self, now):
        """Every WINDOW s: fps actually sent. Too few with a full CPU = step the resolution down."""
        if not self.window or now - self.window < WINDOW:
            return
        self.actual_fps = self.frames / (now - self.window)
        cpu = cpu_times()
        busy = cpu and self.cpu and cpu[1] > self.cpu[1] and \
            (cpu[0] - self.cpu[0]) / (cpu[1] - self.cpu[1]) < BUSY
        self.frames, self.window, self.cpu = 0, now, cpu
        height, fps = self.mode()
        if self.actual_fps >= SLOW * fps:
            if not self.step_down:
                self.slow = None
        elif not busy:
            self.slow = f"Kameraet sender bare {self.actual_fps:.0f} av {fps} fps (lite lys?)."
        elif height == min(RESOLUTIONS):
            self.slow = f"Pi-en klarer bare {self.actual_fps:.0f} av {fps} fps, selv i {height}p."
        else:
            self.step_down += 1
            self.slow = (f"Pi-en klarte bare {self.actual_fps:.0f} av {fps} fps i {height}p. "
                         f"Videoen er satt ned til {self.mode()[0]}p.")
            log.warning("Only %.1f of %d fps at %dp with the CPU full: down to %dp",
                        self.actual_fps, fps, height, self.mode()[0])

    def _check_temp(self, temp):
        self.temp = temp
        if temp is None:
            return
        if not self.hot and temp >= HOT:
            self.hot = True
            log.warning("CPU at %.0f °C: video down to 480p", temp)
        elif self.hot and temp < COOL:
            self.hot = False
            log.info("CPU at %.0f °C: video back to normal", temp)
