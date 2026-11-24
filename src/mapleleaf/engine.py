"""Pure workflow planning and input authorization; this module never sends input.

Coordinates are client pixels. Consequential deduplication keys must identify the
character, reset period, activity and operation (or confirmed boss encounter).
An intent is a reserved attempt, not proof that the game accepted an action.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Callable
import uuid


CONSEQUENTIAL = frozenset({"entry", "claim", "boss_skill"})
INPUT_TYPES = frozenset({"click_anchor", "key"})
BLOCKED_STATES = frozenset({"UNKNOWN", "BLOCKED", "PAUSED", "STOPPED", "NEEDS_OPERATOR", "OUTCOME_UNCERTAIN", "DISCONNECTED"})


def _stop(state: str, reason: str) -> dict:
    return {"type": "stop", "state": state, "reason": reason}


def plan_next(observation: dict, workflow: dict) -> dict:
    """Describe one action without changing state, writing a journal or clicking.

    Steps have ``state``, ``action`` and optional ``reason`` and ``when`` facts.
    Conditions match exact top-level observation values and types. A
    ``step_index`` selects a specific ordered step; otherwise exactly one step
    must match both state and facts. Repeated states cannot restart a quest.
    ``execution_enabled=False`` still permits planning for a dry-run.
    """
    state = observation.get("state", "UNKNOWN")
    if not isinstance(state, str) or state in BLOCKED_STATES or observation.get("blocked"):
        return _stop(str(state), "The current state does not permit an action.")
    if workflow.get("enabled") is not True:
        return _stop(state, "This workflow is disabled.")
    if observation.get("overlay"):
        return _stop(state, "A blocking overlay must be resolved before planning.")
    steps = workflow.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        return _stop(state, "Workflow steps are invalid.")
    for step in steps:
        conditions = step.get("when", {})
        if not isinstance(conditions, dict) or not all(isinstance(name, str) and name for name in conditions):
            return _stop(state, "Step conditions must be a dictionary of named observation facts.")

    def matches(step: dict) -> bool:
        return step.get("state") == state and all(
            name in observation and type(observation[name]) is type(expected) and observation[name] == expected
            for name, expected in step.get("when", {}).items()
        )

    index = workflow.get("step_index")
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(steps):
            return _stop(state, "The workflow step index is invalid or complete.")
        candidates = [steps[index]] if matches(steps[index]) else []
    else:
        candidates = [step for step in steps if matches(step)]
    if len(candidates) != 1:
        return _stop(state, "No unique configured step matches this state and its observed facts.")
    step = candidates[0]
    action = step.get("action")
    if not isinstance(action, dict) or action.get("type") not in INPUT_TYPES | {"wait", "stop"}:
        return _stop(state, "The configured action is unsupported.")
    action = deepcopy(action)
    if action["type"] == "click_anchor":
        target = action.get("target")
        anchors = observation.get("anchors", {})
        match = anchors.get(target) if isinstance(anchors, dict) and isinstance(target, str) else None
        if not isinstance(match, dict) or match.get("found") is not True or match.get("ambiguous"):
            return _stop(state, "The intended anchor is not visible with sufficient evidence.")
    if action["type"] == "key" and not isinstance(action.get("key"), str):
        return _stop(state, "The configured key is missing.")
    action.update(state=state, workflow_id=workflow.get("id"),
                  execution_enabled=workflow.get("execution_enabled") is True,
                  reason=step.get("reason", action.get("reason", "Configured action for the observed state.")))
    for name in ("success_state", "success_anchors", "success_facts"):
        # Postconditions belong to the selected step, not another same-state
        # step or an old nested action field. The caller verifies this copy.
        action.pop(name, None)
        if name in step:
            action[name] = deepcopy(step[name])
    return action


class DuplicateActionIntent(RuntimeError):
    """The semantic operation already has an attempt that cannot be repeated."""


class ClickRateExceeded(RuntimeError):
    """The shared journal already reserved this minute's pointer budget."""


