import base64
from dataclasses import replace
from email.message import Message
from io import BytesIO
from unittest.mock import Mock
from urllib.request import HTTPHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

import numpy as np
import pytest
from PIL import Image
from mapleleaf.capture import Frame, NovaSession, _RejectRedirects, crop_client, DriverError
from mapleleaf.windows import WindowError, WindowInfo


def window():
    return WindowInfo(1, 2, "MapleStoryM.exe", "MapleStoryM", "UnityWndClass", 100, 200, 12, 14, 102, 203, 8, 9, 144, True, False)


def test_crops_decorations_using_physical_client_offset():
    pixels = np.arange(14 * 12 * 3, dtype=np.uint8).reshape(14, 12, 3)
    result = crop_client(Image.fromarray(pixels), window())
    assert result.shape == (9, 8, 3)
    assert np.array_equal(result, pixels[3:12, 2:10])


def test_rejects_dimension_mismatch_instead_of_scaling():
    with pytest.raises(DriverError, match="dimensions"):
        crop_client(Image.new("RGB", (24, 28)), window())


@pytest.mark.parametrize("endpoint", [
    "https://127.0.0.1:4723", "http://example.invalid:4723", "http://127.0.0.1",
    "http://127.0.0.1:0", "http://127.0.0.1:70000", "http://127.0.0.1:wrong",
    "http://127.0.0.1:4723@example.invalid", "http://user:password@localhost:4723",
    "http://localhost:4723/path", "http://localhost:4723?redirect=elsewhere",
    "http://localhost:4723#fragment", " http://localhost:4723", "http://local\nhost:4723",
    "http://localhost.example.invalid:4723", "http://[::1:4723",
])
def test_rejects_nonlocal_or_ambiguous_driver_urls(endpoint):
    with pytest.raises(DriverError):
        NovaSession(endpoint)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:4723", "http://localhost:4723/", "HTTP://LOCALHOST:4723"])
def test_normalizes_supported_loopback_urls_without_dns(endpoint):
    assert NovaSession(endpoint).server == "http://127.0.0.1:4723"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_driver_requests_never_follow_redirects(status):
    opened = []

    class RedirectingHTTPHandler(HTTPHandler):
        def http_open(self, request):
            opened.append(request.full_url)
            headers = Message()
            headers["Location"] = "http://example.invalid/elsewhere"
            response = addinfourl(BytesIO(b"redirect"), headers, request.full_url, status)
            response.msg = "Redirect"
            return response

    session = NovaSession()
    # A synthetic HTTP handler runs the real urllib redirect chain without a
    # listening server, DNS lookup or network request.
    session._opener = build_opener(ProxyHandler({}), _RejectRedirects(), RedirectingHTTPHandler())
    with pytest.raises(DriverError, match="redirects are disabled"):
        session.request("GET", "/status")
    assert opened == ["http://127.0.0.1:4723/status"]


def attached_session():
    session = NovaSession()
    session.identity = window().identity
    session.session_id = "test-session"
    session.request = Mock(return_value="unused")
    return session


def test_foreground_loss_before_capture_sends_no_driver_request(monkeypatch):
    session = attached_session()
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: replace(window(), foreground=False))
    with pytest.raises(WindowError, match="foreground"):
        session.capture_window()
    session.request.assert_not_called()


@pytest.mark.parametrize("after", [replace(window(), foreground=False), replace(window(), minimized=True)])
def test_foreground_loss_during_capture_discards_the_frame(monkeypatch, after):
    session = attached_session()
    states = iter([window(), after])
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: next(states))
    with pytest.raises(WindowError, match="during capture"):
        session.capture_window()
    session.request.assert_called_once_with("GET", "/session/test-session/screenshot")


def test_unobscured_foreground_capture_returns_client_pixels(monkeypatch):
    session = attached_session()
    monkeypatch.setattr("mapleleaf.capture.discover_window", window)
    pixels = np.arange(14 * 12 * 3, dtype=np.uint8).reshape(14, 12, 3)
    encoded = BytesIO()
    Image.fromarray(pixels).save(encoded, format="PNG")
    session.request.return_value = base64.b64encode(encoded.getvalue()).decode("ascii")
    frame = session.capture_window()
    assert np.array_equal(frame.rgb, pixels[3:12, 2:10])
    assert frame.window.foreground is True
    session.request.assert_called_once_with("GET", "/session/test-session/screenshot")


