import asyncio
import ctypes
import fractions
import json
import math
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from aiohttp import web
from av import AudioFrame
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.contrib.media import MediaPlayer
from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
from pynput.mouse import Controller as MouseController, Button

HOST = "127.0.0.1"
PORT = 8765
BUILD_VERSION = "2026.10.06.1"
FPS = int(os.environ.get("DK_FPS", "60"))
WIDTH = 1366
HEIGHT = 768
# In virtual mode, identify the MttVDD monitor by its PnP identity each time.
# A manual DXGI index can still be used for troubleshooting.
CAPTURE_DISPLAY = os.environ.get("DK_CAPTURE_DISPLAY", "virtual").lower()
if CAPTURE_DISPLAY not in ("virtual", "manual"):
    raise ValueError("DK_CAPTURE_DISPLAY must be 'virtual' or 'manual'")
DISPLAY_OUTPUT_INDEX = int(os.environ.get("DK_DISPLAY_OUTPUT_INDEX", "0"))
# Direct mode keeps D3D11 capture frames on the GPU. The selected desktop
# is set to the controller's resolution before opening capture.
CAPTURE_MODE = os.environ.get("DK_CAPTURE_MODE", "direct").lower()
if CAPTURE_MODE not in ("direct", "scale"):
    raise ValueError("DK_CAPTURE_MODE must be 'direct' or 'scale'")
BITRATE = os.environ.get("DK_BITRATE", "14M")
MAXRATE = os.environ.get("DK_MAXRATE", "20M")
# Four frames of peak bitrate leave more room for keyframes, reducing the
# quality swing at each IDR. This is a burst budget, not a playback delay.
VBV_BUFFER = os.environ.get("DK_VBV_BUFFER")
RATE_MODE = os.environ.get("DK_RATE_MODE", "vbr").lower()
if RATE_MODE not in ("vbr", "cbr"):
    raise ValueError("DK_RATE_MODE must be 'vbr' or 'cbr'")
# Historical DK_CQ now selects the initial VBR quantizer. FFmpeg's -cq mode
# disables the explicitly configured VBV, which is unsuitable for this path.
CQ = int(os.environ.get("DK_CQ", "14"))
if not 0 <= CQ <= 51:
    raise ValueError("DK_CQ must be between 0 and 51")
EFFECTIVE_RATE_MODE = RATE_MODE
QUALITY_MODE = "sharp"
AUDIO_ENABLED = True
GOP = int(os.environ.get("DK_GOP", str(max(1, FPS))))
if FPS < 1 or GOP < 1:
    raise ValueError("DK_FPS and DK_GOP must be positive integers")
BASE = Path(__file__).resolve().parent
SECRET_FILE = BASE / "remote_secret.txt"
SETTINGS_FILE = BASE / "remote_settings.json"
FFMPEG_EXE = None
SECRET = SECRET_FILE.read_text(encoding="utf-8").strip() if SECRET_FILE.exists() else secrets.token_urlsafe(32)
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(SECRET, encoding="utf-8")
try:
    saved_settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    if isinstance(saved_settings, dict) and saved_settings.get("quality") in ("sharp", "balanced"):
        QUALITY_MODE = saved_settings["quality"]
    if isinstance(saved_settings, dict) and isinstance(saved_settings.get("audio"), bool):
        AUDIO_ENABLED = saved_settings["audio"]
except (OSError, ValueError, TypeError):
    pass

pcs = set()
sessions = {}
keyboard = KeyboardController()
mouse = MouseController()
escape_task = None
PLAYOUT_URI = "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay"
PLAYOUT_MAX_MS = max(0, min(40950, int(os.environ.get("DK_PLAYOUT_MAX_MS", "80")))) // 10 * 10
HOST_QUEUE_MS = max(0, int(os.environ.get("DK_HOST_QUEUE_MS", "50")))
HOST_QUEUE_FRAMES = max(2, math.ceil(FPS * HOST_QUEUE_MS / 1000))
# One FFmpeg video frame per PES packet, with its own timestamp. Avoid the
# general H.264 parser's next-frame lookahead before handing packets to RTP.
# The legacy path remains available for unusual FFmpeg/PyAV builds.
LOW_DELAY_DEMUX = os.environ.get("DK_LOW_DELAY_DEMUX", "1") != "0"
H264_NAL_HEADER = re.compile(rb"\x00\x00\x01(.)", re.DOTALL)


def mark_h264_keyframe(packet):
    # With noparse, the container does not reliably mark IDRs. Detect them
    # directly so backlog recovery still skips only to a decodable frame.
    if LOW_DELAY_DEMUX and not packet.is_keyframe:
        packet.is_keyframe = any((match[1][0] & 31) == 5
                                 for match in H264_NAL_HEADER.finditer(bytes(packet)))
    return packet


def is_admin():
    try:
        return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def request_admin_restart():
    """Use Windows' normal UAC prompt at startup or from the local host button."""
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.restype = ctypes.c_void_p
    args = [str(Path(__file__).resolve()), *sys.argv[1:]]
    if "--wait-for-host" not in args:
        args.append("--wait-for-host")
    result = shell(None, "runas", sys.executable, subprocess.list2cmdline(args), str(BASE), 1)
    return result is not None and result > 32


class LowDelayExtensions:
    """Keep aiortc's negotiated RTP extensions and add a playout-delay hint."""

    def __init__(self, original):
        self.original = original
        self.extension_id = None

    def configure(self, parameters):
        self.original.configure(parameters)
        self.extension_id = next((ext.id for ext in parameters.headerExtensions
                                  if ext.uri == PLAYOUT_URI), None)

    def get(self, profile, data):
        return self.original.get(profile, data)

    def set(self, values):
        from aiortc.rtp import pack_header_extensions, unpack_header_extensions
        profile, data = self.original.set(values)
        if self.extension_id is None:
            return profile, data
        extensions = [(ext_id, payload) for ext_id, payload in
                      unpack_header_extensions(profile, data) if ext_id != self.extension_id]
        # Twelve-bit minimum/maximum, in units of 10 ms. Minimum is zero.
        extensions.append((self.extension_id, (PLAYOUT_MAX_MS // 10).to_bytes(3, "big")))
        return pack_header_extensions(extensions)


def install_low_delay_sender(sender):
    # aiortc does not expose this extension as a public sender option. Guard
    # the adapter so an incompatible future version keeps ordinary WebRTC.
    try:
        from aiortc.codecs import HEADER_EXTENSIONS
        from aiortc.rtcrtpparameters import RTCRtpHeaderExtensionParameters
        from aiortc.rtp import pack_header_extensions, unpack_header_extensions  # noqa: F401
        attribute = "_RTCRtpSender__rtp_header_extensions_map"
        original = getattr(sender, attribute)
        if not all(hasattr(original, method) for method in ("configure", "get", "set")):
            raise RuntimeError("Unrecognized RTP extension map")
        if not any(ext.uri == PLAYOUT_URI for ext in HEADER_EXTENSIONS["video"]):
            used = {ext.id for group in HEADER_EXTENSIONS.values() for ext in group}
            identifier = next(i for i in range(1, 15) if i not in used)
            HEADER_EXTENSIONS["video"].append(RTCRtpHeaderExtensionParameters(
                id=identifier, uri=PLAYOUT_URI))
        adapter = LowDelayExtensions(original)
        setattr(sender, attribute, adapter)
        return adapter
    except (ImportError, AttributeError, RuntimeError, StopIteration) as exc:
        print("[latency] Sender playout hint unavailable:", exc)
        return None


class DisplayDevice(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("DeviceName", ctypes.c_wchar * 32),
                ("DeviceString", ctypes.c_wchar * 128), ("StateFlags", ctypes.c_ulong),
                ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


class Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class DxgiOutputDesc(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", Rect),
                ("AttachedToDesktop", ctypes.c_int), ("Rotation", ctypes.c_uint),
                ("Monitor", ctypes.c_void_p)]


class Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]


class DisplayMode(ctypes.Structure):
    """DEVMODEW; retain the position/orientation union when changing size."""

    _fields_ = [("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", ctypes.c_ushort), ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort), ("dmDriverExtra", ctypes.c_ushort),
                ("dmFields", ctypes.c_uint32), ("displayUnion", ctypes.c_byte * 16),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_uint32),
                ("dmPelsWidth", ctypes.c_uint32), ("dmPelsHeight", ctypes.c_uint32),
                ("dmDisplayFlags", ctypes.c_uint32), ("dmDisplayFrequency", ctypes.c_uint32),
                ("dmICMMethod", ctypes.c_uint32), ("dmICMIntent", ctypes.c_uint32),
                ("dmMediaType", ctypes.c_uint32), ("dmDitherType", ctypes.c_uint32),
                ("dmReserved1", ctypes.c_uint32), ("dmReserved2", ctypes.c_uint32),
                ("dmPanningWidth", ctypes.c_uint32), ("dmPanningHeight", ctypes.c_uint32)]


def controller_resolution(screen):
    if screen is None:
        return WIDTH, HEIGHT  # Previous HTML clients still work.
    if not isinstance(screen, dict):
        raise ValueError("Controller screen must contain width and height")
    width, height = screen.get("width"), screen.get("height")
    if (type(width) not in (int, float) or type(height) not in (int, float) or
            not 320 <= width <= 8192 or not 200 <= height <= 4320):
        raise ValueError("Controller screen dimensions are invalid")
    # H.264 4:2:0 requires even dimensions; this changes odd sizes by one pixel.
    return int(round(width / 2)) * 2, int(round(height / 2)) * 2


def ranked_display_modes(modes, requested):
    width, height = requested
    ratio = width / height

    def score(mode):
        size_error = abs(mode.dmPelsWidth - width) / width + abs(mode.dmPelsHeight - height) / height
        aspect_error = abs(mode.dmPelsWidth / mode.dmPelsHeight - ratio) / ratio
        return (size_error + 3 * aspect_error, abs(mode.dmDisplayFrequency - FPS))

    return sorted((mode for mode in modes if mode.dmBitsPerPel == 32 and
                   mode.dmPelsWidth >= 320 and mode.dmPelsHeight >= 200 and
                   mode.dmPelsWidth % 2 == 0 and mode.dmPelsHeight % 2 == 0 and
                   mode.dmDisplayFrequency >= min(60, FPS)), key=score)


def set_virtual_resolution(selected, requested):
    """Choose only driver-supported modes on the identified virtual monitor."""
    user32 = ctypes.windll.user32
    enum = user32.EnumDisplaySettingsW
    enum.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(DisplayMode)]
    enum.restype = ctypes.c_int
    change = user32.ChangeDisplaySettingsExW
    change.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(DisplayMode), ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_void_p]
    change.restype = ctypes.c_int32
    current = DisplayMode()
    current.dmSize = ctypes.sizeof(current)
    if not enum(selected["name"], 0xffffffff, ctypes.byref(current)):
        raise RuntimeError("Could not read the virtual monitor's current mode")
    if ((current.dmPelsWidth, current.dmPelsHeight) == requested and
            current.dmDisplayFrequency >= min(60, FPS)):
        return requested
    modes = []
    index = 0
    while True:
        mode = DisplayMode()
        mode.dmSize = ctypes.sizeof(mode)
        if not enum(selected["name"], index, ctypes.byref(mode)):
            break
        modes.append(mode)
        index += 1
    for mode in ranked_display_modes(modes, requested):
        # Retain desktop position and orientation from the current mode.
        target = DisplayMode.from_buffer_copy(current)
        target.dmPelsWidth, target.dmPelsHeight = mode.dmPelsWidth, mode.dmPelsHeight
        target.dmDisplayFrequency = mode.dmDisplayFrequency
        target.dmBitsPerPel = 32
        target.dmFields |= 0x00040000 | 0x00080000 | 0x00100000 | 0x00400000
        if change(selected["name"], ctypes.byref(target), None, 2, None) != 0:  # CDS_TEST
            continue
        if change(selected["name"], ctypes.byref(target), None, 0, None) == 0:
            return target.dmPelsWidth, target.dmPelsHeight
    print("[display] No supported mode could be applied; retaining the current virtual resolution.")
    return current.dmPelsWidth, current.dmPelsHeight


