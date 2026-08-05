import json
import os
import platform
import select
import socket
import sys
import time
from enum import Enum
from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import serial
except ImportError:
    serial = None
 
try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    InputDevice = None

THRESHOLDS = {
    'cpu_temp':     {'warn': 70, 'crit': 85},
    'gpu_temp':     {'warn': 75, 'crit': 90},
    'cpu_usage':    {'warn': 70, 'crit': 90},
    'gpu_usage':    {'warn': 70, 'crit': 90},
    'ram_percent':  {'warn': 75, 'crit': 90},
    'disk_percent': {'warn': 80, 'crit': 95},
}
 
 
class DisplayState(Enum):
    CONNECTED    = 'connected'
    DISCONNECTED = 'disconnected'
    STARTING     = 'starting'

BG_DARK    = (13,  17,  23)
BG_ALT     = (22,  27,  34)
BG_HEADER  = (16,  20,  28)
GREEN      = (63,  185, 80)
YELLOW     = (210, 153, 34)
RED        = (248, 81,  73)
BLUE       = (88,  166, 255)
WHITE      = (240, 246, 252)
GREY       = (139, 148, 158)
DIM        = (48,  54,  61)
LIVE_GREEN = (63,  185, 80)

WIDTH, HEIGHT   = 480, 320
APP_DIR         = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE     = os.path.join(APP_DIR, "config.json")
DEFAULT_WIFI_PORT = 65432
DEFAULT_USB_PORT  = "/dev/ttyGS0"
DEFAULT_BAUD      = 115200
TIMEOUT           = 3.0

EXPECTED_KEYS = {"timestamp", "cpu", "gpu", "ram", "disk", "network", "uptime", "os"}

FONT_DIR = "/usr/share/fonts/truetype/dejavu"

MODE_BTN = (WIDTH - 78, 4, WIDTH - 8, 34)
MODE_HIT = (WIDTH - 120, 0, WIDTH - 1, 62)

DEFAULT_CONFIG = {
    "mode": "wifi",
    "wifi": {"device_ip": "", "port": DEFAULT_WIFI_PORT},
    "usb":  {"host_port": "COM3", "device_port": DEFAULT_USB_PORT, "baud": DEFAULT_BAUD},
    "touch": {"swap_xy": False, "invert_x": False, "invert_y": False},
}

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    except (OSError, json.JSONDecodeError):
        print(f"No valid {CONFIG_FILE}; using defaults.")
    return cfg
 
 
def save_mode(cfg, mode):
    cfg["mode"] = mode
    out = {k: v for k, v in cfg.items()
           if k != "touch" or v != DEFAULT_CONFIG["touch"]}
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(out, f, indent=2)
    except OSError as e:
        print(f"Warning: could not save mode ({e}).")
 
 
def get_own_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None