class RunLedger:
    """Durable intent journal with atomic cross-connection deduplication.

    Pending, uncertain, failed and confirmed attempts retain their budget. Only
    an explicitly reconciled ``not_sent`` outcome releases it. This is an
    at-most-one-reserved-attempt policy, not exactly-once game execution.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS action_intents (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                consequential TEXT,
                dedup_key TEXT,
                action_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                evidence_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS reserved_operation
                ON action_intents(dedup_key)
                WHERE consequential IS NOT NULL AND outcome != 'not_sent';
            CREATE INDEX IF NOT EXISTS action_intents_created_at
                ON action_intents(created_at);
        """)

    def action_intent(self, action: dict, *, max_clicks_per_minute: int | None = None,
                      wall_time: float | None = None) -> str:
        """Persist one reservation, atomically applying an optional click limit.

        UTC wall time survives process restarts; monotonic time is still used by
        the guard for capture age. Future-dated attempts remain counted after a
        backward wall-clock adjustment, so that adjustment cannot reset a budget.
        """
        category = action.get("consequential")
        if category is not None and (not isinstance(category, str) or category not in CONSEQUENTIAL):
            raise ValueError("Unknown consequential action category.")
        key = action.get("dedup_key") if category else None
        if category and (not isinstance(key, str) or not key.strip()):
            raise ValueError("Consequential actions require a semantic dedup_key.")
        payload = json.dumps(action, sort_keys=True, allow_nan=False)
        intent_id = uuid.uuid4().hex
        now = time.time() if wall_time is None else wall_time
        if not _finite_number(now):
            raise ValueError("The action journal clock is invalid.")
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="microseconds")
        if max_clicks_per_minute is not None and (type(max_clicks_per_minute) is not int or max_clicks_per_minute < 1):
            raise ValueError("The pointer-click limit must be a positive integer.")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                if action.get("type") == "click_anchor" and max_clicks_per_minute is not None:
                    cutoff = datetime.fromtimestamp(now - 60, timezone.utc).isoformat(timespec="microseconds")
                    recent = self.connection.execute(
                        "SELECT action_json FROM action_intents WHERE created_at > ?", (cutoff,)
                    )
                    count = sum(json.loads(row[0]).get("type") == "click_anchor" for row in recent)
                    if count >= max_clicks_per_minute:
                        raise ClickRateExceeded("The rolling pointer-click limit has been reached.")
                self.connection.execute(
                    "INSERT INTO action_intents VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
                    (intent_id, timestamp, timestamp, category, key, payload),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateActionIntent("This operation already has a reserved or completed attempt; reconcile it before continuing.") from exc
        return intent_id

    def action_outcome(self, intent_id: str, outcome: str, evidence: object = None) -> None:
        if outcome not in {"confirmed", "uncertain", "failed", "not_sent"}:
            raise ValueError("Outcome must be confirmed, uncertain, failed or not_sent.")
        if outcome == "not_sent" and not evidence:
            raise ValueError("Releasing a reserved attempt requires evidence that input was not sent.")
        evidence_json = json.dumps(evidence, allow_nan=False)
        with self.connection:
            changed = self.connection.execute(
                "UPDATE action_intents SET outcome=?, evidence_json=?, updated_at=? "
                "WHERE id=? AND outcome IN ('pending', 'uncertain', 'failed')",
                (outcome, evidence_json, datetime.now(timezone.utc).isoformat(), intent_id),
            ).rowcount
            if not changed:
                raise ValueError("Intent does not exist or already has a final outcome.")

    def get_intent(self, intent_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM action_intents WHERE id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def unresolved_intents(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM action_intents WHERE consequential IS NOT NULL "
            "AND outcome IN ('pending', 'uncertain', 'failed') ORDER BY created_at"
        )]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RunLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str
    intent_id: str | None = None


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class ActionGuard:
    ledger: RunLedger
    expected_window_identity: str
    expected_size: tuple[int, int] = (1920, 1080)
    expected_dpi: int = 144
    max_capture_age_s: float = 2.0
    max_preset_age_s: float = 30.0
    max_clicks_per_minute: int = 20
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    paused: bool = False
    stopped: bool = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        """Resume from pause only; STOP requires a new explicitly started guard."""
        self.paused = False

    def stop(self) -> None:
        """Latch supervisor STOP; this does not turn off the game's Auto Battle."""
        self.stopped = True

    def authorize(self, action: dict, observation: dict, *, live_authorized: bool = False,
                  dry_run: bool = True) -> GuardDecision:
        """Reserve a verified action before dispatch. The caller still sends it.

        Call immediately before a single input and record its observed outcome.
        A successful decision is not a reusable permit. The runtime must keep
        one dispatch in flight and hold exclusive ownership of the game window.
        """
        def reject(reason: str) -> GuardDecision:
            return GuardDecision(False, reason)

        if self.stopped:
            return reject("Supervisor STOP is active.")
        if self.paused:
            return reject("Supervisor is paused.")
        if dry_run is not False or live_authorized is not True:
            return reject("Dry-run or missing live authorization blocks input.")
        if action.get("execution_enabled") is not True:
            return reject("Live execution is disabled for this workflow.")
        kind = action.get("type")
        if kind not in INPUT_TYPES:
            return reject("This plan does not request an input action.")
        state = observation.get("state")
        if not isinstance(state, str) or not state or state in BLOCKED_STATES or observation.get("blocked"):
            return reject("Current state is unknown or blocked.")
        if action.get("state") != state:
            return reject("The planned state no longer matches the observation.")
        if observation.get("overlay"):
            return reject("An overlay blocks the intended action.")
        window = observation.get("window", {})
        if not isinstance(window, dict) or not self.expected_window_identity or window.get("identity") != self.expected_window_identity:
            return reject("Game window identity is uncertain or changed.")
        if window.get("foreground") is not True:
            return reject("The game is not the foreground window.")
        if window.get("minimized"):
            return reject("The game window is minimized.")
        if (window.get("client_width"), window.get("client_height")) != self.expected_size:
            return reject("The game client resolution changed.")
        if window.get("dpi") != self.expected_dpi:
            return reject("The game window DPI changed.")
        now = self.clock()
        captured_at = observation.get("captured_at")
        if not _finite_number(captured_at) or not 0 <= now - captured_at <= self.max_capture_age_s:
            return reject("The capture is stale or its timestamp is invalid.")
        category = action.get("consequential")
        if category is not None and (not isinstance(category, str) or category not in CONSEQUENTIAL):
            return reject("The consequential action category is unknown.")
        if category in {"entry", "boss_skill"}:
            preset = observation.get("preset", {})
            if not isinstance(preset, dict) or type(preset.get("number")) is not int or preset.get("number") != 1 or preset.get("invalidated") is not False:
                return reject("Preset 1 has not been verified for this activity.")
            if preset.get("window_identity") != self.expected_window_identity:
                return reject("Preset evidence belongs to a different window.")
            verified_at = preset.get("verified_at")
            if not _finite_number(verified_at) or not 0 <= now - verified_at <= self.max_preset_age_s:
                return reject("Preset 1 evidence is stale or invalid.")
            if preset.get("context_epoch") != observation.get("context_epoch"):
                return reject("Preset evidence was invalidated by a context change.")
        if kind == "click_anchor":
            anchors = observation.get("anchors", {})
            target = action.get("target")
            match = anchors.get(target) if isinstance(anchors, dict) and isinstance(target, str) else None
            if not isinstance(match, dict) or match.get("found") is not True or match.get("ambiguous"):
                return reject("The click target is not verified in the current frame.")
            box = match.get("bbox")
            if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_finite_number(value) for value in box):
                return reject("The click target has invalid client coordinates.")
            x, y, width, height = box
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > self.expected_size[0] or y + height > self.expected_size[1]:
                return reject("The click target is outside the game client.")
            if "point" in action:
                point = action["point"]
                if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(type(value) is int for value in point):
                    return reject("The dispatched click point must contain integer client coordinates.")
                if not (x <= point[0] < x + width and y <= point[1] < y + height):
                    return reject("The dispatched click point is outside its verified anchor.")
        elif not isinstance(action.get("key"), str) or not action["key"].strip():
            return reject("The configured key is missing.")
        try:
            intent_id = self.ledger.action_intent(
                action, max_clicks_per_minute=self.max_clicks_per_minute, wall_time=self.wall_clock()
            )
        except ClickRateExceeded as exc:
            return reject(str(exc))
        except (DuplicateActionIntent, TypeError, ValueError, sqlite3.Error, OSError) as exc:
            return reject(f"The action could not be journaled: {exc}")
        return GuardDecision(True, "Action reserved; dispatch once and verify its outcome.", intent_id)
