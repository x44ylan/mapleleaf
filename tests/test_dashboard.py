from pathlib import Path
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mapleleaf.dashboard import DashboardHandler, ObserverController, safe_image_path, validate_bind


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "localhost", "example.com", "127.0.0.2", "::"])
def test_dashboard_rejects_noncanonical_loopback_bind(host):
    with pytest.raises(ValueError, match="127.0.0.1"):
        validate_bind(host, 8765)


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8765"])
def test_dashboard_rejects_invalid_ports(port):
    with pytest.raises(ValueError, match="port"):
        validate_bind("127.0.0.1", port)


def test_dashboard_accepts_fixed_loopback():
    validate_bind("127.0.0.1", 8765)


@pytest.mark.parametrize("name", ["../secret.png", "..\\secret.png", "/secret.png", "C:\\secret.png", "frame-001.png/../secret.png", "%2e%2e/secret.png", "observation-001.json", "any.png", "frame-001.png:secret", "frame-001.png\x00"])
def test_image_route_rejects_unrecognized_or_traversing_paths(tmp_path: Path, name):
    (tmp_path / "frame-001.png").write_bytes(b"image")
    assert safe_image_path(tmp_path, name) is None


def test_image_route_accepts_only_existing_current_run_screenshots(tmp_path: Path):
    frame = tmp_path / "annotated-001.png"
    frame.write_bytes(b"image")
    assert safe_image_path(tmp_path, frame.name) == frame.resolve()
    assert safe_image_path(tmp_path, "frame-999.png") is None
    assert safe_image_path(None, frame.name) is None


@pytest.fixture
def local_dashboard(tmp_path):
    controller = ObserverController(tmp_path, "http://127.0.0.1:4723")
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    server.controller = controller
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    controller.close()
    worker.join(timeout=2)


@pytest.mark.parametrize("origin", [None, "http://example.com", "null", "http://127.0.0.1:1"])
def test_commands_require_exact_same_origin(local_dashboard, origin):
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = Request(local_dashboard + "/api/command", data=b'{"action":"pause"}', headers=headers)
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 403


def test_commands_cannot_request_game_input(local_dashboard):
    request = Request(local_dashboard + "/api/command", data=b'{"action":"click","point":[10,10]}',
                      headers={"Origin": local_dashboard, "Content-Type": "application/json"})
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 400


def test_allowed_read_only_command_and_status(local_dashboard):
    request = Request(local_dashboard + "/api/command", data=b'{"action":"pause"}',
                      headers={"Origin": local_dashboard, "Content-Type": "application/json"})
    with urlopen(request, timeout=2) as response:
        assert response.status == 202
        assert json.load(response)["dry_run"] is True
    with urlopen(local_dashboard + "/api/status", timeout=2) as response:
        result = json.load(response)
    assert result["mode"] == "STOPPED"
    assert len(result["quests"]) == 9
    assert result["observation"] is None


def test_host_header_cannot_rebind_dashboard(local_dashboard):
    request = Request(local_dashboard + "/api/status", headers={"Host": "malicious.example"})
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 403