class Touch:
    def __init__(self, touch_cfg):
        self.dev = None
        self.cfg = touch_cfg
        if InputDevice is None:
            print("Touch unavailable: python3-evdev is not installed.")
            return
        try:
            for path in list_devices():
                dev = InputDevice(path)
                caps = dev.capabilities()
                abs_axes = dict(caps.get(ecodes.EV_ABS, []))
                if ecodes.ABS_X in abs_axes and ecodes.ABS_Y in abs_axes:
                    self.dev = dev
                    self.x_info = abs_axes[ecodes.ABS_X]
                    self.y_info = abs_axes[ecodes.ABS_Y]
                    print(f"Touch device: {dev.name} ({path})")
                    break
            if self.dev is None:
                print("Touch unavailable: no touchscreen found.")
        except Exception as e:
            print(f"Touch unavailable ({e}).")
 
    @property
    def available(self):
        return self.dev is not None
 
    def fileno(self):
        return self.dev.fd if self.dev else None
 
    def _scale(self, raw_x, raw_y):
        fx = (raw_x - self.x_info.min) / max(self.x_info.max - self.x_info.min, 1)
        fy = (raw_y - self.y_info.min) / max(self.y_info.max - self.y_info.min, 1)
        if self.cfg.get("swap_xy"):   fx, fy = fy, fx
        if self.cfg.get("invert_x"):  fx = 1.0 - fx
        if self.cfg.get("invert_y"):  fy = 1.0 - fy
        x = min(max(int(fx * WIDTH),  0), WIDTH  - 1)
        y = min(max(int(fy * HEIGHT), 0), HEIGHT - 1)
        return x, y
 
    def poll(self):
        if self.dev is None:
            return None
        raw_x = raw_y = None
        released = False
        try:
            for event in self.dev.read():
                if event.type == ecodes.EV_ABS:
                    if event.code == ecodes.ABS_X: raw_x = event.value
                    elif event.code == ecodes.ABS_Y: raw_y = event.value
                elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                    if event.value == 1:   self._down = True
                    elif event.value == 0 and getattr(self, "_down", False):
                        self._down = False
                        released = True
        except BlockingIOError:
            pass
        except OSError:
            return None
        if raw_x is not None and raw_y is not None:
            self._last = self._scale(raw_x, raw_y)
        if released:
            return getattr(self, "_last", None)
        return None
 
    def wait_for_tap(self, timeout=None):
        if self.dev is None:
            return None
        deadline = None if timeout is None else time.time() + timeout
        while True:
            remaining = None if deadline is None else max(deadline - time.time(), 0)
            r, _, _ = select.select([self.dev.fd], [], [], remaining)
            if not r:
                return None
            tap = self.poll()
            if tap:
                return tap
 
    def wait_for_raw_tap(self):
        if self.dev is None:
            return None
        raw_x = raw_y = None
        while True:
            select.select([self.dev.fd], [], [], None)
            try:
                for event in self.dev.read():
                    if event.type == ecodes.EV_ABS:
                        if event.code == ecodes.ABS_X: raw_x = event.value
                        elif event.code == ecodes.ABS_Y: raw_y = event.value
                    elif (event.type == ecodes.EV_KEY
                          and event.code == ecodes.BTN_TOUCH
                          and event.value == 0):
                        if raw_x is not None and raw_y is not None:
                            return raw_x, raw_y
            except (BlockingIOError, OSError):
                continue
 
 
def in_box(point, box):
    x, y = point
    x1, y1, x2, y2 = box
    return x1 <= x <= x2 and y1 <= y <= y2

def open_usb_reader(cfg):
    if serial is None:
        raise RuntimeError("pyserial is not installed")
    usb = cfg["usb"]
    return serial.Serial(usb.get("device_port", DEFAULT_USB_PORT),
                         usb.get("baud", DEFAULT_BAUD), timeout=TIMEOUT)
 
 
def open_wifi_server(cfg):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", cfg["wifi"].get("port", DEFAULT_WIFI_PORT)))
    server.listen(1)
    return server
 
 
def read_line(buffer, chunk):
    buffer += chunk
    if b"\n" not in buffer:
        return None, buffer
    line, _, buffer = buffer.partition(b"\n")
    return line.decode("utf-8", errors="replace").strip(), buffer

def parse_message(line):
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        print("Warning: discarded malformed message.")
        return None
    if not EXPECTED_KEYS.issubset(data.keys()):
        print("Warning: discarded message with missing keys.")
        return None
    return data
 
 
def get_color(metric, value):
    if value is None:
        return GREY
    limits = THRESHOLDS[metric]
    if value >= limits['crit']:  return RED
    if value >= limits['warn']:  return YELLOW
    return GREEN
 
 
def worst_color(colors):
    severity = {GREEN: 0, GREY: 0, YELLOW: 1, RED: 2}
    return max(colors, key=lambda c: severity.get(c, 0))

def find_framebuffer():
    for node in ("fb1", "fb0"):
        path = f"/dev/{node}"
        if os.path.exists(path):
            bpp = 16
            try:
                with open(f"/sys/class/graphics/{node}/bits_per_pixel") as f:
                    bpp = int(f.read().strip())
            except OSError:
                pass
            return path, bpp
    raise OSError("no framebuffer device found")
 
 
def image_to_fb_bytes(img, bpp):
    if bpp == 16:  return _rgb_to_rgb565(img.convert("RGB"))
    if bpp == 32:  return img.convert("RGB").tobytes("raw", "BGRX")
    return img.convert("RGB").tobytes("raw", "RGB")
 
 