def test_click_releases_button_before_parking_cursor_in_client_space(monkeypatch):
    session = attached_session()
    monkeypatch.setattr("mapleleaf.capture.discover_window", window)
    monkeypatch.setattr("mapleleaf.capture.time.monotonic", lambda: 100.0)
    frame = Frame(np.zeros((9, 8, 3), dtype=np.uint8), window(), 100.0, "test", 0.0)
    session.dispatch({"type": "click_anchor", "point": [4, 5]}, frame)
    method, route, body = session.request.call_args.args
    assert (method, route) == ("POST", "/session/test-session/actions")
    steps = body["actions"][0]["actions"]
    assert [step["type"] for step in steps] == ["pointerMove", "pointerDown", "pause", "pointerUp", "pause", "pointerMove"]
    assert (steps[0]["x"], steps[0]["y"]) == (6, 8)
    assert steps[-3] == {"type": "pointerUp", "button": 0}
    assert steps[-2] == {"type": "pause", "duration": 150}
    # Client offset is (2,3); neutral client point (5, 9-5) maps to (7,7).
    assert steps[-1] == {"type": "pointerMove", "origin": "viewport", "x": 7, "y": 7, "duration": 0}


@pytest.mark.parametrize("name,code", [("BACKTICK", 0xC0), ("BACKSLASH", 0xDC), ("PRESET_SELECTOR", 0x31), ("F2", 0x71), ("F3", 0x72), ("GLOBAL_PRESET_1", 0x38)])
def test_confirmed_quickslot_bindings_send_explicit_windows_virtual_keys(monkeypatch, name, code):
    session = attached_session()
    monkeypatch.setattr("mapleleaf.capture.discover_window", window)
    monkeypatch.setattr("mapleleaf.capture.time.monotonic", lambda: 100.0)
    frame = Frame(np.zeros((9, 8, 3), dtype=np.uint8), window(), 100.0, "test", 0.0)
    session.dispatch({"type": "key", "key": name}, frame)
    session.request.assert_called_once_with("POST", "/session/test-session/execute/sync", {
        "script": "windows:keys", "args": [{
            "actions": [{"virtualKeyCode": code, "down": True}, {"pause": 80},
                        {"virtualKeyCode": code, "down": False}],
            "forceUnicode": False,
        }],
    })


@pytest.mark.parametrize("name", ["BACKTICK", "BACKSLASH", "PRESET_SELECTOR", "F2", "F3", "GLOBAL_PRESET_1"])
@pytest.mark.parametrize("failure", ["stale", "foreground"])
def test_quickslot_virtual_keys_still_require_fresh_foreground_evidence(monkeypatch, name, failure):
    session = attached_session()
    current = window() if failure == "stale" else replace(window(), foreground=False)
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: current)
    monkeypatch.setattr("mapleleaf.capture.time.monotonic", lambda: 103.0 if failure == "stale" else 100.0)
    frame = Frame(np.zeros((9, 8, 3), dtype=np.uint8), window(), 100.0, "test", 0.0)
    with pytest.raises(WindowError):
        session.dispatch({"type": "key", "key": name}, frame)
    session.request.assert_not_called()


@pytest.mark.parametrize("name", ["TILDE", "1", "DIGIT1", "VK1"])
def test_unconfirmed_symbol_and_digit_bindings_are_rejected(monkeypatch, name):
    session = attached_session()
    monkeypatch.setattr("mapleleaf.capture.discover_window", window)
    monkeypatch.setattr("mapleleaf.capture.time.monotonic", lambda: 100.0)
    frame = Frame(np.zeros((9, 8, 3), dtype=np.uint8), window(), 100.0, "test", 0.0)
    with pytest.raises(DriverError, match="allowlist"):
        session.dispatch({"type": "key", "key": name}, frame)
    session.request.assert_not_called()


def test_shortcut_can_park_game_cursor_without_clicking(monkeypatch):
    session=attached_session()
    monkeypatch.setattr('mapleleaf.capture.discover_window', window)
    monkeypatch.setattr('mapleleaf.capture.time.monotonic',lambda:100.)
    frame=Frame(np.zeros((9,8,3),dtype=np.uint8),window(),100.,'test',0.)
    session.dispatch({'type':'key','key':'F3','park_pointer_before':True},frame)
    calls=session.request.call_args_list
    assert len(calls)==2
    steps=calls[0].args[2]['actions'][0]['actions']
    assert steps==[{'type':'pointerMove','origin':'viewport','duration':120,'x':4,'y':10}]
    assert calls[1].args[2]['script']=='windows:keys'


