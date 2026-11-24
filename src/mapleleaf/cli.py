"""Small observer CLI. Live mode executes a single taught navigation step only."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image

from .capture import NovaSession
from .engine import ActionGuard, RunLedger, plan_next
from .knowledge import KnowledgeStore
from .runtime import WindowLease, event, new_run, write_json
from .vision import VisionDetector, annotate
from .observation import make_observation
from .paths import ROOT
NAVIGATION_KEY_ANCHORS = {
    ("HOME", "BACKTICK"): ("quickslot_clock",),
    ("HOME", "BACKSLASH"): ("quickslot_clock",),
    ("HOME", "F2"): ("dungeons_shortcut", "map_globe"),
}


def load_workflow(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
        raise ValueError("Workflow must contain an explicit steps list.")
    return value


def persist_observation(frame, detection, observation, run_dir, sequence):
    stem = f"{sequence:04d}"
    frame_path = run_dir / f"frame-{stem}.png"
    annotated_path = run_dir / f"annotated-{stem}.png"
    Image.fromarray(frame.rgb).save(frame_path)
    annotate(frame.rgb, detection, observation["plan"]).save(annotated_path)
    observation.update(frame_path=str(frame_path), annotated_path=str(annotated_path))
    write_json(run_dir / f"observation-{stem}.json", observation)
    event(run_dir, "observation", sequence=sequence, state=observation["state"],
          blocked=observation["blocked"], preset=observation["preset"], plan=observation["plan"],
          frame=frame_path.name, reasons=observation["reasons"])
    with KnowledgeStore(run_dir.parent.parent / "data/progress.sqlite") as knowledge:
        knowledge.record_observation(run_dir / f"observation-{stem}.json", observation)


def _collect(session, detector, workflow, run_dir, sequence):
    frame = session.capture_window()
    detection = detector.detect(frame.rgb)
    observation = make_observation(frame, detection, detector.profile)
    observation["plan"] = plan_next(observation, workflow)
    target = observation["plan"].get("target")
    if target and observation["anchors"].get(target, {}).get("bbox"):
        x, y, width, height = observation["anchors"][target]["bbox"]
        observation["plan"]["point"] = [round(x + width / 2), round(y + height / 2)]
    persist_observation(frame, detection, observation, run_dir, sequence)
    return observation, frame


def collect_observation(session, detector, workflow, run_dir, sequence):
    return _collect(session, detector, workflow, Path(run_dir), sequence)[0]


def print_observation(observation):
    plan = observation["plan"]
    preset = observation["preset"].get("number") or "unknown"
    print(f"State: {observation['state']} | Global preset: {preset} | Blocked: {observation['blocked']}")
    print(f"Plan: {plan['type']} {plan.get('target', plan.get('key', ''))} {plan.get('point', '')}")
    print(f"Reason: {plan['reason']}")
    print(f"Annotated frame: {observation['annotated_path']}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run-quest":
        from .runner_cli import main as runner_main
        return runner_main(argv[1:])
    if argv and argv[0] == "run-dailies":
        from .daily_runner import main as daily_main
        return daily_main(argv[1:])
    if argv and argv[0] == "knowledge":
        from .knowledge import main as knowledge_main
        return knowledge_main(argv[1:], root=ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "observe", "detect", "step", "dashboard"])
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "vision.json")
    parser.add_argument("--workflow", type=Path, default=ROOT / "workflows" / "explore.json")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:4723")
    parser.add_argument("--live", action="store_true", help="Authorize one configured navigation action, never a quest entry.")
    parser.add_argument("--focus", action="store_true", help="Activate the verified game window once when attaching; later focus loss still stops.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "dashboard":
        from .dashboard import serve_dashboard
        serve_dashboard(ROOT, port=args.port, server=args.server)
        return 0
    run_dir = new_run(ROOT, args.command)
    print(f"Run: {run_dir}")
    try:
        if args.command == "capture":
            with WindowLease(), NovaSession(args.server, focus_on_attach=args.focus) as session:
                frame = session.capture_window()
                Image.fromarray(frame.rgb).save(run_dir / "frame-0001.png")
                write_json(run_dir / "window.json", frame.window.as_dict())
                event(run_dir, "capture", latency_ms=frame.latency_ms, window=frame.window.as_dict())
                print(run_dir / "frame-0001.png")
            return 0
        detector = VisionDetector(args.profile)
        workflow = load_workflow(args.workflow)
        if args.command == "detect":
            if not args.image:
                raise ValueError("detect requires --image with a client-area screenshot.")
            rgb = np.asarray(Image.open(args.image).convert("RGB"))
            result = detector.detect(rgb)
            write_json(run_dir / "detection.json", result.as_dict())
            annotate(rgb, result).save(run_dir / "annotated.png")
            print(json.dumps(result.as_dict(), indent=2))
            return 0 if not result.blocked else 2
        with WindowLease(), NovaSession(args.server, focus_on_attach=args.focus) as session:
            first, _ = _collect(session, detector, workflow, run_dir, 1)
            if args.command != "step" or not args.live:
                print_observation(first)
                event(run_dir, "dry_run", input_sent=False)
                return 0
            # The first prototype can explore menus only. Entries/claims/skills
            # remain unavailable even if a draft workflow requests them.
            if workflow.get("allow_live_navigation") is not True or workflow.get("execution_enabled") is not True:
                raise RuntimeError("Live navigation is not enabled for this taught workflow.")
            time.sleep(0.25)
            observation, frame = _collect(session, detector, workflow, run_dir, 2)
            action = observation["plan"]
            if first["state"] != observation["state"] or first["blocked"] or observation["blocked"]:
                raise RuntimeError("Screen recognition is not stable across two observations.")
            if action.get("consequential") or action["type"] not in {"click_anchor", "key"}:
                raise RuntimeError("The observer prototype permits only a single taught menu-navigation action.")
            if action["type"] == "key":
                pair = [observation["state"], action.get("key")]
                required = NAVIGATION_KEY_ANCHORS.get(tuple(pair))
                if required is None or pair not in workflow.get("reviewed_navigation_keys", []):
                    raise RuntimeError("Key is not a reviewed navigation binding for this state.")
                if not all(item["anchors"].get(anchor, {}).get("found")
                           and not item["anchors"].get(anchor, {}).get("ambiguous")
                           for item in (first, observation) for anchor in required):
                    raise RuntimeError("Required navigation anchors are not visible across both observations.")
            if action["type"] == "click_anchor":
                spec = detector.profile["anchors"].get(action["target"], {})
                if spec.get("click_safe") is not True:
                    raise RuntimeError("Anchor is not approved for navigation clicks.")
                first_box = first["anchors"].get(action["target"], {}).get("bbox")
                latest_box = observation["anchors"][action["target"]]["bbox"]
                if not first_box or max(abs(a-b) for a,b in zip(first_box,latest_box)) > 5:
                    raise RuntimeError("Click target moved between observations.")
            with RunLedger(ROOT / "data" / "progress.sqlite") as ledger:
                guard = ActionGuard(ledger, frame.window.identity)
                decision = guard.authorize(action, observation, live_authorized=True, dry_run=False)
                event(run_dir, "guard_decision", **asdict(decision), action=action)
                if not decision.allowed:
                    raise RuntimeError(decision.reason)
                try:
                    session.dispatch(action, frame)
                    event(run_dir, "input_sent", intent_id=decision.intent_id, action=action)
                    time.sleep(0.7)
                    after, _ = _collect(session, detector, workflow, run_dir, 3)
                    expected = action.get("success_state")
                    confirmed = expected and after["state"] == expected and not after["blocked"]
                    success_anchors = action.get("success_anchors", [])
                    confirmed = confirmed and all(after["anchors"].get(anchor, {}).get("found") for anchor in success_anchors)
                    confirmed = confirmed and all(name in after and type(after[name]) is type(value) and after[name] == value
                                                  for name, value in action.get("success_facts", {}).items())
                    ledger.action_outcome(decision.intent_id, "confirmed" if confirmed else "uncertain", {
                        "before": observation["state"], "after": after["state"], "expected": expected,
                        "screenshot": after["frame_path"],
                    })
                    print_observation(after)
                    print("Navigation verified." if confirmed else "Action sent; resulting screen needs inspection. No further input sent.")
                    if not confirmed:
                        return 2
                except BaseException as exc:
                    intent = ledger.get_intent(decision.intent_id)
                    if intent and intent["outcome"] == "pending":
                        ledger.action_outcome(decision.intent_id, "uncertain", {"error": str(exc)})
                    raise
        return 0
    except KeyboardInterrupt:
        event(run_dir, "stopped", input_cancelled=True)
        print("Supervisor stopped. Game auto-play state is unchanged/unknown.")
        return 130
    except Exception as exc:
        event(run_dir, "failure", error=str(exc))
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