async def capture_for_controller(requested):
    selected = await asyncio.to_thread(select_capture_display)
    if CAPTURE_DISPLAY == "virtual" and not sessions:
        target = await asyncio.to_thread(set_virtual_resolution, selected, requested)
        # DXGI output indexes/bounds can change when Windows changes a mode.
        # Re-enumerate by virtual monitor identity before opening ddagrab.
        for _ in range(30):
            selected = await asyncio.to_thread(select_capture_display)
            if (selected["width"], selected["height"]) == target:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Windows has not finished applying the virtual display mode")
    selected["requestedWidth"], selected["requestedHeight"] = requested
    actual = selected["width"], selected["height"]
    selected["resolutionNote"] = ("" if actual == requested else
        f"Controller requested {requested[0]}×{requested[1]}; streaming {actual[0]}×{actual[1]}. " +
        ("Another controller is using the virtual display." if sessions else
         "This is the closest usable mode offered by the virtual display driver."))
    print(f'[display] Controller {requested[0]}x{requested[1]} → stream {actual[0]}x{actual[1]}')
    return selected


def _com_method(pointer, index, result, *arguments):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[index])


def _com_release(pointer):
    _com_method(pointer, 2, ctypes.c_ulong)(pointer)


def windows_virtual_monitors():
    user32 = ctypes.windll.user32
    enum = user32.EnumDisplayDevicesW
    enum.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                     ctypes.POINTER(DisplayDevice), ctypes.c_ulong]
    enum.restype = ctypes.c_int
    monitors = {}
    adapter_index = 0
    while True:
        adapter = DisplayDevice()
        adapter.cb = ctypes.sizeof(adapter)
        if not enum(None, adapter_index, ctypes.byref(adapter), 0):
            break
        monitor_index = 0
        while True:
            monitor = DisplayDevice()
            monitor.cb = ctypes.sizeof(monitor)
            if not enum(adapter.DeviceName, monitor_index, ctypes.byref(monitor), 0):
                break
            identity = (monitor.DeviceID + " " + monitor.DeviceString + " " +
                        adapter.DeviceString).lower()
            if "mtt1337" in identity or "mttvdd" in identity or "virtual display driver" in identity:
                monitors[adapter.DeviceName.upper()] = monitor.DeviceID or monitor.DeviceString
            monitor_index += 1
        adapter_index += 1
    return monitors


def dxgi_outputs():
    iid = Guid((ctypes.c_byte * 16).from_buffer_copy(
        uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le))
    factory = ctypes.c_void_p()
    create = ctypes.windll.dxgi.CreateDXGIFactory1
    create.argtypes = [ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long
    result = create(ctypes.byref(iid), ctypes.byref(factory))
    if result < 0:
        raise RuntimeError(f"CreateDXGIFactory1 failed (0x{result & 0xffffffff:08x})")
    outputs = []
    try:
        adapter_index = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = _com_method(factory, 7, ctypes.c_long, ctypes.c_uint,
                             ctypes.POINTER(ctypes.c_void_p))(
                                 factory, adapter_index, ctypes.byref(adapter))
            if hr < 0:
                break
            try:
                output_index = 0
                while True:
                    output = ctypes.c_void_p()
                    hr = _com_method(adapter, 7, ctypes.c_long, ctypes.c_uint,
                                     ctypes.POINTER(ctypes.c_void_p))(
                                         adapter, output_index, ctypes.byref(output))
                    if hr < 0:
                        break
                    try:
                        desc = DxgiOutputDesc()
                        hr = _com_method(output, 7, ctypes.c_long,
                                         ctypes.POINTER(DxgiOutputDesc))(
                                             output, ctypes.byref(desc))
                        if hr >= 0 and desc.AttachedToDesktop:
                            bounds = desc.DesktopCoordinates
                            outputs.append({"adapter": adapter_index, "output": output_index,
                                            "name": desc.DeviceName.upper(),
                                            "bounds": (bounds.left, bounds.top, bounds.right, bounds.bottom)})
                    finally:
                        _com_release(output)
                    output_index += 1
            finally:
                _com_release(adapter)
            adapter_index += 1
    finally:
        _com_release(factory)
    return outputs


def select_capture_display():
    if CAPTURE_DISPLAY == "manual":
        return {"adapter": 0, "output": DISPLAY_OUTPUT_INDEX,
                "name": "manual DXGI selection", "bounds": None, "identity": "manual",
                "width": WIDTH, "height": HEIGHT}
    monitors = windows_virtual_monitors()
    matches = [dict(output, identity=monitors[output["name"]])
               for output in dxgi_outputs() if output["name"] in monitors]
    if len(matches) != 1:
        detail = "not found" if not matches else "multiple virtual monitors active"
        raise RuntimeError("MttVDD virtual display " + detail +
                           ". Activate exactly one virtual display; "
                           "refusing to stream a physical monitor.")
    selected = matches[0]
    bounds = selected["bounds"]
    selected["width"], selected["height"] = bounds[2] - bounds[0], bounds[3] - bounds[1]
    return selected


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("data", InputUnion)]


class KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class KeyInputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]


class KeyInput(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("data", KeyInputUnion)]


def inject_event(event):
    user32 = ctypes.windll.user32
    if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
        if is_admin():
            raise RuntimeError("Windows rejected input while the host is already administrator. "
                               "UAC screens still require approval on the PC. "
                               "If this is Task Manager, send the host's [input] error log.")
        raise RuntimeError("Windows blocked input: the host is not administrator. "
                           "Restart it and approve the Windows prompt on the PC before connecting.")


def inject_virtual_key(vk, down):
    if os.name != "nt":
        (keyboard.press if down else keyboard.release)(KeyCode.from_vk(vk))
        return
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    extended = vk in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                      0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
    flags = (0 if down else 0x0002) | (0x0001 if extended else 0)
    event = KeyInput(1, KeyInputUnion(ki=KeyboardInput(vk, scan, flags, 0, 0)))
    inject_event(event)


CODE_VK = {
    "Escape": 0x1B, "Backquote": 0xC0, "Backspace": 0x08, "Tab": 0x09,
    "Enter": 0x0D, "Space": 0x20, "CapsLock": 0x14,
    "ShiftLeft": 0xA0, "ShiftRight": 0xA1, "ControlLeft": 0xA2, "ControlRight": 0xA3,
    "AltLeft": 0xA4, "AltRight": 0xA5, "MetaLeft": 0x5B, "MetaRight": 0x5C,
    "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28,
    "PageUp": 0x21, "PageDown": 0x22, "End": 0x23, "Home": 0x24,
    "Insert": 0x2D, "Delete": 0x2E, "PrintScreen": 0x2C, "Pause": 0x13,
    "ScrollLock": 0x91, "NumLock": 0x90, "ContextMenu": 0x5D,
    "Minus": 0xBD, "Equal": 0xBB, "BracketLeft": 0xDB, "BracketRight": 0xDD,
    "Backslash": 0xDC, "Semicolon": 0xBA, "Quote": 0xDE, "Comma": 0xBC,
    "Period": 0xBE, "Slash": 0xBF, "NumpadAdd": 0x6B, "NumpadSubtract": 0x6D,
    "NumpadMultiply": 0x6A, "NumpadDivide": 0x6F, "NumpadDecimal": 0x6E,
}
CODE_VK.update({"Key" + chr(i): i for i in range(ord("A"), ord("Z") + 1)})
CODE_VK.update({"Digit" + str(i): ord(str(i)) for i in range(10)})
CODE_VK.update({"Numpad" + str(i): 0x60 + i for i in range(10)})
CODE_VK.update({"F" + str(i): 0x6F + i for i in range(1, 25)})


