"""Appium/Nova capture and a minimal input transport; guards live in engine.py."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.error import HTTPError

import numpy as np
from PIL import Image

from .windows import WindowInfo, WindowError, discover_window


class DriverError(RuntimeError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DriverError("Appium redirects are disabled for loopback-only transport.")


def _loopback_endpoint(server: str) -> str:
    if not isinstance(server, str) or not server or any(ord(char) <= 32 or ord(char) == 127 for char in server):
        raise DriverError("The driver endpoint must be an explicit local loopback HTTP URL.")
    try:
        parsed = urlsplit(server)
        port = parsed.port
        valid = (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.username is None and parsed.password is None
            and port is not None and 1 <= port <= 65535
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        )
    except ValueError as exc:
        raise DriverError("The driver endpoint is not a valid loopback HTTP URL.") from exc
    if not valid:
        raise DriverError("The driver endpoint must use local loopback HTTP with an explicit port and no credentials, path, query or fragment.")
    # Resolve the supported localhost alias ourselves rather than trusting DNS
    # or a configured proxy to preserve the local-only transport boundary.
    return f"http://127.0.0.1:{port}"


@dataclass
class Frame:
    rgb: np.ndarray
    window: WindowInfo
    captured_at: float
    captured_utc: str
    latency_ms: float


def crop_client(image: Image.Image, window: WindowInfo) -> np.ndarray:
    if image.size != (window.outer_width, window.outer_height):
        raise DriverError(f"Screenshot dimensions {image.size} do not match the measured window.")
    x = window.client_x - window.outer_x
    y = window.client_y - window.outer_y
    if x < 0 or y < 0 or x + window.client_width > image.width or y + window.client_height > image.height:
        raise DriverError("Client rectangle does not fit inside the screenshot.")
    return np.asarray(image.convert("RGB").crop((x, y, x + window.client_width, y + window.client_height))).copy()


class NovaSession:
    def __init__(self, server: str = "http://127.0.0.1:4723", *, focus_on_attach: bool = False):
        # A local control service only. Avoid sending a game frame to a remote endpoint.
        if not isinstance(focus_on_attach, bool):
            raise DriverError("focus_on_attach must be an explicit boolean.")
        self.server = _loopback_endpoint(server)
        self.focus_on_attach = focus_on_attach
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())
        self.session_id = None
        self.identity = None

    def request(self, method, route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.server + route, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=15) as response:
                value = json.load(response).get("value")
        except HTTPError as exc:
            raise DriverError(f"Appium {method} {route}: {exc.read().decode(errors='replace')[:800]}") from exc
        if isinstance(value, dict) and value.get("error"):
            raise DriverError(value.get("message", value["error"]))
        return value

    def __enter__(self):
        window = discover_window()
        if window.minimized:
            raise WindowError("Restore the game window before capture.")
        value = self.request("POST", "/session", {"capabilities": {"alwaysMatch": {
            "platformName": "Windows", "appium:automationName": "NovaWindows",
            "appium:appTopLevelWindow": hex(window.hwnd), "appium:shouldCloseApp": False,
            "appium:newCommandTimeout": 120,
        }}})
        self.session_id = value["sessionId"]
        self.identity = window.identity
        try:
            self._verify_attached_window()
            if self.focus_on_attach:
                self._focus_initial_window(window.hwnd)
        except BaseException:
            # __exit__ is not invoked when __enter__ fails. Release this driver
            # session without closing the game or masking the original error.
            try:
                self.__exit__(None, None, None)
            except Exception:
                pass
            raise
        return self

    def _verify_attached_window(self) -> WindowInfo:
        window = discover_window()
        if window.identity != self.identity or window.minimized:
            raise WindowError("Game window identity/visibility changed during attachment.")
        return window

    def _focus_initial_window(self, hwnd: int) -> None:
        """One explicitly authorized activation, never automatic capture recovery."""
        self.request("POST", self.route("/window"), {"handle": hex(hwnd)})
        time.sleep(0.1)
        current = self._verify_attached_window()
        if not current.foreground:
            element = self.request("POST", self.route("/execute/sync"), {
                "script": "windows:getWindowElement", "args": [],
            })
            current = self._verify_attached_window()
            if not current.foreground:
                element_key = "element-6066-11e4-a52e-4f735466cecf"
                if not isinstance(element, dict) or not isinstance(element.get(element_key), str) or not element[element_key]:
                    raise DriverError("Nova did not return a verified window element for focus.")
                self.request("POST", self.route("/execute/sync"), {
                    "script": "windows:setFocus", "args": [{element_key: element[element_key]}],
                })
                time.sleep(0.1)
                current = self._verify_attached_window()
        if not current.foreground:
            raise WindowError("Initial activation failed; bring the game to the foreground manually.")

    def __exit__(self, *_):
        if self.session_id:
            try:
                self.request("DELETE", self.route(""))
            finally:
                self.session_id = None

    def route(self, suffix):
        if not self.session_id:
            raise DriverError("No driver session is open.")
        return f"/session/{self.session_id}{suffix}"

    def capture_window(self) -> Frame:
        before = discover_window()
        if before.identity != self.identity or before.minimized:
            raise WindowError("Game window identity/visibility changed.")
        if not before.foreground:
            raise WindowError("Capture stopped: keep the game foreground and unobscured.")
        started = time.monotonic()
        utc = datetime.now(timezone.utc).isoformat()
        encoded = self.request("GET", self.route("/screenshot"))
        elapsed = (time.monotonic() - started) * 1000
        after = discover_window()
        if not after.foreground or after.minimized:
            raise WindowError("Capture stopped: game lost foreground or was minimized during capture.")
        geometry_fields = ("identity", "outer_x", "outer_y", "outer_width", "outer_height", "client_x", "client_y", "client_width", "client_height", "dpi")
        if any(getattr(before, key) != getattr(after, key) for key in geometry_fields):
            raise WindowError("Game window geometry changed during capture.")
        with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as image:
            rgb = crop_client(image, after)
        return Frame(rgb, after, started, utc, elapsed)

    def dispatch(self, action: dict, frame: Frame):
        """Call only after ActionGuard.authorize; recheck foreground/geometry at dispatch."""
        now = discover_window()
        if now.identity != frame.window.identity or not now.foreground or now.minimized:
            raise WindowError("Input cancelled: game lost foreground or identity changed.")
        for field in ("client_x", "client_y", "client_width", "client_height", "dpi"):
            if getattr(now, field) != getattr(frame.window, field):
                raise WindowError("Input cancelled: window geometry changed after observation.")
        if time.monotonic() - frame.captured_at > 2:
            raise WindowError("Input cancelled: observation became stale.")
        if action["type"] == "click_anchor":
            x, y = action["point"]
            if not (0 <= x < now.client_width and 0 <= y < now.client_height):
                raise DriverError("Input target is outside the client rectangle.")
            sequence = [{"type": "pointerMove", "origin": "viewport",
                         "x": x + now.client_x - now.outer_x, "y": y + now.client_y - now.outer_y, "duration": 0},
                        {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 80},
                        {"type": "pointerUp", "button": 0},
                        # Keep the release position briefly for apps that poll
                        # input state before moving the already released mouse.
                        {"type": "pause", "duration": 150},
                        # Park the released pointer away from menu anchors so
                        # cursor pixels/hover effects do not obscure the next
                        # observation. This occurs only during live dispatch;
                        # capture and dry-run never move the pointer.
                        {"type": "pointerMove", "origin": "viewport",
                         "x": 5 + now.client_x - now.outer_x,
                         "y": now.client_height - 5 + now.client_y - now.outer_y,
                         "duration": 0}]
            if action.get("pointer_mode") == "settled":
                # Cadence verified in the supervised Elite pilot; keep the
                # released pointer in place while the game consumes the click.
                sequence = [
                    {"type": "pointerMove", "origin": "viewport",
                     "x": x + now.client_x - now.outer_x, "y": y + now.client_y - now.outer_y, "duration": 200},
                    {"type": "pause", "duration": 250},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pause", "duration": 150},
                    {"type": "pointerUp", "button": 0},
                ]
            source = {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": sequence}
        elif action["type"] == "key":
            key = str(action["key"]).upper()
            # The operator-confirmed quickslot bindings need normal Windows
            # virtual-key events, rather than Unicode text entry. Nova's
            # documented windows:keys route supports these explicit codes.
            # PRESET_SELECTOR is the named first-slot binding: the caller must
            # verify the settings page and preset shortcut before authorizing
            # it. Arbitrary digit-key input is not part of this allowlist.
            physical_codes = {"BACKTICK": 0xC0, "BACKSLASH": 0xDC, "PRESET_SELECTOR": 0x31,
                              "F2": 0x71, "F3": 0x72, "GLOBAL_PRESET_1": 0x38}  # Tested menu/preset bindings.
            if action.get('park_pointer_before') and key in physical_codes:
                # A game-rendered hand can hide a button after the next menu
                # opens. Move it off controls before a taught shortcut, without
                # clicking; client geometry and foreground were checked above.
                self.request('POST', self.route('/actions'), {'actions': [{
                    'type':'pointer', 'id':'mouse', 'parameters':{'pointerType':'mouse'},
                    'actions':[{'type':'pointerMove','origin':'viewport','duration':120,
                                'x':now.client_x-now.outer_x+2,
                                'y':now.client_y-now.outer_y+now.client_height-2}]}]})
                if discover_window() != now or time.monotonic()-frame.captured_at > 2:
                    raise WindowError('Input cancelled: window changed or observation became stale while parking the pointer.')
            if key in physical_codes:
                code = physical_codes[key]
                self.request("POST", self.route("/execute/sync"), {
                    "script": "windows:keys", "args": [{
                        "actions": [{"virtualKeyCode": code, "down": True},
                                    {"pause": 80},
                                    {"virtualKeyCode": code, "down": False}],
                        "forceUnicode": False,
                    }],
                })
                return
            allowed = {f"F{i}": chr(0xE030 + i) for i in range(1, 13)}
            allowed.update({"ESCAPE": "\ue00c", "ENTER": "\ue007"})
            if key not in allowed:
                raise DriverError("Key is not in the initial menu-navigation key allowlist.")
            source = {"type": "key", "id": "keyboard", "actions": [
                {"type": "keyDown", "value": allowed[key]}, {"type": "pause", "duration": 80},
                {"type": "keyUp", "value": allowed[key]}]}
        else:
            raise DriverError("The transport only supports one click or menu key.")
        self.request("POST", self.route("/actions"), {"actions": [source]})
