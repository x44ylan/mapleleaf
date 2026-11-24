"""Loopback-only observer UI. Its commands only capture, classify and preview."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import re
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid


COMMANDS = frozenset({"start", "pause", "resume", "stop", "screenshot", "test_detection", "preview"})
INTERVAL_SECONDS = 2.0
MAX_CAPTURES = 300
DEFAULT_QUESTS = [
    ("D02", "Chaos Daily Dungeon"), ("D03", "Elite Dungeon"),
    ("D07", "Expedition / Chaos Expedition"), ("D08", "Other bosses / Commander / Root Abyss"),
    ("D09", "Dimension Invasion"), ("D10", "Mu Lung Dojo"),
    ("D15", "Daily Task / Daily Mission rewards"), ("D16", "Guild activities and rewards"),
    ("D17", "Event activities and rewards"),
]


def validate_bind(host: str, port: int) -> None:
    if host != "127.0.0.1":
        raise ValueError("The observer dashboard binds only to 127.0.0.1.")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The dashboard port must be between 1 and 65535.")


def safe_image_path(run_dir: Path | None, name: str) -> Path | None:
    """Resolve only known screenshot names within this dashboard's current run."""
    if run_dir is None or re.fullmatch(r"(?:frame|annotated)-[0-9]+\.png", name) is None:
        return None
    root = run_dir.resolve()
    path = (root / name).resolve()
    return path if path.parent == root and path.is_file() else None


def _quest_catalog(root: Path) -> list[dict]:
    for path in (root / "config/selected-dailies.yaml", root / "docs/selected-dailies.yaml"):
        if path.is_file():
            try:
                import yaml
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                items = document.get("activities", [])
                quests = [{"id": str(item["id"]), "name": str(item["name"])}
                          for item in items if isinstance(item, dict) and item.get("selected") is True]
                if quests:
                    return quests
            except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
                pass
    return [{"id": key, "name": name} for key, name in DEFAULT_QUESTS]