def inject_button(button, down):
    if os.name == "nt":
        flags = {Button.left: (0x0002, 0x0004), Button.right: (0x0008, 0x0010),
                 Button.middle: (0x0020, 0x0040)}[button][not down]
        inject_event(Input(0, InputUnion(mi=MouseInput(0, 0, 0, flags, 0, 0))))
    else:
        (mouse.press if down else mouse.release)(button)


def windows_scroll_amount(horizontal=False):
    if os.name != "nt":
        return 3
    value = ctypes.c_uint(3)
    # SPI_GETWHEELSCROLLCHARS / SPI_GETWHEELSCROLLLINES.
    command = 0x006C if horizontal else 0x0068
    if not ctypes.windll.user32.SystemParametersInfoW(command, 0, ctypes.byref(value), 0):
        return 3
    return value.value


class HostInputState:
    """Inject the HTML's requested keys verbatim and retain fractional scroll."""

    def __init__(self, page_height=HEIGHT):
        self.keys = {}
        self.buttons = set()
        self.last_error = 0
        self.scroll_remainder = [0.0, 0.0]
        self.page_height = page_height

    def key(self, name, code, down):
        identity = code or name
        token = self.keys.get(identity) if not down else None
        if token is None:
            if os.name == "nt" and code in CODE_VK:
                token = ("vk", CODE_VK[code])
            elif name == "Escape":
                token = ("vk", 0x1B)
            elif name in ("`", "~"):
                token = ("vk", 0xC0)
            else:
                key = resolve_key(name)
                if key is None:
                    return
                value = getattr(key, "value", key)
                vk = getattr(value, "vk", None)
                token = ("vk", vk) if os.name == "nt" and vk is not None else ("key", key)
        if token[0] == "vk":
            inject_virtual_key(token[1], down)
        else:
            (keyboard.press if down else keyboard.release)(token[1])
        if down:
            self.keys[identity] = token
        else:
            self.keys.pop(identity, None)

    def release_all(self):
        for token in list(self.keys.values()):
            try:
                if token[0] == "vk":
                    inject_virtual_key(token[1], False)
                else:
                    keyboard.release(token[1])
            except Exception:
                pass
        self.keys.clear()
        for button in list(self.buttons):
            try:
                inject_button(button, False)
            except Exception:
                pass
        self.buttons.clear()
        self.scroll_remainder = [0.0, 0.0]

    def scroll_pixels(self, dx, dy, line_pixels=16):
        line_pixels = max(8.0, min(64.0, float(line_pixels)))
        for axis, pixels in enumerate((dx, dy)):
            pixels = max(-10000.0, min(10000.0, float(pixels)))
            amount = windows_scroll_amount(horizontal=axis == 0)
            if amount == 0:
                continue
            pixels_per_notch = self.page_height if amount == 0xFFFFFFFF else max(1, amount) * line_pixels
            # Browser +Y is down; Windows positive WHEEL is up. Positive
            # browser +X and Windows HWHEEL both scroll to the right.
            units = pixels * 120.0 / pixels_per_notch * (1 if axis == 0 else -1)
            total = units + self.scroll_remainder[axis]
            whole = int(total)
            self.scroll_remainder[axis] = total - whole
            if not whole:
                continue
            if os.name == "nt":
                flags = 0x1000 if axis == 0 else 0x0800
                inject_event(Input(0, InputUnion(mi=MouseInput(0, 0, whole & 0xFFFFFFFF, flags, 0, 0))))
            else:
                # Non-Windows fallback receives whole notches.
                notches = int(whole / 120)
                if notches:
                    mouse.scroll(notches if axis == 0 else 0, notches if axis == 1 else 0)


def host_mouse_state(selected=None):
    """Clipping indicates many games' mouse capture; raw-input-only games may not clip."""
    if os.name != "nt":
        return {"locked": None, "method": "unavailable"}
    user32 = ctypes.windll.user32
    clip = Rect()
    if not user32.GetClipCursor(ctypes.byref(clip)):
        return {"locked": None, "method": "unavailable"}
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    full = (left, top, left + width, top + height)
    rectangle = (clip.left, clip.top, clip.right, clip.bottom)
    confined = width > 0 and height > 0 and rectangle != full
    bounds = selected["bounds"] if selected else full
    on_stream = (clip.right > bounds[0] and clip.left < bounds[2] and
                 clip.bottom > bounds[1] and clip.top < bounds[3])
    return {"locked": bool(confined and on_stream), "method": "Windows cursor clipping"}


def move_mouse_relative(dx, dy):
    dx = max(-2048, min(2048, int(dx)))
    dy = max(-2048, min(2048, int(dy)))
    if dx or dy:
        event = Input(0, InputUnion(mi=MouseInput(dx, dy, 0, 0x0001, 0, 0)))
        inject_event(event)