def _rgb_to_rgb565(img):
    r, g, b = img.split()
    low  = ImageChops.add(g.point(lambda v: (v << 3) & 0xE0),
                          b.point(lambda v: v >> 3))
    high = ImageChops.add(r.point(lambda v: v & 0xF8),
                          g.point(lambda v: v >> 5))
    return Image.merge("LA", (low, high)).tobytes()
 
 
def write_frame(fb, img, bpp):
    fb.seek(0)
    fb.write(image_to_fb_bytes(img, bpp))
    fb.flush()
 
 
def load_fonts():
    """Loads DejaVu fonts on the Pi; falls back gracefully on Windows/Linux dev."""
    system = platform.system()
    if system == "Windows":
        bold_path   = "C:/Windows/Fonts/consolab.ttf"
        normal_path = "C:/Windows/Fonts/consola.ttf"
    else:
        bold_path   = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
        normal_path = os.path.join(FONT_DIR, "DejaVuSans.ttf")
    try:
        title  = ImageFont.truetype(bold_path,   20)
        label  = ImageFont.truetype(bold_path,   15)
        value  = ImageFont.truetype(normal_path, 14)
        small  = ImageFont.truetype(normal_path, 12)
        big    = ImageFont.truetype(bold_path,   28)
    except OSError:
        title = label = value = small = big = ImageFont.load_default()
    return title, label, value, small, big

def draw_progress_bar(draw, x, y, w, h, percent, color):
    """Rounded progress bar — track then filled portion."""
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2,
                            fill=(33, 38, 45))
    fill_w = int(max(0, min(percent, 100)) / 100 * w)
    if fill_w > 0:
        draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=h // 2,
                                fill=color)
 
 