class ObserverController:
    """A single worker owns all driver calls; HTTP threads only enqueue commands."""

    def __init__(self, root: Path, server: str):
        self.root = Path(root).resolve()
        self.server = server
        self.commands: Queue = Queue(maxsize=16)
        self.lock = threading.Lock()
        self.shutdown = threading.Event()
        self.run_dir: Path | None = None
        self.session = None
        self.lease = None
        self.detector = None
        self.workflow = None
        self.sequence = 0
        self.state = {
            "mode": "STOPPED", "dry_run": True, "message": "Ready for read-only observation.",
            "capture_count": 0, "capture_limit": MAX_CAPTURES, "interval_seconds": INTERVAL_SECONDS,
            "observation": None, "preview": None, "quests": _quest_catalog(self.root),
            "run_directory": None, "image_url": None,
        }
        self.worker = threading.Thread(target=self._work, name="mapleleaf-observer", daemon=True)
        self.worker.start()

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _update(self, **values) -> None:
        with self.lock:
            self.state.update(values)

    def enqueue(self, command: str, quest_id: str | None = None) -> None:
        if command not in COMMANDS:
            raise ValueError("Unknown observer command.")
        if command == "preview" and quest_id not in {item["id"] for item in self.state["quests"]}:
            raise ValueError("Choose one of the configured daily activities.")
        self.commands.put_nowait((command, quest_id))

    def _new_run(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.root / "runs" / f"dashboard-{stamp}-{uuid.uuid4().hex[:6]}"
        path.mkdir(parents=True, exist_ok=False)
        self.run_dir = path
        self.sequence = 0
        self._update(capture_count=0, run_directory=str(path), observation=None, image_url=None, preview=None)

    def _open(self) -> None:
        if self.session is not None:
            return
        from .capture import NovaSession
        from .runtime import WindowLease
        from .vision import VisionDetector
        self.detector = VisionDetector(self.root / "config/vision.json")
        self.workflow = json.loads((self.root / "workflows/explore.json").read_text(encoding="utf-8"))
        self.lease = WindowLease()
        try:
            self.lease.__enter__()
            self.session = NovaSession(self.server, focus_on_attach=True)
            self.session.__enter__()
        except Exception:
            self._close()
            raise

    def _close(self) -> None:
        session, lease = self.session, self.lease
        self.session = self.lease = None
        try:
            if session is not None:
                session.__exit__(None, None, None)
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)

    def _observe(self) -> None:
        from .cli import collect_observation
        if self.sequence >= MAX_CAPTURES:
            self._close()
            self._update(mode="STOPPED", message="Capture limit reached. Start Observer creates a new run.")
            return
        self.sequence += 1
        observation = collect_observation(self.session, self.detector, self.workflow, self.run_dir, self.sequence)
        # Never expose a filesystem path as a URL; the route accepts a fixed basename.
        image_name = Path(observation["annotated_path"]).name
        self._update(observation=observation, capture_count=self.sequence,
                     image_url=f"/image/{image_name}", message="Observation saved. No game input was sent.")
        if self.sequence >= MAX_CAPTURES:
            self._close()
            self._update(mode="STOPPED", message="300-capture limit reached. Observation has stopped.")

    def _preview(self, quest_id: str) -> None:
        quest = next(item for item in self.state["quests"] if item["id"] == quest_id)
        observation = self.snapshot()["observation"]
        result = {
            "mode": "DRY_RUN_ONLY", "quest": quest, "blocked": True,
            "reasons": [
                "This activity has no validated end-to-end workflow in this observer prototype.",
                "Entry, completion, reward, and return-state detectors must be taught before a quest can run.",
                "Global Preset 1 must be freshly verified before each activity entry.",
            ],
            "latest_observation": observation.get("captured_utc") if observation else None,
            "current_state": observation.get("state", "UNKNOWN") if observation else "UNKNOWN",
            "preset": observation.get("preset") if observation else None,
            "planned_inputs": [],
        }
        self._update(preview=result, message=f"Dry-run preview for {quest['name']}: definition incomplete; no inputs.")

    def _command(self, command: str, quest_id: str | None) -> None:
        mode = self.snapshot()["mode"]
        if command == "start":
            if mode in {"RUNNING", "PAUSED"}:
                return
            self._new_run()
            self._open()
            self._update(mode="RUNNING", message="Observer started. Attaching may focus the game window.")
        elif command == "pause":
            if mode == "RUNNING":
                self._close()
                self._update(mode="PAUSED", message="Paused; capture stopped and game-window lease released.")
        elif command == "resume":
            if mode == "PAUSED":
                self._open()
                self._update(mode="RUNNING", message="Observation resumed. No game input will be sent.")
        elif command == "stop":
            self._close()
            self._update(mode="STOPPED", message="Observer stopped. The game's own Auto Battle is unchanged.")
        elif command in {"screenshot", "test_detection"}:
            if mode == "STOPPED" or self.run_dir is None:
                self._new_run()
            transient = self.session is None
            try:
                self._open()
                self._observe()
            finally:
                if transient:
                    self._close()
        elif command == "preview":
            self._preview(quest_id)

    def _work(self) -> None:
        next_capture = 0.0
        while not self.shutdown.is_set():
            try:
                try:
                    command, quest_id = self.commands.get(timeout=0.2)
                except Empty:
                    command = None
                if command:
                    self._command(command, quest_id)
                    if command in {"start", "resume"}:
                        next_capture = 0.0
                    elif command in {"screenshot", "test_detection"}:
                        next_capture = time.monotonic() + INTERVAL_SECONDS
                if self.snapshot()["mode"] == "RUNNING" and time.monotonic() >= next_capture:
                    self._observe()
                    next_capture = time.monotonic() + INTERVAL_SECONDS
            except Exception as error:
                try:
                    self._close()
                except Exception:
                    pass
                self._update(mode="STOPPED", message=f"Observation stopped: {error}")
        try:
            self._close()
        except Exception:
            pass

    def close(self) -> None:
        self.shutdown.set()
        self.worker.join(timeout=35)


HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mapleleaf Observer</title>
<style>body{font:16px system-ui;background:#101820;color:#edf5ef;margin:24px;max-width:1500px}h1{margin-bottom:6px}button,select{font:inherit;padding:10px;margin:4px;background:#20352b;color:#edf5ef;border:1px solid #6a947a;border-radius:6px}button:disabled{opacity:.5}section{background:#17252b;padding:16px;margin:16px 0;border-radius:8px}.muted{color:#b8c8ce}#frame{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px ui-monospace,monospace}.row{display:flex;gap:20px;flex-wrap:wrap}.row section{flex:1;min-width:260px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:5px;border-bottom:1px solid #33454b}.badge{color:#9dedb7}</style>
<h1>Mapleleaf Observer <small class="badge">Dry run only</small></h1>
<p class="muted">Capture, inspect, and preview. These controls send no game inputs. Attaching the driver may focus the game window. Pause releases the window for other tools.</p>
<div id="controls"><button data-command="start">Start Observer</button><button data-command="pause">Pause</button><button data-command="resume">Resume</button><button data-command="stop">Stop</button><button data-command="screenshot">Screenshot</button><button data-command="test_detection">Test Detection</button></div>
<section><strong id="mode">STOPPED</strong><p id="message">Connecting…</p><p id="summary"></p><p id="preset"></p><p class="muted">Maximum 300 captures per run; at least 2 seconds between automatic captures. Stop controls this observer only.</p></section>
<section><label for="quest">Selected daily activity </label><select id="quest"></select><button id="preview">Preview Single Quest</button><p class="muted">Quest definitions are incomplete. Preview reports missing requirements; it never starts an activity.</p><pre id="quest-preview"></pre></section>
<section><img id="frame" alt="No game frame captured yet" hidden><p id="captured" class="muted"></p></section>
<div class="row"><section><h2>Visual anchors</h2><p class="muted">Scores are template similarity, not probabilities.</p><table><thead><tr><th>Anchor</th><th>Similarity</th><th>Result</th></tr></thead><tbody id="anchors"></tbody></table></section><section><h2>Detection and intended action</h2><pre id="details"></pre></section></div>
<script src="/app.js"></script></html>"""

SCRIPT = """const $=id=>document.getElementById(id);let lastImage='',loadedQuests=false;
async function command(action){try{const body={action};if(action==='preview')body.quest_id=$('quest').value;const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();if(!r.ok)throw Error(v.error||'Command rejected');await refresh();}catch(e){$('message').textContent=e.message;}}
document.querySelectorAll('[data-command]').forEach(b=>b.onclick=()=>command(b.dataset.command));$('preview').onclick=()=>command('preview');
async function refresh(){try{const r=await fetch('/api/status');if(!r.ok)throw Error('Observer unavailable');const s=await r.json();$('mode').textContent=s.mode+' — DRY RUN';$('message').textContent=s.message;const o=s.observation||{};$('summary').textContent='State: '+(o.state||'UNKNOWN')+' | Captures: '+s.capture_count+'/'+s.capture_limit+' | Input: always disabled';$('preset').textContent='Global Preset 1: '+(o.preset&&o.preset.number===1&&o.preset.invalidated===false?'verified in latest frame':'not verified');
if(!loadedQuests){for(const q of s.quests){const e=document.createElement('option');e.value=q.id;e.textContent=q.id+' · '+q.name;$('quest').appendChild(e);}loadedQuests=true;}
const image=s.image_url? s.image_url+'?run='+encodeURIComponent(s.run_directory):'';if(image&&image!==lastImage){$('frame').src=image;$('frame').hidden=false;lastImage=image;}if(!image){$('frame').hidden=true;lastImage='';}
$('captured').textContent=o.captured_utc?'Captured '+o.captured_utc+' | '+Number(o.capture_latency_ms||0).toFixed(0)+' ms':'';$('anchors').replaceChildren();for(const [name,m] of Object.entries(o.anchors||{})){const tr=document.createElement('tr');for(const value of [name,Number(m.score).toFixed(3),m.ambiguous?'AMBIGUOUS':m.found?'found':'absent']){const td=document.createElement('td');td.textContent=value;tr.appendChild(td);}$('anchors').appendChild(tr);}
$('details').textContent=JSON.stringify({blocked:o.blocked,reasons:o.reasons,plan:o.plan,window:o.window},null,2);$('quest-preview').textContent=s.preview?JSON.stringify(s.preview,null,2):'';}catch(e){$('message').textContent=e.message;}}
refresh();setInterval(refresh,1000);"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _reply(self, code: int, content: bytes, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, code: int, value: dict):
        self._reply(code, json.dumps(value).encode("utf-8"))

    def _valid_host(self):
        return self.headers.get("Host") == self.origin.removeprefix("http://")

    def do_GET(self):
        if not self._valid_host():
            return self._json(403, {"error": "Unexpected host."})
        path = unquote(urlsplit(self.path).path)
        controller = self.server.controller
        if path == "/":
            self._reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/app.js":
            self._reply(200, SCRIPT.encode("utf-8"), "application/javascript; charset=utf-8")
        elif path == "/api/status":
            self._json(200, controller.snapshot())
        elif path.startswith("/image/"):
            image = safe_image_path(controller.run_dir, path.removeprefix("/image/"))
            if image is None:
                return self._json(404, {"error": "Image unavailable in the current run."})
            self._reply(200, image.read_bytes(), "image/png")
        else:
            self._json(404, {"error": "Unknown observer route."})

    def do_POST(self):
        if not self._valid_host() or self.headers.get("Origin") != self.origin:
            return self._json(403, {"error": "Commands require the local dashboard origin."})
        if self.path != "/api/command" or self.headers.get_content_type() != "application/json":
            return self._json(400, {"error": "Invalid observer command request."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) - {"action", "quest_id"}:
                raise ValueError("Invalid command fields.")
            self.server.controller.enqueue(body.get("action"), body.get("quest_id"))
        except Exception as error:
            return self._json(400, {"error": str(error) or "Command queue is full."})
        self._json(202, {"accepted": True, "dry_run": True})


def serve_dashboard(project_root: Path, host="127.0.0.1", port=8765, server="http://127.0.0.1:4723"):
    validate_bind(host, port)
    # Bind before starting the worker so a port collision cannot leak a thread.
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.daemon_threads = True
    controller = ObserverController(Path(project_root), server)
    httpd.controller = controller
    print(f"Mapleleaf read-only observer: http://127.0.0.1:{port}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        controller.close()