@pytest.mark.parametrize('changed',[replace(window(),foreground=False),replace(window(),client_x=120)])
def test_no_shortcut_if_window_changes_during_pointer_move(monkeypatch,changed):
    session=attached_session()
    windows=iter([window(),changed])
    monkeypatch.setattr('mapleleaf.capture.discover_window',lambda:next(windows))
    monkeypatch.setattr('mapleleaf.capture.time.monotonic',lambda:100.)
    frame=Frame(np.zeros((9,8,3),dtype=np.uint8),window(),100.,'test',0.)
    with pytest.raises(WindowError,match='while parking'):
        session.dispatch({'type':'key','key':'F3','park_pointer_before':True},frame)
    assert session.request.call_count==1
    assert session.request.call_args.args[1].endswith('/actions')


def test_attach_does_not_explicitly_focus_without_authorization(monkeypatch):
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: replace(window(), foreground=False))
    session = NovaSession()
    session.request = Mock(return_value={"sessionId": "attached"})
    session.__enter__()
    assert session.request.call_count == 1
    assert session.request.call_args.args[:2] == ("POST", "/session")


def test_authorized_attach_activates_window_once_without_unneeded_focus_fallback(monkeypatch):
    states = iter([replace(window(), foreground=False), replace(window(), foreground=False), window()])
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: next(states))
    sleeps = []
    monkeypatch.setattr("mapleleaf.capture.time.sleep", sleeps.append)
    session = NovaSession(focus_on_attach=True)
    session.request = Mock(side_effect=[{"sessionId": "attached"}, None])
    session.__enter__()
    assert session.request.call_args_list[1].args == ("POST", "/session/attached/window", {"handle": "0x1"})
    assert session.request.call_count == 2
    assert sleeps == [0.1]


def test_authorized_attach_uses_returned_window_element_only_when_still_background(monkeypatch):
    background = replace(window(), foreground=False)
    states = iter([background, background, background, background, window()])
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: next(states))
    sleeps = []
    monkeypatch.setattr("mapleleaf.capture.time.sleep", sleeps.append)
    element = {"element-6066-11e4-a52e-4f735466cecf": "observed-window-element"}
    session = NovaSession(focus_on_attach=True)
    session.request = Mock(side_effect=[{"sessionId": "attached"}, None, element, None])
    session.__enter__()
    assert session.request.call_args_list[2].args == ("POST", "/session/attached/execute/sync",
                                                   {"script": "windows:getWindowElement", "args": []})
    assert session.request.call_args_list[3].args == ("POST", "/session/attached/execute/sync",
                                                   {"script": "windows:setFocus", "args": [element]})
    assert sum(sleeps) <= 0.3


def test_attach_skips_element_focus_if_window_became_foreground_after_lookup(monkeypatch):
    background = replace(window(), foreground=False)
    states = iter([background, background, background, window()])
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: next(states))
    monkeypatch.setattr("mapleleaf.capture.time.sleep", lambda _: None)
    session = NovaSession(focus_on_attach=True)
    session.request = Mock(side_effect=[{"sessionId": "attached"}, None, {"element-6066-11e4-a52e-4f735466cecf": "element"}])
    session.__enter__()
    assert session.request.call_count == 3
    assert session.request.call_args.args[2]["script"] == "windows:getWindowElement"


def test_focus_rejects_identity_change_and_cleans_up_driver_session(monkeypatch):
    background = replace(window(), foreground=False)
    states = iter([background, background, replace(window(), pid=999)])
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: next(states))
    monkeypatch.setattr("mapleleaf.capture.time.sleep", lambda _: None)
    session = NovaSession(focus_on_attach=True)
    session.request = Mock(side_effect=[{"sessionId": "attached"}, None, None])
    with pytest.raises(WindowError, match="identity"):
        session.__enter__()
    assert session.request.call_args.args == ("DELETE", "/session/attached")
    assert session.session_id is None


def test_failed_authorized_focus_stops_without_repeated_attempts(monkeypatch):
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: replace(window(), foreground=False))
    monkeypatch.setattr("mapleleaf.capture.time.sleep", lambda _: None)
    session = NovaSession(focus_on_attach=True)
    session.request = Mock(side_effect=[{"sessionId": "attached"}, None,
                                       {"element-6066-11e4-a52e-4f735466cecf": "element"}, None, None])
    with pytest.raises(WindowError, match="activation failed"):
        session.__enter__()
    assert session.request.call_count == 5
    assert session.request.call_args.args == ("DELETE", "/session/attached")


def test_capture_never_refocuses_even_when_initial_focus_was_authorized(monkeypatch):
    session = attached_session()
    session.focus_on_attach = True
    monkeypatch.setattr("mapleleaf.capture.discover_window", lambda: replace(window(), foreground=False))
    with pytest.raises(WindowError, match="foreground"):
        session.capture_window()
    session.request.assert_not_called()
