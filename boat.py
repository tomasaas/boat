"""The boat: two fixed thrusters (left/right) with differential steering.

    boat = Boat(load_config())
    boat.drive(surge=1, yaw=0)   # surge: +1 forward / -1 back, yaw: +1 starboard / -1 port
    boat.status()
    boat.close()
"""
import json
import os

from camera import RESOLUTIONS, max_fps
from dshot import SIMULATED, Esc

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {
    "speed": 0.3,  # throttle while a key is held (0..1)
    "turn": 0.5,   # how much yaw adds/subtracts per side (0..1)
    "poles": 14,   # motor magnets, used to turn eRPM into RPM
    "left": {"gpio": 18, "reverse": False},
    "right": {"gpio": 19, "reverse": False},
    # on: off = no video over 4G. height: a key of RESOLUTIONS. kbps caps the data use.
    # device: "" = the first USB camera.
    "camera": {"on": True, "height": 480, "fps": 15, "kbps": 400, "device": ""},
}


def mix(surge, yaw, speed, turn):
    """Differential thrust -> (left, right) throttle in -speed..speed.

    Left ahead of right turns the bow to starboard, whether moving forward or back.
    If a side would exceed 1, both are scaled down so the turn keeps its shape.
    """
    left = surge + turn * yaw
    right = surge - turn * yaw
    biggest = max(1.0, abs(left), abs(right))
    return speed * left / biggest, speed * right / biggest


def clean_config(cfg):
    """Fill in defaults and coerce types, so a bad form value can't crash the motors."""
    def side(name):
        s = cfg.get(name, {})
        return {"gpio": int(s.get("gpio", DEFAULT_CONFIG[name]["gpio"])),
                "reverse": bool(s.get("reverse", False))}

    def camera():
        c, d = cfg.get("camera", {}), DEFAULT_CONFIG["camera"]
        height = int(c.get("height", d["height"]))
        if height not in RESOLUTIONS:
            height = d["height"]
        return {"on": bool(c.get("on", d["on"])),
                "height": height,
                "fps": max(1, min(max_fps(height), int(c.get("fps", d["fps"])))),
                "kbps": max(100, min(8000, int(c.get("kbps", d["kbps"])))),
                "device": str(c.get("device", d["device"])).strip()}
    return {
        "speed": max(0.0, min(1.0, float(cfg.get("speed", DEFAULT_CONFIG["speed"])))),
        "turn": max(0.0, min(1.0, float(cfg.get("turn", DEFAULT_CONFIG["turn"])))),
        "poles": max(2, int(cfg.get("poles", DEFAULT_CONFIG["poles"]))),
        "left": side("left"),
        "right": side("right"),
        "camera": camera(),
    }


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return clean_config({})
    with open(CONFIG_FILE) as f:
        return clean_config(json.load(f))


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


class Boat:
    def __init__(self, config):
        self.config = config
        self.left = Esc(config["left"]["gpio"], config["poles"], config["left"]["reverse"])
        self.right = Esc(config["right"]["gpio"], config["poles"], config["right"]["reverse"])

    def drive(self, surge, yaw):
        self.left.throttle, self.right.throttle = mix(surge, yaw, self.config["speed"], self.config["turn"])

    def stop(self):
        self.drive(0, 0)

    def status(self):
        return {
            "simulated": SIMULATED,
            "left": {"throttle": self.left.throttle, "rpm": self.left.rpm},
            "right": {"throttle": self.right.throttle, "rpm": self.right.rpm},
        }

    def close(self):
        self.left.close()
        self.right.close()