class LatestH264Track(MediaStreamTrack):
    """Discard stale compressed packets only at a decodable keyframe."""

    kind = "video"

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.pending = deque()
        self.skipped = 0
        self.peak_queue = 0
        self.sent_times = deque()
        self.changed = asyncio.Event()
        self.stopped = False
        self.last_pts = None
        self.last_send_time = None
        self.pts_offset = 0
        self.rebase = False

    def replace_source(self, source, first_packet):
        # A new NVENC process begins with SPS/PPS/IDR. Switch at that IDR,
        # keeping the same RTP sender and its strictly increasing clock.
        self.skipped += self.queued()
        self.source = source
        self.pending.clear()
        self.pending.append(first_packet)
        self.rebase = True
        self.changed.set()

    async def source_packet(self):
        while not self.stopped:
            source = self.source
            self.changed.clear()
            read = asyncio.create_task(source.recv())
            changed = asyncio.create_task(self.changed.wait())
            try:
                await asyncio.wait((read, changed), return_when=asyncio.FIRST_COMPLETED)
                if source is self.source and not self.stopped:
                    return read.result()
            finally:
                for task in (read, changed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(read, changed, return_exceptions=True)
            if self.pending:
                return self.pending.popleft()
        raise MediaStreamError

    def queued(self):
        return len(self.pending) + self.source._queue.qsize()

    def sent_rate(self):
        now = time.monotonic()
        while self.sent_times and self.sent_times[0] < now - 2:
            self.sent_times.popleft()
        if len(self.sent_times) < 2:
            return None
        return round((len(self.sent_times) - 1) / (self.sent_times[-1] - self.sent_times[0]), 1)

    async def recv(self):
        # MediaPlayer's MPEG-TS demux queue is unbounded. Preserve dependencies
        # by seeking the newest IDR, then send every packet after that IDR.
        queued = self.queued()
        self.peak_queue = max(self.peak_queue, queued)
        if queued > HOST_QUEUE_FRAMES:
            packets = list(self.pending)
            self.pending.clear()
            while True:
                try:
                    item = self.source._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    self.source._queue.put_nowait(None)
                    break
                packets.append(item)
            keyframes = [i for i, packet in enumerate(packets)
                         if mark_h264_keyframe(packet).is_keyframe]
            if keyframes:
                start = keyframes[-1]
                self.skipped += start
                self.pending.extend(packets[start:])
            else:
                self.pending.extend(packets)
        packet = self.pending.popleft() if self.pending else await self.source_packet()
        mark_h264_keyframe(packet)
        now = time.monotonic()
        if hasattr(packet, "pts") and packet.pts is not None and packet.time_base is not None:
            pts = round(packet.pts * packet.time_base * 90000)
            if self.rebase:
                gap = max(1, round((now - self.last_send_time) * 90000)) if self.last_send_time else 1500
                self.pts_offset = (self.last_pts + gap if self.last_pts is not None else 0) - pts
                self.rebase = False
            pts += self.pts_offset
            if self.last_pts is not None:
                pts = max(self.last_pts + 1, pts)
            packet.pts = packet.dts = pts
            packet.time_base = fractions.Fraction(1, 90000)
            self.last_pts = pts
        self.last_send_time = now
        self.sent_times.append(now)
        return packet

    def stop(self):
        super().stop()
        self.stopped = True
        self.changed.set()
        # CapturePipeline ends FFmpeg before joining MediaPlayer's pipe reader.


class SpeakerLoopbackTrack(MediaStreamTrack):
    """Capture the Windows default playback endpoint without blocking the event loop."""

    kind = "audio"
    RATE = 48000
    SAMPLES = 960  # 20 ms, matching aiortc's Opus packet cadence.

    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.queue = asyncio.Queue(maxsize=2)
        self.stopping = threading.Event()
        self.started = False
        self.pts = 0
        self.error = None
        self.device = "starting"
        self.captured = 0
        self.last_capture = None
        self.last_sent = None
        self.level = 0.0
        self.pending_audio = deque(maxlen=2)
        self.audio_lock = threading.Lock()
        self.wake_scheduled = False

    def status(self):
        if self.error:
            return self.error
        if self.last_capture is None:
            return "starting"
        return "streaming" if time.monotonic() - self.last_capture < 1 else "capture stalled"

    def publish(self, data):
        # A bounded asyncio queue alone doesn't bound the callbacks queued
        # by a capture thread while the event loop is busy.
        with self.audio_lock:
            self.pending_audio.append(data)
            if self.wake_scheduled:
                return
            self.wake_scheduled = True
        self.loop.call_soon_threadsafe(self.drain_audio)

    def drain_audio(self):
        with self.audio_lock:
            pending = list(self.pending_audio)
            self.pending_audio.clear()
            self.wake_scheduled = False
        for data in pending:
            self._enqueue(data)

    def _enqueue(self, data):
        if self.stopping.is_set():
            return
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(data)

    def _capture(self):
        try:
            import soundcard as sc
            import numpy as np
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("Windows has no default playback device")
            microphone = sc.get_microphone(speaker.id, include_loopback=True)
            if microphone is None or not microphone.isloopback:
                raise RuntimeError("WASAPI loopback for the default speaker was not found")
            self.device = speaker.name
            print("[audio] Capturing default playback device:", self.device)
            # WASAPI may ignore the requested blocksize. Keep the queue bounded
            # so delayed capture never accumulates stale sound.
            with microphone.recorder(samplerate=self.RATE, channels=2, blocksize=960) as recorder:
                pending = np.empty((0, 2), dtype="float32")
                capture_pts = 0
                clock_origin = time.monotonic()
                while not self.stopping.is_set():
                    # Read the available WASAPI data, rather than building an
                    # additional fixed-size SoundCard backlog under host load.
                    samples = recorder.record(numframes=None)
                    if not len(samples):
                        continue
                    samples = np.nan_to_num(samples, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
                    now = time.monotonic()
                    self.last_capture = now
                    self.level = min(1.0, float(np.max(np.abs(samples))))
                    pending = np.concatenate((pending, samples), axis=0)
                    # Keep the audio clock advancing across discarded chunks.
                    # Re-anchor after a device/capture stall, not every read.
                    wall_pts = round((now - clock_origin) * self.RATE) - len(pending)
                    if wall_pts - capture_pts > self.RATE // 10:
                        capture_pts = wall_pts
                    discard = max(0, len(pending) // self.SAMPLES - 2) * self.SAMPLES
                    if discard:
                        pending = pending[discard:]
                        capture_pts += discard
                    while len(pending) >= self.SAMPLES:
                        block, pending = pending[:self.SAMPLES], pending[self.SAMPLES:]
                        pcm = (np.clip(block, -1, 1) * 32767).astype("<i2", copy=False)
                        self.publish((capture_pts, pcm.tobytes()))
                        capture_pts += self.SAMPLES
                        self.captured += 1
        except Exception as exc:
            if not self.stopping.is_set():
                self.error = str(exc)
                print("[audio] Loopback stopped:", repr(exc))
                self.publish(None)

    async def recv(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self._capture, name="DK speaker loopback", daemon=True).start()
        data = await self.queue.get()
        if data is None:
            raise RuntimeError(self.error or "Speaker loopback stopped")
        pts, pcm = data
        frame = AudioFrame(format="s16", layout="stereo", samples=self.SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate = self.RATE
        frame.pts = pts
        frame.time_base = fractions.Fraction(1, self.RATE)
        self.pts = pts + self.SAMPLES
        self.last_sent = time.monotonic()
        return frame

    def stop(self):
        self.stopping.set()
        super().stop()
KEY_MAP = {
    "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab,
    "Escape": Key.esc, "Shift": Key.shift, "Control": Key.ctrl,
    "Alt": Key.alt, "Meta": Key.cmd, "CapsLock": Key.caps_lock,
    "Delete": Key.delete, "Insert": Key.insert, "Home": Key.home,
    "End": Key.end, "PageUp": Key.page_up, "PageDown": Key.page_down,
    "ArrowUp": Key.up, "ArrowDown": Key.down, "ArrowLeft": Key.left,
    "ArrowRight": Key.right, "PrintScreen": Key.print_screen,
    "Pause": Key.pause, "ScrollLock": Key.scroll_lock, "NumLock": Key.num_lock,
}
for i in range(1, 13):
    KEY_MAP["F" + str(i)] = getattr(Key, "f" + str(i))


def resolve_key(name):
    if name in KEY_MAP:
        return KEY_MAP[name]
    if name == " ":
        return Key.space
    if isinstance(name, str) and len(name) == 1:
        return name
    return None


def screen_size():
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def ffmpeg_path():
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found. Install it with: winget install -e --id Gyan.FFmpeg")
    return path


def check_ffmpeg(exe):
    global EFFECTIVE_RATE_MODE
    try:
        filters = subprocess.run([exe, "-hide_banner", "-filters"],
                                 capture_output=True, text=True, timeout=10)
        encoders = subprocess.run([exe, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True, timeout=10)
        encoder_options = subprocess.run([exe, "-hide_banner", "-h", "encoder=h264_nvenc"],
                                         capture_output=True, text=True, timeout=10)
    except Exception as exc:
        raise RuntimeError("Could not inspect FFmpeg: " + str(exc))
    filter_text = filters.stdout + filters.stderr
    encoder_text = encoders.stdout + encoders.stderr
    missing = []
    required_filters = ("ddagrab", "scale") if CAPTURE_MODE == "scale" else ("ddagrab",)
    for name in required_filters:
        if name not in filter_text:
            missing.append(name)
    if "h264_nvenc" not in encoder_text:
        missing.append("h264_nvenc")
    if missing:
        raise RuntimeError("FFmpeg is missing required support: " + ", ".join(missing))
    help_text = encoder_options.stdout + encoder_options.stderr
    if RATE_MODE == "vbr" and (encoder_options.returncode != 0 or
                               not re.search(r"(?m)^\s+-init_qpP\s", help_text) or
                               not re.search(r"(?m)^\s+vbr\s", help_text)):
        EFFECTIVE_RATE_MODE = "cbr"
        print("[capture] This FFmpeg build lacks NVENC bounded VBR; using CBR quality fallback.")
    return True


def bitrate_bits(value):
    """Parse the decimal bit-rate units accepted by the encoder settings."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*", str(value))
    if not match:
        raise ValueError("Bitrate must be a number optionally followed by k, M, or G")
    bits = float(match[1]) * {"": 1, "k": 1000, "m": 1000000, "g": 1000000000}[match[2].lower()]
    if not math.isfinite(bits) or bits <= 0:
        raise ValueError("Bitrate must be positive")
    return math.ceil(bits)


def video_settings(quality, bitrate_cap=None):
    video = ({"bitrate": "8M", "maxrate": "12M", "cq": 20} if quality == "balanced" else
             {"bitrate": BITRATE, "maxrate": MAXRATE, "cq": CQ})
    if bitrate_cap is not None:
        peak = min(bitrate_bits(video["maxrate"]), max(1500000, int(bitrate_cap)))
        video["maxrate"] = str(peak)
        video["bitrate"] = str(min(bitrate_bits(video["bitrate"]), round(peak * .8)))
    ceiling = video["maxrate"] if EFFECTIVE_RATE_MODE == "vbr" else video["bitrate"]
    video["buffer"] = VBV_BUFFER or str(math.ceil(4 * bitrate_bits(ceiling) / FPS))
    return video


def build_ffmpeg(selected, quality="sharp", bitrate_cap=None):
    exe = FFMPEG_EXE or ffmpeg_path()
    video = video_settings(quality, bitrate_cap)
    width, height = selected.get("width", WIDTH), selected.get("height", HEIGHT)
    command = [exe, "-hide_banner", "-loglevel", "warning",
            "-fflags", "nobuffer", "-flags", "low_delay"]
    if selected["adapter"]:
        command += ["-init_hw_device", f'd3d11va=cap:{selected["adapter"]}',
                    "-filter_hw_device", "cap"]
    command += [
            "-f", "lavfi", "-i",
            f'ddagrab=output_idx={selected["output"]}:framerate={FPS}:draw_mouse=0']
    if CAPTURE_MODE == "scale":
        # Reliable fallback for FFmpeg builds where D3D11 scaling fails.
        command += ["-filter_threads", "1", "-vf",
                    f"hwdownload,format=bgra,scale={width}:{height}:flags=fast_bilinear,format=nv12"]
    # ddagrab timestamps are irregular microsecond capture times. Rounding
    # them to the default 1/FPS encoder clock can collapse adjacent frames
    # into one DTS tick under load. Preserve them on the RTP/MPEG-TS clock.
    command += ["-fps_mode", "vfr", "-enc_time_base:v", "1:90000",
            "-an", "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ull",
            "-profile:v", "baseline",
            "-rc", EFFECTIVE_RATE_MODE, "-b:v", video["bitrate"],
            "-maxrate", video["maxrate"] if EFFECTIVE_RATE_MODE == "vbr" else video["bitrate"],
            "-bufsize", video["buffer"], "-g", str(GOP), "-bf", "0",
            "-rc-lookahead", "0", "-delay", "0", "-zerolatency", "1",
            "-forced-idr", "1", "-multipass", "qres"]
    if EFFECTIVE_RATE_MODE == "vbr":
        # NVENC CQ in FFmpeg clears averageBitRate AND vbvBufferSize, even
        # with explicit -b:v / -bufsize. Use bitrate-controlled VBR instead.
        # Retain DK_CQ as the starting I/P quantizer, without disabling VBV.
        command += ["-init_qpI", str(video["cq"]), "-init_qpP", str(video["cq"])]
    command += [
            "-flush_packets", "1",
            "-f", "mpegts", "-mpegts_flags", "initial_discontinuity",
            "-omit_video_pes_length", "0" if LOW_DELAY_DEMUX else "1",
            "-muxdelay", "0", "-muxpreload", "0", "pipe:1"]
    return command


class VideoRateController:
    """Use un-clamped receiver feedback to budget the external encoder."""

    def __init__(self, quality):
        settings = video_settings(quality)
        self.maximum = bitrate_bits(settings["maxrate"] if EFFECTIVE_RATE_MODE == "vbr" else settings["bitrate"])
        self.cap = min(self.maximum, 8000000 if quality == "sharp" else 6000000)
        self.initial_cap = self.cap
        self.remb = None
        self.remb_time = 0
        self.baseline_rtt = None
        self.congested_until = 0
        self.congested_since = None
        self.clean_since = None
        self.last_change = time.monotonic()
        self.reason = "Stable quality / fixed bitrate budget"

    def observe(self, stats, now=None):
        now = time.monotonic() if now is None else now
        loss = float(stats.get("lossFraction", 0))
        loss = max(0.0, min(1.0, loss)) if math.isfinite(loss) else 0.0
        rtt = stats.get("rttMs")
        if isinstance(rtt, (int, float)) and math.isfinite(rtt) and rtt >= 0:
            self.baseline_rtt = rtt if self.baseline_rtt is None else min(self.baseline_rtt, rtt)
            queued = rtt > self.baseline_rtt + 70
        else:
            queued = False
        if loss >= .01 or queued:
            if self.congested_since is None:
                self.congested_since = now
            self.congested_until = now + 4
            self.clean_since = None
            self.reason = "Reducing bitrate for packet loss" if loss >= .01 else "Reducing bitrate for network queueing"
        else:
            self.congested_since = None
            if self.clean_since is None:
                self.clean_since = now

    def observe_remb(self, bitrate, now=None):
        if bitrate > 0:
            self.remb = bitrate
            self.remb_time = time.monotonic() if now is None else now

    def suggested(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_change < 12:
            return self.cap
        desired = self.cap
        fresh_remb = self.remb is not None and now - self.remb_time < 5
        sustained = (self.congested_since is not None and now - self.congested_since >= 3
                     and now < self.congested_until)
        # REMB alone may follow our current sending rate, especially on a
        # quiet desktop. Chasing it creates a self-induced bitrate staircase.
        if sustained:
            desired = min(desired, self.cap * .8)
            if fresh_remb:
                desired = min(desired, self.remb * .85)
        elif (fresh_remb and self.clean_since is not None and now - self.clean_since > 45
              and now - self.last_change > 45 and self.remb > self.cap * 1.5):
            desired = min(self.initial_cap, self.cap * 1.1, self.remb * .85)
            self.reason = "Recovering picture quality on a clear connection"
        return round(max(min(1500000, self.maximum), min(self.maximum, desired)))


class FFmpegPipe:
    def __init__(self, pipe): self.pipe = pipe
    def read(self, size=-1): return self.pipe.read(size)
    def seek(self, offset, whence=0): return -1
    def tell(self): return 0
    def readable(self): return True
    def seekable(self): return False


class CaptureProcess:
    def __init__(self, proc):
        self.proc = proc
        self.player = None
        self.log_task = asyncio.create_task(self.log())
        self.close_task = None

    async def log(self):
        while self.proc.poll() is None:
            line = await asyncio.to_thread(self.proc.stderr.readline)
            if line:
                print("[ffmpeg]", line.decode(errors="replace").rstrip())
            else:
                break

    @classmethod
    async def open(cls, selected, quality, cap):
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.ABOVE_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
        capture = cls(subprocess.Popen(build_ffmpeg(selected, quality, cap),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=flags))
        opening = asyncio.create_task(asyncio.to_thread(MediaPlayer,
            FFmpegPipe(capture.proc.stdout), format="mpegts", decode=False, options={
                "fflags": "noparse+nofillin" if LOW_DELAY_DEMUX else "nobuffer",
                "flags": "low_delay", "max_delay": "0", "probesize": "32768",
                "analyzeduration": "0", "max_packet_size": "4194304"}))
        try:
            capture.player = await asyncio.wait_for(asyncio.shield(opening), timeout=8)
            if capture.player.video is None:
                raise RuntimeError("Capture did not expose an H.264 video track")
            capture.player._throttle_playback = False
            return capture
        except BaseException:
            if capture.proc.poll() is None:
                capture.proc.kill()
            # Finish probing after ending its pipe, including cancellation.
            try:
                capture.player = await opening
            except Exception:
                pass
            await capture.close()
            raise

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        await asyncio.to_thread(self.proc.wait, timeout=3)
        if self.player and self.player.video:
            await asyncio.to_thread(self.player.video.stop)
        self.log_task.cancel()
        await asyncio.gather(self.log_task, return_exceptions=True)
        self.proc.stdout.close()
        self.proc.stderr.close()


class CapturePipeline:
    def __init__(self, selected, quality, capture, rate):
        self.selected, self.quality = selected, quality
        self.capture, self.rate = capture, rate
        self.track = LatestH264Track(capture.player.video)
        self.task = None
        self.closed = False
        self.restarts = 0
        self.retired = []

    @classmethod
    async def open(cls, selected, quality, stream_mode="stable"):
        rate = VideoRateController(quality)
        if stream_mode == "stable":
            # Stable mode uses the full configured encoder budget. The 8 Mbps
            # initial cap is reserved for adaptive streams that may tune it.
            rate.cap = rate.maximum
            rate.initial_cap = rate.maximum
        capture = await CaptureProcess.open(selected, quality, rate.cap)
        return cls(selected, quality, capture, rate)

    async def adapt(self):
        while not self.closed:
            await asyncio.sleep(1)
            cap = self.rate.suggested()
            if abs(cap - self.rate.cap) < self.rate.cap * .1:
                continue
            replacement = None
            try:
                replacement = await CaptureProcess.open(self.selected, self.quality, cap)
                first = await asyncio.wait_for(replacement.player.video.recv(), timeout=3)
                if not mark_h264_keyframe(first).is_keyframe:
                    raise RuntimeError("New capture did not start with an IDR")
                old = self.capture
                self.retired.append(old)
                self.capture = replacement
                self.track.replace_source(replacement.player.video, first)
                replacement = None
                self.rate.cap = cap
                self.rate.last_change = time.monotonic()
                self.restarts += 1
                print(f"[video] Peak budget {cap / 1000000:.1f} Mbps: {self.rate.reason}")
                await old.close()
                self.retired.remove(old)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.rate.last_change = time.monotonic()
                print("[video] Could not change bitrate; retaining current capture:", exc)
            finally:
                if replacement:
                    await replacement.close()

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.track.stop()
        await self.capture.close()
        for retired in self.retired:
            await retired.close()
        self.retired.clear()


def install_bandwidth_feedback(sender, controller):
    # aiortc's H.264 target_bitrate affects encode(Frame), not pack(Packet).
    # Read REMB before its software-encoder clamp, then retain normal RTCP.
    from aiortc.rtp import RtcpPsfbPacket, RTCP_PSFB_APP, unpack_remb_fci
    original = sender._handle_rtcp_packet

    async def handle(packet):
        if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
            try:
                bitrate, ssrcs = unpack_remb_fci(packet.fci)
                if sender._ssrc in ssrcs:
                    controller.observe_remb(bitrate)
            except ValueError:
                pass
        await original(packet)

    sender._handle_rtcp_packet = handle


def retain_retransmissions():
    # The 128-packet default is shorter than one RTT on a busy WAN stream.
    # Keep NACK recovery available without adding a playout queue.
    import aiortc.rtcrtpsender as sender_module
    if isinstance(getattr(sender_module, "RTP_HISTORY_SIZE", None), int):
        sender_module.RTP_HISTORY_SIZE = max(sender_module.RTP_HISTORY_SIZE, 2048)


def force_h264(pc, sender):
    codecs = RTCRtpSender.getCapabilities("video").codecs
    h264 = [codec for codec in codecs if codec.mimeType.lower() == "video/h264"]
    if not h264:
        raise RuntimeError("aiortc reports no H.264 RTP capability.")
    transceiver = next(t for t in pc.getTransceivers() if t.sender == sender)
    transceiver.setCodecPreferences(h264)


async def release_host_pointer_and_escape(channel, input_state=None, key_name="Escape"):
    # The browser may consume Escape while unlocking pointer lock. Hold the
    # Windows Escape here so a game can see it even if the browser loses focus.
    try:
        if input_state:
            input_state.release_all()
        for button in (Button.left, Button.middle, Button.right):
            inject_button(button, False)
        if os.name == "nt":
            for vk in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C):
                inject_virtual_key(vk, False)
        else:
            for modifier in (Key.alt, Key.ctrl, Key.shift, Key.cmd):
                keyboard.release(modifier)
        if os.name == "nt":
            ctypes.windll.user32.ClipCursor(None)
        # The Chromebook already mapped this key. The host never swaps it.
        escape_vk = 0xC0 if key_name in ("`", "~") else 0x1B
        inject_virtual_key(escape_vk, True)
        try:
            await asyncio.sleep(0.16)
        finally:
            inject_virtual_key(escape_vk, False)
            if os.name == "nt":
                ctypes.windll.user32.ClipCursor(None)
        if channel.readyState == "open":
            channel.send(json.dumps({"type": "escape_ack"}))
    except Exception as exc:
        print("[input] Could not release game input:", repr(exc))
        if channel.readyState == "open":
            channel.send(json.dumps({"type": "escape_error", "error": str(exc)}))


def handle_control_message(channel, raw, track=None, selected=None, audio=None, quality="sharp",
                           input_state=None, playout=None, pipeline=None):
    try:
        _handle_control_message(channel, raw, track, selected, audio, quality, input_state, playout, pipeline)
    except Exception as exc:
        now = time.monotonic()
        if input_state is None or now - input_state.last_error >= 1:
            if input_state:
                input_state.last_error = now
            print("[input]", exc)
            if channel.readyState == "open":
                channel.send(json.dumps({"type": "input_error", "error": str(exc)}))


def _handle_control_message(channel, raw, track=None, selected=None, audio=None, quality="sharp",
                            input_state=None, playout=None, pipeline=None):
    global escape_task
    try:
        msg = json.loads(raw)
    except Exception:
        return
    kind = msg.get("type")
    if kind == "network_stats" and pipeline:
        if pipeline.task is not None:
            pipeline.rate.observe(msg)
        return
    if kind == "release_all":
        if input_state:
            input_state.release_all()
        return
    if kind == "escape":
        if escape_task is None or escape_task.done():
            escape_task = asyncio.create_task(release_host_pointer_and_escape(
                channel, input_state, msg.get("key", "Escape")))
        return
    if kind == "ping":
        video = video_settings(quality, pipeline.rate.cap if pipeline else None)
        if channel.readyState == "open":
            channel.send(json.dumps({"type": "pong", "id": msg.get("id")}))
            channel.send(json.dumps({"type": "host_info", "captureMode": CAPTURE_MODE,
                                     "hostBuild": BUILD_VERSION,
                                     "fps": FPS, "targetWidth": selected.get("width", WIDTH) if selected else WIDTH,
                                     "targetHeight": selected.get("height", HEIGHT) if selected else HEIGHT,
                                     "resolutionNote": selected.get("resolutionNote", "") if selected else "",
                                     "videoPipe": "complete frames" if LOW_DELAY_DEMUX else "legacy parser",
                                     "bitrate": video["bitrate"], "quality": quality,
                                     "audio": audio.status() if audio else ("unavailable" if AUDIO_ENABLED else "off on host"),
                                     "audioDevice": audio.device if audio else None,
                                     "audioLevel": audio.level if audio else None,
                                     "audioFrames": audio.captured if audio else 0,
                                     "queueFrames": track.queued() if track else None,
                                     "peakQueueFrames": track.peak_queue if track else None,
                                     "queueRecoveryFrames": HOST_QUEUE_FRAMES,
                                     "skippedPackets": track.skipped if track else None,
                                     "hostPacketRate": track.sent_rate() if track else None,
                                     "gop": GOP, "vbvBuffer": video["buffer"],
                                     "rateMode": EFFECTIVE_RATE_MODE, "maxrate": video["maxrate"],
                                     "streamMode": "adaptive" if pipeline and pipeline.task is not None else "stable",
                                     "rateNote": pipeline.rate.reason if pipeline else "Fixed bitrate",
                                     "rateChanges": pipeline.restarts if pipeline else 0,
                                     "inputAdmin": is_admin(), "mouseState": host_mouse_state(selected),
                                     "playoutMaxMs": PLAYOUT_MAX_MS if playout and playout.extension_id else None,
                                     "videoProfile": "baseline",
                                     "displayName": selected["name"] if selected else None,
                                     "displayOutputIndex": selected["output"] if selected else None}))
        return
    if kind == "mouse":
        action = msg.get("action")
        if action == "move":
            x = max(0.0, min(1.0, float(msg.get("x", 0))))
            y = max(0.0, min(1.0, float(msg.get("y", 0))))
            bounds = selected["bounds"] if selected else None
            if bounds:
                left, top, right, bottom = bounds
                position = (left + round(x * max(0, right-left-1)),
                            top + round(y * max(0, bottom-top-1)))
            else:
                sw, sh = screen_size()
                position = (round(x * max(0, sw - 1)), round(y * max(0, sh - 1)))
            if os.name == "nt":
                if not ctypes.windll.user32.SetCursorPos(*position):
                    raise RuntimeError("Windows blocked cursor movement; run the host as administrator.")
            else:
                mouse.position = position
        elif action == "relative":
            move_mouse_relative(msg.get("dx", 0), msg.get("dy", 0))
        elif action in ("down", "up"):
            button = {0: Button.left, 1: Button.middle, 2: Button.right}.get(msg.get("button", 0))
            if button is not None:
                inject_button(button, action == "down")
                if input_state:
                    if action == "down":
                        input_state.buttons.add(button)
                    else:
                        input_state.buttons.discard(button)
        elif action == "scroll" and input_state:
            if selected and "x" in msg and "y" in msg:
                left, top, right, bottom = selected["bounds"]
                x, y = max(0.0, min(1.0, float(msg["x"]))), max(0.0, min(1.0, float(msg["y"])))
                position = (left + round(x * max(0, right-left-1)), top + round(y * max(0, bottom-top-1)))
                if os.name == "nt":
                    if not ctypes.windll.user32.SetCursorPos(*position):
                        raise RuntimeError("Windows blocked scrolling; restart the host as administrator.")
                else:
                    mouse.position = position
            input_state.scroll_pixels(msg.get("dx", 0), msg.get("dy", 0), msg.get("linePixels", 16))
        elif action == "wheel" and input_state:
            # Older pages send signed notches. Keep them usable during upgrade.
            input_state.scroll_pixels(float(msg.get("dx", 0)) * 48,
                                      float(msg.get("dy", 0)) * 48)
        return
    if kind == "key":
        if msg.get("action") not in ("down", "up"):
            return
        if input_state:
            input_state.key(msg.get("key"), msg.get("code"), msg["action"] == "down")
        else:
            key = resolve_key(msg.get("key"))
            if key is not None:
                (keyboard.press if msg["action"] == "down" else keyboard.release)(key)


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Remote-Secret"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Cache-Control"] = "no-store"
    return response


async def health(_request):
    width, height = WIDTH, HEIGHT
    try:
        selected = await asyncio.to_thread(select_capture_display)
        width, height = selected["width"], selected["height"]
        display = {"name": selected["name"], "adapter": selected["adapter"],
                   "output": selected["output"], "bounds": selected["bounds"]}
    except Exception as exc:
        display = {"error": str(exc)}
    return web.json_response({"ok": True, "name": "DK Remote WebRTC Low Latency",
                              "hostBuild": BUILD_VERSION,
                              "fps": FPS, "width": width, "height": height,
                              "bitrate": BITRATE, "display": display,
                              "capture": "DXGI/ddagrab + " + ("direct NVENC" if CAPTURE_MODE == "direct" else "CPU scale + NVENC"),
                              "captureMode": CAPTURE_MODE})


async def client_page(_request):
    path = select_client_page(BASE)
    if path is None:
        return web.Response(text="Put remote_client_low_latency.html next to the Python host.", status=404)
    print(f"[client] Serving {path.name} (host build {BUILD_VERSION})")
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


def select_client_page(base):
    # Downloaded copies can have (2)/(3) suffixes. Prefer the matching build
    # over a stale canonical filename, then the most recent dated build.
    candidates = []
    for path in base.glob("remote_client_low_latency*.html"):
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as stream:
                header = stream.read(4096)
            match = re.search(r'<meta\s+name="dk-remote-build"\s+content="([\d.]+)"', header)
            build = match.group(1) if match else "0"
            version = tuple(int(part) for part in build.split("."))
            candidates.append(((build == BUILD_VERSION, version, path.stat().st_mtime), path))
        except (OSError, UnicodeError, ValueError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


async def offer(request):
    supplied_secret = request.headers.get("X-Remote-Secret", "")
    if not secrets.compare_digest(supplied_secret, SECRET):
        return web.json_response({"error": "Bad secret"}, status=403)
    try:
        params = await request.json()
        remote_offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        requested_resolution = controller_resolution(params.get("screen"))
        quality = QUALITY_MODE
        network_mode = params.get("networkMode", "auto")
        if network_mode not in ("auto", "lan"):
            raise ValueError("Network mode must be auto or lan")
        network_mode = "auto"  # Ignore a persisted LAN preference from older clients.
        stream_mode = params.get("streamMode", "stable")
        if stream_mode not in ("stable", "adaptive"):
            raise ValueError("Streaming mode must be stable or adaptive")
        client_id = params.get("clientId")
        if client_id is not None and (not isinstance(client_id, str) or
                                      not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", client_id)):
            raise ValueError("Invalid controller session ID")
    except Exception as exc:
        return web.json_response({"error": "Invalid offer: " + str(exc)}, status=400)

    async with request.app["offer_lock"]:
        return await create_session(request, params, remote_offer, requested_resolution,
                                    quality, network_mode, client_id, stream_mode)


async def create_session(request, params, remote_offer, requested_resolution,
                         quality, network_mode, client_id, stream_mode="stable"):
    try:
        # A resumed tab gets a fresh peer, while its old ICE transport may
        # still claim to be connected. Retire that tab's previous capture now.
        for session in list(sessions.values()):
            if client_id and session.get("client_id") == client_id and session.get("cleanup"):
                await session["cleanup"]()
        async with request.app["display_config_lock"]:
            selected = await capture_for_controller(requested_resolution)
        if params.get("screen") is None:
            selected["resolutionNote"] = "This HTML client did not report its screen size. Close the old client/about:blank tab and open the updated HTML."
            print("[client] Older HTML connected without screen resolution; using the compatibility resolution.")
    except Exception as exc:
        print("[capture]", exc)
        return web.json_response({"error": str(exc)}, status=503)
    print(f'[capture] {selected["name"]} ({selected["identity"]}) '
          f'=> DXGI adapter {selected["adapter"]} output {selected["output"]}')

    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[
        RTCIceServer(urls="stun:stun.cloudflare.com:3478")]))
    pcs.add(pc)
    session_id = secrets.token_hex(8)
    try:
        pipeline = await CapturePipeline.open(selected, quality, stream_mode)
        live_track = pipeline.track
        sender = pc.addTrack(live_track)
        force_h264(pc, sender)
        playout = install_low_delay_sender(sender)
        install_bandwidth_feedback(sender, pipeline.rate)
        retain_retransmissions()
        if stream_mode == "adaptive":
            pipeline.rate.reason = "Adaptive bandwidth enabled"
            pipeline.task = asyncio.create_task(pipeline.adapt())
    except Exception as exc:
        if "pipeline" in locals():
            await pipeline.close()
        await pc.close()
        pcs.discard(pc)
        print("[capture] Could not open FFmpeg video stream:", repr(exc))
        return web.json_response({
            "error": "Capture startup failed. Check the [ffmpeg] lines in the host console; "
                     "the DXGI/scaler/encoder stage stopped before video reached PyAV."
        }, status=502)

    audio_track = None
    if AUDIO_ENABLED:
        try:
            import soundcard  # noqa: F401; validates the optional Windows audio dependency.
            audio_track = SpeakerLoopbackTrack(asyncio.get_running_loop())
            audio_sender = pc.addTrack(audio_track)
            # Independent streams prevent speaker buffering from forcing the
            # screen into an audio/video synchronization delay.
            if hasattr(audio_sender, "_stream_id"):
                audio_sender._stream_id = str(uuid.uuid4())
        except Exception as exc:
            print("[audio] Unavailable:", exc, "— install with: python3 -m pip install soundcard numpy")

    input_state = HostInputState(page_height=selected["height"])
    sessions[session_id] = {"pc": pc, "pipeline": pipeline,
                            "track": live_track, "audio_track": audio_track,
                            "selected": selected, "input_state": input_state,
                            "last_activity": time.monotonic(), "client_id": client_id}
    control_channels = []

    @pc.on("datachannel")
    def on_datachannel(channel):
        print("Control data channel:", channel.label)
        if channel.label == "control":
            control_channels.append(channel)
        @channel.on("open")
        def on_open():
            if channel.label == "control":
                video = video_settings(quality, pipeline.rate.cap)
                channel.send(json.dumps({"type": "host_info", "captureMode": CAPTURE_MODE,
                                         "hostBuild": BUILD_VERSION,
                                         "fps": FPS, "targetWidth": selected["width"], "targetHeight": selected["height"],
                                         "resolutionNote": selected.get("resolutionNote", ""),
                                         "videoPipe": "complete frames" if LOW_DELAY_DEMUX else "legacy parser",
                                         "bitrate": video["bitrate"], "quality": quality,
                                         "audio": "starting" if audio_track else ("unavailable" if AUDIO_ENABLED else "off on host"),
                                         "displayOutputIndex": selected["output"],
                                         "displayName": selected["name"],
                                         "queueFrames": live_track.queued(), "skippedPackets": live_track.skipped,
                                         "peakQueueFrames": live_track.peak_queue,
                                         "queueRecoveryFrames": HOST_QUEUE_FRAMES,
                                         "hostPacketRate": live_track.sent_rate(),
                                         "gop": GOP, "vbvBuffer": video["buffer"],
                                         "inputAdmin": is_admin(), "mouseState": host_mouse_state(selected),
                                         "playoutMaxMs": PLAYOUT_MAX_MS if playout and playout.extension_id else None,
                                         "videoProfile": "baseline",
                                         "rateMode": EFFECTIVE_RATE_MODE, "maxrate": video["maxrate"],
                                         "streamMode": "adaptive" if pipeline.task is not None else "stable",
                                         "rateNote": pipeline.rate.reason, "rateChanges": pipeline.restarts}))
        @channel.on("message")
        def on_message(message):
            if isinstance(message, str):
                if session_id in sessions:
                    sessions[session_id]["last_activity"] = time.monotonic()
                handle_control_message(channel, message, live_track, selected, audio_track,
                                       quality, input_state, playout, pipeline)
        @channel.on("close")
        def on_close():
            if channel.label == "control":
                input_state.release_all()

    async def cleanup():
        session = sessions.pop(session_id, None)
        if session is None:
            return
        input_state.release_all()
        mouse_watcher = session.get("mouse_watcher")
        if mouse_watcher:
            mouse_watcher.cancel()
        watcher = session.get("watcher")
        if watcher and watcher is not asyncio.current_task():
            watcher.cancel()
        if session["audio_track"]:
            session["audio_track"].stop()
        try:
            await session["pipeline"].close()
        except Exception as exc:
            print("[capture] Cleanup error:", exc)
        try: await pc.close()
        except Exception: pass
        pcs.discard(pc)

    sessions[session_id]["cleanup"] = cleanup

    async def watch_display():
        while session_id in sessions:
            await asyncio.sleep(1.5)
            if session_id not in sessions:
                return
            # Chrome may throttle or freeze a background tab's control pings.
            # Keep the encoder alive across tab switches and brief lid sleeps,
            # then release stale sessions if the controller never returns.
            if time.monotonic() - sessions[session_id]["last_activity"] > 120:
                print(f"[{session_id}] Controller stopped responding; releasing capture.")
                await cleanup()
                return
            try:
                # Driver enumeration may block during hotplug or mode changes.
                # It must not stall the loop that sends video and handles input.
                current = await asyncio.to_thread(select_capture_display)
                if session_id not in sessions:
                    return
                moved = any(current[field] != selected[field]
                            for field in ("adapter", "output", "name", "bounds"))
            except Exception as exc:
                print(f"[{session_id}] Virtual display unavailable: {exc}")
                moved = True
            if moved or pipeline.capture.proc.poll() is not None:
                print(f"[{session_id}] Capture layout changed; asking client to reconnect.")
                for channel in control_channels:
                    if channel.readyState == "open":
                        channel.send(json.dumps({"type": "capture_restart"}))
                await asyncio.sleep(0.15)
                await cleanup()
                return

    sessions[session_id]["watcher"] = asyncio.create_task(watch_display())

    async def watch_mouse():
        last = None
        while session_id in sessions:
            state = host_mouse_state(selected)
            if state != last:
                for channel in control_channels:
                    if channel.readyState == "open":
                        channel.send(json.dumps({"type": "mouse_state", **state}))
                last = state
            await asyncio.sleep(0.15)

    sessions[session_id]["mouse_watcher"] = asyncio.create_task(watch_mouse())

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        print(f"[{session_id}] WebRTC connection: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await cleanup()

    await pc.setRemoteDescription(remote_offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return web.json_response({"sdp": pc.localDescription.sdp,
                              "type": pc.localDescription.type,
                              "hostBuild": BUILD_VERSION,
                              "sessionId": session_id})


async def on_shutdown(_app):
    global escape_task
    if escape_task and not escape_task.done():
        escape_task.cancel()
        await asyncio.gather(escape_task, return_exceptions=True)
    escape_task = None
    for session in list(sessions.values()):
        session["input_state"].release_all()
        try: session.get("mouse_watcher") and session["mouse_watcher"].cancel()
        except Exception: pass
        try: session.get("watcher") and session["watcher"].cancel()
        except Exception: pass
        if session["audio_track"]:
            session["audio_track"].stop()
        try: await session["pipeline"].close()
        except Exception: pass
    await asyncio.gather(*(pc.close() for pc in list(pcs)), return_exceptions=True)
    pcs.clear()
    sessions.clear()


def make_app():
    app = web.Application(middlewares=[cors_middleware], client_max_size=4 * 1024 * 1024)
    app["display_config_lock"] = asyncio.Lock()
    app["offer_lock"] = asyncio.Lock()
    app.router.add_get("/", client_page)
    app.router.add_get("/health", health)
    app.router.add_post("/offer", offer)
    app.router.add_options("/offer", offer)
    app.on_shutdown.append(on_shutdown)
    return app


def desktop_gui():
    import tkinter as tk
    from tkinter import ttk

    global QUALITY_MODE, AUDIO_ENABLED
    root = tk.Tk()
    root.title("DK Remote — Windows host")
    root.geometry("560x640")
    root.minsize(500, 620)
    root.configure(padx=20, pady=16)
    events = queue.Queue()
    worker = None
    server_loop = None
    server_stop = None
    tunnel = None
    running = False
    closing = False
    quality = tk.StringVar(value=QUALITY_MODE)
    audio = tk.BooleanVar(value=AUDIO_ENABLED)
    status = tk.StringVar(value="Starting host…")
    display = tk.StringVar(value="Checking virtual display…")
    url = tk.StringVar(value="Waiting for tunnel…")

    ttk.Label(root, text="DK Remote", font=("Segoe UI", 22, "bold")).pack(anchor="w")
    ttk.Label(root, text="Virtual display resolution automatically follows the controller's screen.").pack(anchor="w", pady=(0, 14))
    ttk.Label(root, textvariable=status, wraplength=490).pack(anchor="w", pady=(0, 10))
    ttk.Label(root, textvariable=display, wraplength=490).pack(anchor="w", pady=(0, 12))
    elevated = is_admin()
    permission_text = ("Input: administrator — Task Manager and elevated games supported"
                       if elevated else "Input: standard user — Task Manager may block control")
    ttk.Label(root, text=permission_text, wraplength=490).pack(anchor="w", pady=(0, 8))

    def restart_admin():
        try:
            if request_admin_restart():
                close()
            else:
                status.set("Administrator restart was cancelled. The current host is still running.")
        except Exception as exc:
            status.set("Could not restart as administrator: " + str(exc))

    if not elevated:
        ttk.Button(root, text="Restart as administrator", command=restart_admin).pack(fill="x", pady=(0, 12))
    options = ttk.Frame(root)
    options.pack(fill="x")
    ttk.Label(options, text="Picture quality").grid(row=0, column=0, sticky="w", padx=(0, 14))
    preset = ttk.Combobox(options, textvariable=quality, values=("sharp", "balanced"), state="readonly", width=15)
    preset.grid(row=0, column=1, sticky="w")
    audio_box = ttk.Checkbutton(options, text="Send PC speaker audio", variable=audio)
    audio_box.grid(row=1, column=0, columnspan=2, sticky="w", pady=10)
    try:
        import soundcard  # noqa: F401
        audio_message = "Audio capture ready (Windows default speaker)"
    except Exception:
        audio_message = "For audio: python3 -m pip install soundcard numpy"
    ttk.Label(root, text=audio_message, wraplength=490).pack(anchor="w")

    ttk.Label(root, text="Chromebook address", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(9, 2))
    address = ttk.Entry(root, textvariable=url, state="readonly")
    address.pack(fill="x")
    ttk.Label(root, text="Connection secret", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 2))
    secret_entry = ttk.Entry(root, show="•")
    secret_entry.insert(0, SECRET)
    secret_entry.configure(state="readonly")
    secret_entry.pack(fill="x")

    def copy(value):
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update_idletasks()

    def copy_address():
        if url.get().startswith("https://"):
            copy(url.get())

    buttons = ttk.Frame(root)
    buttons.pack(fill="x", pady=12)
    ttk.Button(buttons, text="Copy address", command=copy_address).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="Copy secret", command=lambda: copy(SECRET)).pack(side="left")
    ttk.Label(root, text="On the Chromebook: open the address, paste the secret, press Connect.\nHold Alt for 1 second to show or hide the menu and mouse-lock button.", wraplength=490).pack(anchor="w", pady=(2, 14))

    def check_display():
        try:
            chosen = select_capture_display()
            display.set(f'Virtual display: {chosen["name"]}  •  {chosen["width"]}×{chosen["height"]}  •  automatic')
        except Exception as exc:
            display.set("Virtual display: " + str(exc))

    def server_worker():
        nonlocal server_loop, server_stop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server_loop = loop
        server_stop = asyncio.Event()

        async def serve():
            runner = web.AppRunner(make_app(), access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, HOST, PORT).start()
                events.put(("ready", None))
                await server_stop.wait()
            finally:
                await runner.cleanup()

        try:
            loop.run_until_complete(serve())
        except Exception as exc:
            events.put(("error", str(exc)))
        finally:
            loop.close()
            server_loop = None
            events.put(("stopped", None))

    def tunnel_reader(process):
        found = False
        try:
            for line in iter(process.stdout.readline, b""):
                message = line.decode(errors="replace").rstrip()
                print("[cloudflared]", message)
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", message)
                if match:
                    found = True
                    events.put(("url", match.group(0)))
            code = process.wait()
            if not found:
                events.put(("tunnel_error", f"Tunnel exited ({code}); check the [cloudflared] console lines."))
        except Exception as exc:
            events.put(("tunnel_error", str(exc)))

    def start_tunnel():
        nonlocal tunnel
        exe = shutil.which("cloudflared")
        if not exe:
            status.set("Host ready. Run cloudflared tunnel --url http://127.0.0.1:8765 in PowerShell.")
            return
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            tunnel = subprocess.Popen([exe, "tunnel", "--url", f"http://{HOST}:{PORT}"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      creationflags=flags)
            threading.Thread(target=tunnel_reader, args=(tunnel,), daemon=True).start()
            status.set("Host ready; waiting for Cloudflare tunnel address…")
        except Exception as exc:
            status.set("Host ready, but cloudflared could not start: " + str(exc))

    def start():
        nonlocal worker, running, closing
        if running:
            return
        closing = False
        globals()["QUALITY_MODE"] = quality.get()
        globals()["AUDIO_ENABLED"] = audio.get()
        try:
            SETTINGS_FILE.write_text(json.dumps({"quality": quality.get(), "audio": audio.get()}), encoding="utf-8")
        except OSError as exc:
            print("[settings] Could not save UI choices:", exc)
        check_display()
        running = True
        preset.configure(state="disabled")
        audio_box.configure(state="disabled")
        start_button.configure(text="Stop host")
        status.set("Starting signaling server…")
        url.set("Waiting for tunnel…")
        worker = threading.Thread(target=server_worker, name="DK WebRTC server", daemon=True)
        worker.start()

    def stop():
        nonlocal running, tunnel
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
        tunnel = None
        if server_loop and server_stop and server_loop.is_running():
            server_loop.call_soon_threadsafe(server_stop.set)
        running = False
        start_button.configure(state="disabled", text="Stopping…")
        status.set("Stopping…")
        url.set("Host stopped")

    def toggle():
        if running:
            stop()
        else:
            start()

    start_button = ttk.Button(root, text="Start host", command=toggle)
    start_button.pack(fill="x", pady=(0, 10))
    ttk.Button(root, text="Refresh display status", command=check_display).pack(fill="x")

    def poll():
        nonlocal running
        while True:
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == "ready":
                if running:
                    start_tunnel()
                elif server_loop and server_stop:
                    server_loop.call_soon_threadsafe(server_stop.set)
            elif kind == "url" and running:
                url.set(value)
                status.set("Ready. Open this address on the Chromebook.")
            elif kind in ("error", "tunnel_error") and running:
                status.set(value)
            elif kind == "stopped":
                running = False
                start_button.configure(state="normal", text="Start host")
                preset.configure(state="readonly")
                audio_box.configure(state="normal")
                if not closing and status.get() == "Stopping…":
                    status.set("Host stopped.")
        if not closing:
            root.after(150, poll)

    def close():
        nonlocal closing
        closing = True
        stop()
        def finish():
            if worker and worker.is_alive():
                root.after(100, finish)
            else:
                root.destroy()
        root.after(100, finish)

    root.protocol("WM_DELETE_WINDOW", close)
    def refresh_display():
        if not closing:
            check_display()
            root.after(2500, refresh_display)
    refresh_display()
    poll()
    root.after(100, start)
    root.mainloop()


def main():
    global FFMPEG_EXE
    if os.name == "nt" and not is_admin() and os.environ.get("DK_ALLOW_STANDARD_INPUT") != "1":
        # Ask before there is an active stream. A remote-only UAC prompt would
        # otherwise strand the user when Task Manager takes the foreground.
        launched = False
        if "--wait-for-host" not in sys.argv:
            try:
                launched = request_admin_restart()
            except Exception as exc:
                print("[input] Administrator launch failed:", exc)
        if not launched:
            message = ("DK Remote needs administrator access to control Task Manager and elevated games.\n\n"
                       "Start the host again and approve the Windows prompt on this PC. "
                       "Streaming has not started.")
            print("[input]", message)
            if os.environ.get("DK_NO_GUI") != "1":
                from tkinter import messagebox
                messagebox.showerror("Administrator access required", message)
        return
    if "--wait-for-host" in sys.argv:
        # The elevated replacement waits for the old GUI to release its port.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                try:
                    probe.bind((HOST, PORT))
                    break
                except OSError:
                    time.sleep(0.1)
    try:
        exe = ffmpeg_path()
        check_ffmpeg(exe)
    except Exception as exc:
        if os.name == "nt" and os.environ.get("DK_NO_GUI") != "1":
            from tkinter import messagebox
            messagebox.showerror("DK Remote cannot start", str(exc))
            return
        raise
    FFMPEG_EXE = exe
    print("\n============================================================")
    print(" DK REMOTE - WEBRTC + DXGI + NVENC (LOW LATENCY)")
    print(f" Build            : {BUILD_VERSION}")
    print("============================================================")
    print(f" Signaling server : http://{HOST}:{PORT}")
    print(f" Capture          : DXGI Desktop Duplication / {FPS} FPS")
    print(f" Display          : {'auto MttVDD virtual monitor' if CAPTURE_DISPLAY == 'virtual' else f'manual DXGI output_idx={DISPLAY_OUTPUT_INDEX}'}")
    print(f" Target           : controller screen resolution (automatic) @ {FPS} FPS")
    print(f" Frame path       : {'D3D11 direct to NVENC (no scaler)' if CAPTURE_MODE == 'direct' else 'CPU resize to NV12 + NVENC'}")
    if CAPTURE_MODE == "direct":
        print(" Virtual mode     : Windows resolution is chosen automatically when a controller connects.")
    video = video_settings(QUALITY_MODE)
    print(f" Encoder          : h264_nvenc p4 ULL / {EFFECTIVE_RATE_MODE} / {video['bitrate']} target / "
          f"{video['maxrate'] if EFFECTIVE_RATE_MODE == 'vbr' else video['bitrate']} ceiling / {video['buffer']} VBV / {GOP}-frame GOP")
    print(f" Playout hint     : 0–{PLAYOUT_MAX_MS} ms (browser best effort)")
    print(f" Video pipe       : {'complete frames / no parser lookahead' if LOW_DELAY_DEMUX else 'legacy H.264 parser'}")
    print(f" Input permission : {'administrator' if is_admin() else 'standard user; use Restart as administrator for Task Manager'}")
    print(" Chromebook secret:")
    print(SECRET)
    print("\n In another PowerShell:")
    print(f" cloudflared tunnel --url http://{HOST}:{PORT}")
    print(" Open the cloudflared URL on your Chromebook and enter the secret above.")
    try:
        import soundcard  # noqa: F401
        print(" Audio            : Windows default speaker (WASAPI loopback)")
    except Exception:
        print(" Audio            : install with python3 -m pip install soundcard numpy")
    print("============================================================\n")
    if os.name == "nt" and os.environ.get("DK_NO_GUI") != "1":
        desktop_gui()
    else:
        web.run_app(make_app(), host=HOST, port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
