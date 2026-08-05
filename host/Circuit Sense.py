"""
CircuitSense - Host Companion Application (Circuit Sense.py)
Team Queue5

Runs on the user's Windows or Linux PC. Detects the OS, reads hardware data
through the right libraries for that OS, formats everything into one standard
JSON message, and sends it to the CircuitSense device once per second over
USB serial or Wi-Fi TCP.

Layered structure (SDD 2.3):
  Layer 1 (bottom): OS-specific sensor readers
  Layer 2 (middle): JSON formatting
  Layer 3 (top):    connection and send loop

Dependencies:
  Both OS:   psutil, pyserial
  Windows:   pythonnet + LibreHardwareMonitorLib.dll in the same folder
             (run as administrator to read hardware sensors - SRS 2.5 / NFR-2)
  Linux:     lm-sensors installed, pynvml for NVIDIA GPUs

Covers: SRS FR-1 to FR-12, SDD 3.1 (host), 3.2 (communication), 3.4 (data contract)
"""

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

import psutil

# Step 1 - Startup: import optional OS-specific libraries, suppress ImportError (SDD 3.1)
try:
    import serial  # pyserial, USB mode
except ImportError:
    serial = None

try:
    import pynvml  # Linux NVIDIA GPU data
except ImportError:
    pynvml = None

# Step 2 - OS Detection: detected once, stored as a module-level constant (SDD 3.1, FR-7)
HOST_OS = platform.system()  # 'Windows' or 'Linux'

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config.json")  # SRS 2.5: script and config ship
                                           # in one application folder, so the
                                           # config is found regardless of the
                                           # working directory it launches from
POLL_INTERVAL = 1.0          # seconds between messages (SDD 3.4: 1 message per second)
RETRY_ATTEMPTS = 3           # connection retry (SDD 3.1 Step 4)
RETRY_DELAY = 2.0            # seconds between retries (SDD 3.1 Step 4)
DEFAULT_WIFI_PORT = 65432    # SDD 3.4 protocol table
USB_BAUD = 115200            # SDD 3.4 protocol table


# ---------------------------------------------------------------------------
# Layer 1: sensor readers (bottom layer, all OS-specific code stays here)
# ---------------------------------------------------------------------------

def init_lhm():
    """Windows only. Load LibreHardwareMonitorLib.dll through pythonnet and
    return an opened Computer object, or None if unavailable (SDD 3.1)."""
    if HOST_OS != "Windows":
        return None
    try:
        import clr
        dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "LibreHardwareMonitorLib.dll")
        clr.AddReference(dll_path)
        from LibreHardwareMonitor.Hardware import Computer
        computer = Computer()
        computer.IsCpuEnabled = True
        computer.IsGpuEnabled = True
        computer.Open()
        return computer
    except Exception as e:
        print(f"Warning: LibreHardwareMonitor not available ({e}). "
              f"CPU temperature and GPU data will be unavailable.")
        return None


def read_lhm_sensors(computer):
    """Windows only. One pass over LibreHardwareMonitor hardware. Returns
    (cpu_temp, gpu_usage, gpu_temp), any of which may be None."""
    cpu_temp = None
    gpu_usage = None
    gpu_temp = None
    if computer is None:
        return cpu_temp, gpu_usage, gpu_temp
    try:
        for hw in computer.Hardware:
            hw.Update()
            hw_type = str(hw.HardwareType)
            for sensor in hw.Sensors:
                s_type = str(sensor.SensorType)
                s_name = str(sensor.Name)
                value = sensor.Value
                if value is None:
                    continue
                if hw_type == "Cpu" and s_type == "Temperature":
                    # prefer the package reading, otherwise take the first one
                    if "Package" in s_name or cpu_temp is None:
                        cpu_temp = float(value)
                elif hw_type.startswith("Gpu"):
                    if s_type == "Load" and s_name == "GPU Core":
                        gpu_usage = float(value)
                    elif s_type == "Temperature" and s_name == "GPU Core":
                        gpu_temp = float(value)
    except Exception as e:
        print(f"Warning: LibreHardwareMonitor read failed ({e}).")
    return cpu_temp, gpu_usage, gpu_temp