def draw_mode_button(draw, small_font):
    x1, y1, x2, y2 = MODE_BTN
    draw.rounded_rectangle(MODE_BTN, radius=6, outline=DIM, width=1)
    draw.text(((x1 + x2) // 2, (y1 + y2) // 2), "MODE",
              font=small_font, fill=GREY, anchor="mm")
 
 
def draw_live_badge(draw, small_font):
    """LIVE indicator badge, bottom-right of the data screen."""
    draw.rounded_rectangle([372, 294, 472, 316], radius=4,
                            fill=BG_ALT, outline=DIM)
    draw.ellipse([380, 301, 390, 311], fill=LIVE_GREEN)
    draw.text((396, 305), "LIVE", font=small_font, fill=LIVE_GREEN, anchor="lm")

def render_mode_select(fonts, touch_ok):
    title_font, label_font, value_font, small_font, big_font = fonts
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
    draw = ImageDraw.Draw(img)
 
    # Header
    draw.rectangle([0, 0, WIDTH, 42], fill=BG_HEADER)
    draw.text((WIDTH // 2, 21), "CircuitSense",
              font=title_font, fill=WHITE, anchor="mm")
    draw.line([0, 42, WIDTH, 42], fill=DIM, width=1)
 
    draw.text((WIDTH // 2, 72), "Select connection mode",
              font=value_font, fill=GREY, anchor="mm")
 
    # USB button
    draw.rounded_rectangle((24, 96, 228, 240), radius=10, outline=BLUE, width=2,
                            fill=BG_ALT)
    draw.text((126, 158), "USB", font=big_font, fill=WHITE, anchor="mm")
    draw.text((126, 196), "serial cable", font=small_font, fill=GREY, anchor="mm")
 
    # Wi-Fi button
    draw.rounded_rectangle((252, 96, 456, 240), radius=10, outline=BLUE, width=2,
                            fill=BG_ALT)
    draw.text((354, 158), "Wi-Fi", font=big_font, fill=WHITE, anchor="mm")
    draw.text((354, 196), "local network", font=small_font, fill=GREY, anchor="mm")
 
    hint = ("No touchscreen detected — using config.json"
            if not touch_ok else "Tap a mode to begin")
    hint_color = YELLOW if not touch_ok else GREY
    draw.text((WIDTH // 2, 270), hint, font=small_font,
              fill=hint_color, anchor="mm")
    return img
 
 
def render_waiting(mode, cfg, fonts):
    title_font, label_font, value_font, small_font, big_font = fonts
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
    draw = ImageDraw.Draw(img)
 
    # Header
    draw.rectangle([0, 0, WIDTH, 42], fill=BG_HEADER)
    draw.text((14, 21), "CircuitSense", font=title_font, fill=WHITE, anchor="lm")
    draw_mode_button(draw, small_font)
    draw.line([0, 42, WIDTH, 42], fill=DIM, width=1)
 
    draw.text((WIDTH // 2, 110), "Waiting for host\u2026",
              font=label_font, fill=GREY, anchor="mm")
 
    if mode == "wifi":
        ip   = get_own_ip()
        port = cfg["wifi"].get("port", DEFAULT_WIFI_PORT)
        if ip:
            draw.text((WIDTH // 2, 170), f"{ip}:{port}",
                      font=label_font, fill=BLUE, anchor="mm")
            draw.text((WIDTH // 2, 206), "enter this address on the PC",
                      font=small_font, fill=GREY, anchor="mm")
        else:
            draw.text((WIDTH // 2, 170), "No network connection",
                      font=label_font, fill=YELLOW, anchor="mm")
    else:
        port = cfg["usb"].get("device_port", DEFAULT_USB_PORT)
        draw.text((WIDTH // 2, 170), f"USB  {port}",
                  font=label_font, fill=BLUE, anchor="mm")
        draw.text((WIDTH // 2, 206), "connect the USB cable to the PC",
                  font=small_font, fill=GREY, anchor="mm")
    return img
 
 
def render_data_screen(data, fonts):
    """Improved data layout — header bar, per-row backgrounds, progress bars,
    color-coded values, and a LIVE badge. All six metrics on one screen."""
    title_font, label_font, value_font, small_font, big_font = fonts
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
    draw = ImageDraw.Draw(img)

    #Header
    draw.rectangle([0, 0, WIDTH, 38], fill=BG_HEADER)
    draw.text((14, 19), "CircuitSense", font=title_font, fill=BLUE, anchor="lm")
    draw.text((WIDTH - 90, 19), str(data.get("os", "")),
              font=small_font, fill=GREY, anchor="lm")
    draw_mode_button(draw, small_font)
    draw.line([0, 38, WIDTH, 38], fill=DIM, width=1)

    #Row layout
    ROW_H = 44
    row_tops = [39 + i * ROW_H for i in range(6)]
    row_bgs  = [BG_ALT, BG_DARK, BG_ALT, BG_DARK, BG_ALT, BG_DARK]
    for y, bg in zip(row_tops, row_bgs):
        draw.rectangle([0, y, WIDTH, y + ROW_H - 1], fill=bg)
 
    #Dividers between rows
    for y in row_tops[1:]:
        draw.line([14, y, WIDTH - 14, y], fill=DIM, width=1)
 
    cpu  = data["cpu"]
    gpu  = data["gpu"]
    ram  = data["ram"]
    disk = data["disk"]
    net  = data["network"]
    up   = data["uptime"]

    #CPU
    y = row_tops[0]
    c_use = get_color('cpu_usage', cpu["usage_percent"])
    c_tmp = get_color('cpu_temp',  cpu["temperature_c"])
    draw.text((14, y + 4),  "CPU",  font=small_font, fill=GREY)
    draw.text((14, y + 20), f"{cpu['usage_percent']:.1f}%",
              font=value_font, fill=c_use)
    draw_progress_bar(draw, 90, y + 24, 190, 6, cpu["usage_percent"], c_use)
    draw.text((295, y + 4),  "TEMP", font=small_font, fill=GREY)
    draw.text((295, y + 20), f"{cpu['temperature_c']:.1f}\u00b0C",
              font=value_font, fill=c_tmp)
    dot = worst_color([c_use, c_tmp])
    draw.ellipse([449, y + 13, 465, y + 29], fill=dot)

    #GPU
    y = row_tops[1]
    if gpu.get("available") and gpu["usage_percent"] is not None:
        c_use = get_color('gpu_usage', gpu["usage_percent"])
        c_tmp = get_color('gpu_temp',  gpu["temperature_c"])
        draw.text((14, y + 4),  "GPU",  font=small_font, fill=GREY)
        draw.text((14, y + 20), f"{gpu['usage_percent']:.1f}%",
                  font=value_font, fill=c_use)
        draw_progress_bar(draw, 90, y + 24, 190, 6, gpu["usage_percent"], c_use)
        draw.text((295, y + 4),  "TEMP", font=small_font, fill=GREY)
        draw.text((295, y + 20), f"{gpu['temperature_c']:.1f}\u00b0C",
                  font=value_font, fill=c_tmp)
        dot = worst_color([c_use, c_tmp])
        draw.ellipse([449, y + 13, 465, y + 29], fill=dot)
    else:
        draw.text((14, y + 4),  "GPU", font=small_font, fill=GREY)
        draw.text((14, y + 20), "N/A", font=value_font, fill=GREY)
        draw.ellipse([449, y + 13, 465, y + 29], fill=GREY)

    #RAM
    y   = row_tops[2]
    col = get_color('ram_percent', ram["percent"])
    draw.text((14, y + 4),  "RAM",  font=small_font, fill=GREY)
    draw.text((14, y + 20), f"{ram['used_gb']:.1f} / {ram['total_gb']:.1f} GB",
              font=value_font, fill=col)
    draw_progress_bar(draw, 230, y + 24, 130, 6, ram["percent"], col)
    draw.ellipse([449, y + 13, 465, y + 29], fill=col)
 
    #DISK
    y   = row_tops[3]
    col = get_color('disk_percent', disk["usage_percent"])
    draw.text((14, y + 4),  "DISK", font=small_font, fill=GREY)
    draw.text((14, y + 20), f"{disk['usage_percent']:.1f}%",
              font=value_font, fill=col)
    draw_progress_bar(draw, 90, y + 24, 190, 6, disk["usage_percent"], col)
    draw.ellipse([449, y + 13, 465, y + 29], fill=col)
 
    #NETWOR
    y = row_tops[4]
    draw.text((14, y + 4),  "NET", font=small_font, fill=GREY)
    draw.text((14, y + 20), f"\u25b2 {net['upload_mbps']:.1f} Mbps",
              font=value_font, fill=BLUE)
    draw.text((240, y + 20), f"\u25bc {net['download_mbps']:.1f} Mbps",
              font=value_font, fill=GREEN)
 
    #UPTIME
    y = row_tops[5]
    draw.text((14, y + 4),  "UPTIME", font=small_font, fill=GREY)
    draw.text((14, y + 18), up["formatted"], font=label_font, fill=WHITE)
    draw_live_badge(draw, small_font)

    return img

def render_calibration(prompt, target, fonts):
    title_font, label_font, value_font, small_font, big_font = fonts
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
    draw = ImageDraw.Draw(img)
    tx   = min(max(target[0], 18), WIDTH  - 19)
    ty   = min(max(target[1], 18), HEIGHT - 19)
    draw.line((tx - 16, ty, tx + 16, ty), fill=RED, width=3)
    draw.line((tx, ty - 16, tx, ty + 16), fill=RED, width=3)
    draw.ellipse((tx - 9, ty - 9, tx + 9, ty + 9), outline=RED, width=2)
    draw.text((WIDTH // 2, HEIGHT // 2 - 16), "Touch calibration",
              font=label_font, fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, HEIGHT // 2 + 16), prompt,
              font=small_font, fill=GREY, anchor="mm")
    return img

#Calibration
def run_calibration(fb, bpp, fonts, touch, cfg):
    if not touch.available:
        print("Calibration needs a touchscreen; none was detected.")
        return
    targets = [
        ("Tap the crosshair (top-left)",    (0,     0)),
        ("Tap the crosshair (top-right)",   (WIDTH, 0)),
        ("Tap the crosshair (bottom-left)", (0,     HEIGHT)),
    ]
    raws = []
    for prompt, point in targets:
        write_frame(fb, render_calibration(prompt, point, fonts), bpp)
        raw = touch.wait_for_raw_tap()
        if raw is None:
            print("Calibration cancelled.")
            return
        print(f"  {prompt}: raw {raw}")
        raws.append(raw)
        time.sleep(0.4)
 
    tl, tr, bl = raws
    across  = (tr[0] - tl[0], tr[1] - tl[1])
    down    = (bl[0] - tl[0], bl[1] - tl[1])
    swap_xy = abs(across[1]) > abs(across[0])
    invert_x = (across[1] < 0) if swap_xy else (across[0] < 0)
    invert_y = (down[0]   < 0) if swap_xy else (down[1]   < 0)
 
    cfg["touch"] = {"swap_xy": bool(swap_xy),
                    "invert_x": bool(invert_x),
                    "invert_y": bool(invert_y)}
    touch.cfg = cfg["touch"]
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"Calibration saved: {cfg['touch']}")
    except OSError as e:
        print(f"Warning: could not save calibration ({e}).")
 
    _, label_font, _, small_font, _ = fonts
    for _ in range(3):
        img  = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
        draw = ImageDraw.Draw(img)
        draw.text((WIDTH // 2, 120), "Calibration saved",
                  font=label_font, fill=WHITE, anchor="mm")
        draw.text((WIDTH // 2, 160), "Tap anywhere to check the marker follows",
                  font=small_font, fill=GREY, anchor="mm")
        write_frame(fb, img, bpp)
        tap = touch.wait_for_tap(timeout=6)
        if tap is None:
            break
        img  = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
        draw = ImageDraw.Draw(img)
        draw.ellipse((tap[0]-12, tap[1]-12, tap[0]+12, tap[1]+12),
                     outline=GREEN, width=3)
        draw.text((WIDTH // 2, HEIGHT - 30), f"{tap[0]}, {tap[1]}",
                  font=small_font, fill=GREEN, anchor="mm")
        write_frame(fb, img, bpp)
        time.sleep(1.2)

#Mode Selection
def select_mode(fb, bpp, fonts, touch, cfg):
    write_frame(fb, render_mode_select(fonts, touch.available), bpp)
    if not touch.available:
        mode = cfg.get("mode", "wifi")
        print(f"No touchscreen; using mode '{mode}' from config.json.")
        time.sleep(2)
        return mode
    while True:
        tap = touch.wait_for_tap()
        if tap is None:
            continue
        mode = "usb" if tap[0] < WIDTH // 2 else "wifi"
        print(f"Mode selected: {mode}")
        save_mode(cfg, mode)
        return mode

#Receive loops
def handle_tap(touch, state):
    tap = touch.poll()
    if tap is None:
        return False
    hit = in_box(tap, MODE_HIT)
    print(f"Tap at {tap[0]},{tap[1]} -> {'MODE' if hit else 'ignored'}")
    return hit
 
 
def run_usb_loop(cfg, fb, bpp, fonts, touch):
    ser = open_usb_reader(cfg)
    print(f"USB mode: listening on {cfg['usb'].get('device_port', DEFAULT_USB_PORT)}")
    write_frame(fb, render_waiting("usb", cfg, fonts), bpp)
    state  = DisplayState.STARTING
    buffer = b""
    fds    = [ser.fileno()] + ([touch.fileno()] if touch.available else [])
    try:
        while True:
            ready, _, _ = select.select(fds, [], [], TIMEOUT)
            if touch.available and touch.fileno() in ready:
                if handle_tap(touch, state):
                    return 'back'
            if ser.fileno() in ready:
                chunk = ser.read(ser.in_waiting or 1)
                if chunk:
                    while True:
                        line, buffer = read_line(buffer, chunk)
                        chunk = b""
                        if line is None:
                            break
                        data = parse_message(line)
                        if data:
                            write_frame(fb, render_data_screen(data, fonts), bpp)
                            state = DisplayState.CONNECTED
                    continue
            if not ready and state == DisplayState.CONNECTED:
                write_frame(fb, render_waiting("usb", cfg, fonts), bpp)
                state = DisplayState.DISCONNECTED
                print("Connection lost.")
    finally:
        ser.close()
 
 
def run_wifi_loop(cfg, fb, bpp, fonts, touch):
    server   = open_wifi_server(cfg)
    port     = cfg["wifi"].get("port", DEFAULT_WIFI_PORT)
    touch_fd = [touch.fileno()] if touch.available else []
    print(f"Wi-Fi mode: listening on TCP port {port}")
    write_frame(fb, render_waiting("wifi", cfg, fonts), bpp)
    server.setblocking(False)
    try:
        while True:
            conn = None
            while conn is None:
                ready, _, _ = select.select([server.fileno()] + touch_fd, [], [], 1.0)
                if touch.available and touch.fileno() in ready:
                    if handle_tap(touch, None):
                        return 'back'
                if server.fileno() in ready:
                    conn, addr = server.accept()
                    conn.setblocking(False)
                    print(f"Host connected from {addr[0]}.")
 
            buffer = b""
            state  = DisplayState.CONNECTED
            while True:
                ready, _, _ = select.select([conn.fileno()] + touch_fd, [], [], TIMEOUT)
                if touch.available and touch.fileno() in ready:
                    if handle_tap(touch, state):
                        conn.close()
                        return 'back'
                if conn.fileno() in ready:
                    try:
                        chunk = conn.recv(4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        break
                    while True:
                        line, buffer = read_line(buffer, chunk)
                        chunk = b""
                        if line is None:
                            break
                        data = parse_message(line)
                        if data:
                            write_frame(fb, render_data_screen(data, fonts), bpp)
                elif not ready:
                    break
 
            conn.close()
            write_frame(fb, render_waiting("wifi", cfg, fonts), bpp)
            print("Connection lost.")
    finally:
        server.close()

#Dev mode
def run_dev_preview(fonts):
    """Generates a static PNG preview of the data screen on a dev machine."""
    sample = {
        "timestamp": "2026-07-31T12:00:00Z",
        "cpu":     {"usage_percent": 48.2, "temperature_c": 69.8},
        "gpu":     {"usage_percent": 26.0, "temperature_c": 48.0, "available": True},
        "ram":     {"used_gb": 13.0, "total_gb": 31.9, "percent": 40.8},
        "disk":    {"usage_percent": 36.9},
        "network": {"upload_mbps": 28.4, "download_mbps": 954.5},
        "uptime":  {"seconds": 21600, "formatted": "0d 6h 0m"},
        "os":      platform.system(),
    }
    img = render_data_screen(sample, fonts)
    img.save("circuitsense_preview.png")
    print(f"Saved preview to circuitsense_preview.png  ({platform.system()} dev mode)")

#Main
def main():
    fonts = load_fonts()
 
    # Dev machine (no framebuffer) — just render a PNG preview and exit
    if not any(os.path.exists(f"/dev/{n}") for n in ("fb0", "fb1")):
        run_dev_preview(fonts)
        return
 
    cfg   = load_config()
    touch = Touch(cfg.get("touch", {}))
 
    try:
        fb_path, bpp = find_framebuffer()
    except OSError as e:
        print(f"No framebuffer ({e}). Is the display driver installed?")
        sys.exit(1)
    print(f"Framebuffer: {fb_path} at {bpp}-bit")
 
    try:
        fb = open(fb_path, "wb")
    except OSError as e:
        print(f"Could not open {fb_path} ({e}).")
        sys.exit(1)
 
    if "--calibrate" in sys.argv:
        run_calibration(fb, bpp, fonts, touch, cfg)
        fb.close()
        return
 
    try:
        while True:
            mode = select_mode(fb, bpp, fonts, touch, cfg)
            try:
                result = run_usb_loop(cfg, fb, bpp, fonts, touch) \
                         if mode == "usb" else \
                         run_wifi_loop(cfg, fb, bpp, fonts, touch)
            except Exception as e:
                print(f"{mode} mode error ({e}). Returning to mode select.")
                time.sleep(2)
                continue
            if result != 'back':
                break
            print("Returning to mode selection.")
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        fb.close()
 
 
if __name__ == "__main__":
    main()