def read_linux_cpu_temp():
    """Linux only. CPU temperature through lm-sensors (subprocess, SDD 3.4
    library table), with psutil as a fallback. Returns float or None."""
    try:
        result = subprocess.run(["sensors", "-j"], capture_output=True,
                                text=True, timeout=2)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for chip_name, chip in data.items():
                if "coretemp" in chip_name or "k10temp" in chip_name:
                    temp = _find_temp_input(chip)
                    if temp is not None:
                        return temp
            for chip in data.values():
                temp = _find_temp_input(chip)
                if temp is not None:
                    return temp
    except Exception:
        pass
    # fallback: psutil reads the same hwmon interface
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp"):
            if key in temps and temps[key]:
                return float(temps[key][0].current)
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass
    return None


def _find_temp_input(node):
    """Recursive search of a sensors -j chip dict for the first temp*_input value."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("temp") and key.endswith("_input"):
                return float(value)
            found = _find_temp_input(value)
            if found is not None:
                return found
    return None


def init_nvml():
    """Linux only. Initialize pynvml. Returns True if an NVIDIA GPU is usable."""
    if pynvml is None:
        return False
    try:
        pynvml.nvmlInit()
        return pynvml.nvmlDeviceGetCount() > 0
    except Exception:
        return False


def read_nvidia_gpu():
    """Linux/NVIDIA. Returns (usage, temp) through pynvml, or (None, None)."""
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        usage = float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
        temp = float(pynvml.nvmlDeviceGetTemperature(
            handle, pynvml.NVML_TEMPERATURE_GPU))
        return usage, temp
    except Exception:
        return None, None


def read_sysfs_gpu():
    """Linux/AMD-Intel. Reads GPU usage and temperature from the sysfs tree
    (SDD 3.4 library table). Returns (usage, temp), either may be None."""
    usage = None
    temp = None
    try:
        import glob
        for card in glob.glob("/sys/class/drm/card[0-9]*/device"):
            busy_path = os.path.join(card, "gpu_busy_percent")
            if usage is None and os.path.exists(busy_path):
                with open(busy_path) as f:
                    usage = float(f.read().strip())
            if temp is None:
                for temp_path in glob.glob(
                        os.path.join(card, "hwmon/hwmon*/temp1_input")):
                    with open(temp_path) as f:
                        temp = float(f.read().strip()) / 1000.0
                    break
            if usage is not None and temp is not None:
                break
    except Exception:
        pass
    return usage, temp


def read_cpu_usage():
    """CPU utilization percent through psutil, both OS (FR-1)."""
    return psutil.cpu_percent(interval=None)


def read_cpu_temperature(lhm_computer, lhm_cache):
    """CPU temperature routed by OS: LibreHardwareMonitor on Windows,
    lm-sensors on Linux (FR-1, SDD 3.1). Returns float or None."""
    if HOST_OS == "Windows":
        return lhm_cache[0]
    return read_linux_cpu_temp()


def read_gpu(lhm_cache, nvml_ready):
    """GPU usage and temperature routed by OS (FR-2, SDD 3.1 Step 5).
    Returns the gpu section of the payload with available set to False
    when no supported GPU or library is found."""
    if HOST_OS == "Windows":
        usage, temp = lhm_cache[1], lhm_cache[2]
    elif nvml_ready:
        usage, temp = read_nvidia_gpu()
    else:
        usage, temp = read_sysfs_gpu()

    available = usage is not None or temp is not None
    return {
        "usage_percent": round(usage, 1) if usage is not None else None,
        "temperature_c": round(temp, 1) if temp is not None else None,
        "available": available,
    }


def read_ram():
    """RAM data through psutil (FR-3). Returns the ram payload section."""
    vm = psutil.virtual_memory()
    return {
        "used_gb": round(vm.used / (1024 ** 3), 1),
        "total_gb": round(vm.total / (1024 ** 3), 1),
        "percent": round(vm.percent, 1),
    }


def read_disk():
    """Primary disk utilization through psutil (FR-4)."""
    root = os.path.abspath(os.sep)  # 'C:\\' on Windows, '/' on Linux
    return {"usage_percent": round(psutil.disk_usage(root).percent, 1)}


def read_network(net_state):
    """Upload and download throughput in Mbps through psutil counters
    (FR-5). net_state is (previous_counters, previous_time); returns
    (network payload section, new net_state)."""
    counters = psutil.net_io_counters()
    now = time.time()
    prev_counters, prev_time = net_state
    if prev_counters is None:
        upload = download = 0.0
    else:
        elapsed = max(now - prev_time, 0.001)
        upload = (counters.bytes_sent - prev_counters.bytes_sent) * 8 / elapsed / 1e6
        download = (counters.bytes_recv - prev_counters.bytes_recv) * 8 / elapsed / 1e6
    section = {
        "upload_mbps": round(max(upload, 0.0), 1),
        "download_mbps": round(max(download, 0.0), 1),
    }
    return section, (counters, now)


def read_uptime():
    """System uptime through psutil.boot_time() (FR-6, SDD 3.1 Step 5)."""
    seconds = int(time.time() - psutil.boot_time())
    return {"seconds": seconds, "formatted": format_uptime(seconds)}


def format_uptime(seconds):
    """Formats seconds as a human-readable string, e.g. '2d 4h 31m' (SDD 3.4)."""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    return f"{days}d {hours}h {minutes}m"


# ---------------------------------------------------------------------------
# Layer 2: JSON formatter (middle layer)
# ---------------------------------------------------------------------------

def collect_payload(lhm_computer, nvml_ready, net_state):
    """Assembles one complete data payload conforming to the JSON data
    contract in SDD 3.4. Returns (payload dict, new net_state)."""
    lhm_cache = read_lhm_sensors(lhm_computer) if HOST_OS == "Windows" \
        else (None, None, None)
    network, net_state = read_network(net_state)
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu": {
            "usage_percent": round(read_cpu_usage(), 1),
            "temperature_c": _round_or_zero(
                read_cpu_temperature(lhm_computer, lhm_cache)),
        },
        "gpu": read_gpu(lhm_cache, nvml_ready),
        "ram": read_ram(),
        "disk": read_disk(),
        "network": network,
        "uptime": read_uptime(),
        "os": HOST_OS,
    }
    return payload, net_state


def _round_or_zero(value):
    """cpu.temperature_c is not nullable in the data contract; if the sensor
    library is unavailable the host sends 0.0 and has already warned on stdout."""
    return round(value, 1) if value is not None else 0.0


def serialize_payload(payload):
    """Serializes the payload to a UTF-8 encoded, newline-terminated JSON
    byte string (SDD 3.2 framing)."""
    return json.dumps(payload).encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# Configuration manager (config.json and the first-run prompt)
# ---------------------------------------------------------------------------

def check_dependencies(mode):
    """SRS 4.1 (Host Configuration Interface): if a required library is
    unavailable on the detected OS, print clear installation instructions
    rather than letting the application terminate without explanation.
    Returns True if the required libraries for the chosen mode are present."""
    ok = True

    if mode == "usb" and serial is None:
        print("\nRequired library missing: pyserial (needed for USB mode).")
        print("  Install it with:  pip install pyserial")
        print("  Or select Wi-Fi mode instead by deleting config.json "
              "and running this program again.")
        ok = False

    if HOST_OS == "Windows":
        app_dir = os.path.dirname(os.path.abspath(__file__))
        dll = os.path.join(app_dir, "LibreHardwareMonitorLib.dll")
        hidsharp = os.path.join(app_dir, "HidSharp.dll")
        if not os.path.exists(dll) or not os.path.exists(hidsharp):
            missing = [name for name, path in
                       (("LibreHardwareMonitorLib.dll", dll), ("HidSharp.dll", hidsharp))
                       if not os.path.exists(path)]
            print(f"\nOptional component missing: {', '.join(missing)}")
            print("  CPU temperature and all GPU data will be unavailable.")
            print("  To enable them:")
            print("    1. pip install pythonnet")
            print("    2. Download LibreHardwareMonitor and place BOTH "
                  "LibreHardwareMonitorLib.dll and HidSharp.dll in this folder")
            print("    3. Right-click each .dll, Properties, tick Unblock if shown")
            print("    4. Run this program as administrator (SRS NFR-2)")
    else:
        if subprocess.run(["which", "sensors"], capture_output=True).returncode != 0:
            print("\nOptional component missing: lm-sensors "
                  "(needed for CPU temperature on Linux).")
            print("  Install it with:  sudo apt install lm-sensors")
            print("  Then run:         sudo sensors-detect")
        if pynvml is None:
            print("\nOptional library missing: pynvml "
                  "(needed for NVIDIA GPU data on Linux).")
            print("  Install it with:  pip install nvidia-ml-py")
            print("  AMD and Intel GPUs are read through sysfs and need no library.")

    return ok


DEFAULT_CONFIG = {
    "mode": "wifi",
    "wifi": {"device_ip": "", "port": DEFAULT_WIFI_PORT},
    "usb": {"host_port": "COM3", "device_port": "/dev/ttyGS0", "baud": USB_BAUD},
    "touch": {"swap_xy": True, "invert_x": False, "invert_y": True},
}


def load_config():
    """Step 3 - reads the unified config.json shared by host and device. Any
    missing section is filled from the defaults, so a partial file still works.
    Returns None if the file is absent or unreadable (FR-12, SDD 3.1)."""
    if not os.path.exists(CONFIG_FILE):
        return None
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    if cfg.get("mode") not in ("usb", "wifi"):
        return None
    if cfg["mode"] == "wifi" and not cfg["wifi"].get("device_ip"):
        return None
    return cfg


def first_run_setup():
    """First-run flow: console prompt for USB or Wi-Fi plus the connection
    parameters, then writes the unified config.json (FR-11, SDD 3.1)."""
    print("CircuitSense first-run setup")
    print("  [1] USB serial connection")
    print("  [2] Wi-Fi connection")
    while True:
        choice = input("Select connection mode (1 or 2): ").strip()
        if choice in ("1", "2"):
            break
        print("Enter 1 or 2.")

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if choice == "1":
        default_port = "COM3" if HOST_OS == "Windows" else "/dev/ttyACM0"
        port = input(f"Serial port [{default_port}]: ").strip() or default_port
        cfg["mode"] = "usb"
        cfg["usb"]["host_port"] = port
    else:
        host = input("CircuitSense device IP address "
                     "(shown on the device screen): ").strip()
        port_text = input(f"TCP port [{DEFAULT_WIFI_PORT}]: ").strip()
        cfg["mode"] = "wifi"
        cfg["wifi"]["device_ip"] = host
        cfg["wifi"]["port"] = int(port_text) if port_text else DEFAULT_WIFI_PORT

    save_config(cfg)
    print(f"Configuration saved to {CONFIG_FILE}.")
    print("To switch modes later, change the \"mode\" value in config.json "
          "to \"usb\" or \"wifi\".")
    return cfg


def save_config(cfg):
    """Writes the unified configuration object to config.json (SDD 3.1)."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Layer 3: connection (top layer, host side of the Communication Layer)
# ---------------------------------------------------------------------------

def open_connection(cfg):
    """Step 4 - opens the transport chosen in config.json, retrying on
    failure with a fixed backoff: 3 attempts, 2-second delay (SDD 3.1).
    Returns the connection object or None if all attempts fail."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            if cfg["mode"] == "usb":
                conn = open_usb_connection(cfg)
            else:
                conn = open_wifi_connection(cfg)
            print(f"Connected ({cfg['mode']} mode).")
            return conn
        except Exception as e:
            print(f"Connection attempt {attempt}/{RETRY_ATTEMPTS} failed: {e}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    return None


def open_usb_connection(cfg):
    """Opens a pyserial Serial object on the configured port at 115200 baud
    (SDD 3.2 USB mode). Host is the writer."""
    if serial is None:
        raise RuntimeError("pyserial is not installed")
    usb = cfg["usb"]
    return serial.Serial(usb["host_port"], usb.get("baud", USB_BAUD), timeout=3)


def open_wifi_connection(cfg):
    """Connects a TCP client socket to the device's address and port stored
    in config.json (SDD 3.2 Wi-Fi mode). Host is the TCP client."""
    wifi = cfg["wifi"]
    sock = socket.create_connection(
        (wifi["device_ip"], wifi.get("port", DEFAULT_WIFI_PORT)), timeout=3)
    return sock


def send_message(conn, cfg, data):
    """Transmits one newline-terminated JSON byte string over the active
    transport (SDD 3.2): serial.write in USB mode, socket.sendall in Wi-Fi mode."""
    if cfg["mode"] == "usb":
        conn.write(data)
    else:
        conn.sendall(data)


def close_connection(conn, cfg):
    """Closes the active connection cleanly (SDD 3.1 Step 6)."""
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main: startup sequence and polling loop (SDD 3.1 Steps 1-6)
# ---------------------------------------------------------------------------

def main():
    if HOST_OS not in ("Windows", "Linux"):
        print(f"Unsupported operating system: {HOST_OS}. "
              f"CircuitSense supports Windows and Linux.")
        sys.exit(1)
    print(f"CircuitSense host application starting on {HOST_OS}.")

    # SIGTERM triggers the same graceful shutdown path as Ctrl+C (SDD 3.1 Step 6)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    # Step 3 - configuration load / first-run setup
    cfg = load_config()
    if cfg is None:
        cfg = first_run_setup()

    # dependency check with installation instructions (SRS 4.1)
    if not check_dependencies(cfg["mode"]):
        print("\nCannot continue until the missing library above is installed.")
        sys.exit(1)

    # sensor library setup
    lhm_computer = init_lhm()
    nvml_ready = init_nvml() if HOST_OS == "Linux" else False
    if HOST_OS == "Linux" and not nvml_ready and read_sysfs_gpu() == (None, None):
        print("Warning: no supported GPU found. GPU data will show as unavailable.")

    # Step 4 - connection establishment
    conn = open_connection(cfg)
    if conn is None:
        print("Could not connect to the CircuitSense device. Exiting.")
        sys.exit(1)

    # Step 5 - polling loop, one message per second
    net_state = (None, None)
    psutil.cpu_percent(interval=None)  # prime the CPU counter
    print("Sending data every 1 second. Press Ctrl+C to stop.")
    try:
        while True:
            cycle_start = time.time()
            payload, net_state = collect_payload(lhm_computer, nvml_ready, net_state)
            data = serialize_payload(payload)
            try:
                send_message(conn, cfg, data)
            except Exception as e:
                # on transmission error: log a warning, attempt reconnection,
                # continue the loop (SDD 3.1 Step 5)
                print(f"Warning: transmission failed ({e}). Reconnecting...")
                close_connection(conn, cfg)
                conn = open_connection(cfg)
                if conn is None:
                    print("Reconnection failed. Retrying next cycle.")
                    conn = _dead_connection(cfg)
            elapsed = time.time() - cycle_start
            time.sleep(max(POLL_INTERVAL - elapsed, 0))
    except KeyboardInterrupt:
        # Step 6 - graceful shutdown
        print("\nShutting down.")
    finally:
        if conn is not None:
            close_connection(conn, cfg)
        if lhm_computer is not None:
            try:
                lhm_computer.Close()
            except Exception:
                pass


class _dead_connection:
    """Placeholder connection that always fails to send, so the loop keeps
    retrying reconnection once per cycle without special-casing None."""

    def __init__(self, cfg):
        self.cfg = cfg

    def write(self, data):
        raise ConnectionError("no active connection")

    def sendall(self, data):
        raise ConnectionError("no active connection")

    def close(self):
        pass


if __name__ == "__main__":
    main()
