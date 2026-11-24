"""Read-only template matching for fixed-size game client frames.

Scores are OpenCV normalized correlation values, not probabilities.  A match
must exceed its configured threshold and be distinct from other occurrences.
No function in this module captures a screen or sends input.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class AnchorMatch:
    found: bool
    score: float
    bbox: list[int] | None
    ambiguous: bool = False
    color_error: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "score": self.score,
            "bbox": self.bbox,
            "ambiguous": self.ambiguous,
            "color_error": self.color_error,
        }


@dataclass(frozen=True)
class Detection:
    state: str
    anchors: dict[str, AnchorMatch]
    reasons: list[str]
    blocked: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "anchors": {name: match.as_dict() for name, match in self.anchors.items()},
            "reasons": list(self.reasons),
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class _AnchorSpec:
    template: np.ndarray
    template_rgb: np.ndarray
    roi: tuple[int, int, int, int]
    threshold: float
    min_margin: float
    click_safe: bool
    max_color_error: float | None
    pixel_mode: str


def _anchor_pixels(rgb: np.ndarray, mode: str) -> np.ndarray:
    if mode == "neutral_white":
        # Absolute brightness rejects dimmed overlays; chroma separates white
        # HUD lettering from animated coloured scenery behind it.
        low, high = rgb.min(axis=2), rgb.max(axis=2)
        return ((low >= 235) & (high - low <= 15)).astype(np.uint8) * 255
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


class VisionDetector:
    """Load an explicit profile and recognize only unambiguous known states.

    Template paths are relative to the profile JSON.  The frame must be an RGB
    uint8 array of exactly ``client_size``; no implicit resizing is performed.
    Invalid profiles fail at initialization rather than silently disabling a
    detector.  Frame errors fail closed with a blocked UNKNOWN detection.
    """

    def __init__(self, profile_path: Path):
        self.profile_path = Path(profile_path).resolve()
        try:
            self.profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot read vision profile {self.profile_path}: {error}") from error
        profile = self.profile
        if not isinstance(profile, dict) or profile.get("schema_version") != 1:
            raise ValueError("Vision profile must use schema_version 1")
        size = profile.get("client_size")
        if not self._int_list(size, 2) or min(size) <= 0:
            raise ValueError("client_size must be [width, height] in positive integer pixels")
        self.client_size = tuple(size)
        anchors = profile.get("anchors")
        if not isinstance(anchors, dict) or not anchors:
            raise ValueError("Vision profile must contain at least one anchor")
        self._anchors = {
            name: self._load_anchor(name, spec) for name, spec in anchors.items()
        }
        states = profile.get("states")
        if not isinstance(states, dict) or not states:
            raise ValueError("Vision profile must contain at least one state")
        self._states: dict[str, tuple[list[str], list[str]]] = {}
        for name, rule in states.items():
            if not isinstance(name, str) or not name or name == "UNKNOWN":
                raise ValueError("State names must be nonempty strings other than UNKNOWN")
            if not isinstance(rule, dict):
                raise ValueError(f"State {name}: rules must be an object")
            required, excluded = rule.get("all_of"), rule.get("none_of", [])
            if not isinstance(required, list) or not required:
                raise ValueError(f"State {name}: all_of must contain at least one anchor")
            if not isinstance(excluded, list):
                raise ValueError(f"State {name}: none_of must be a list")
            for anchor in required + excluded:
                if not isinstance(anchor, str) or anchor not in self._anchors:
                    raise ValueError(f"State {name}: unknown anchor {anchor!r}")
            if set(required) & set(excluded):
                raise ValueError(f"State {name}: the same anchor cannot be required and excluded")
            self._states[name] = (required, excluded)

    @staticmethod
    def _int_list(value: Any, length: int) -> bool:
        return (
            isinstance(value, list)
            and len(value) == length
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        )

    def _load_anchor(self, name: str, spec: Any) -> _AnchorSpec:
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("Anchor names must be nonempty strings with object definitions")
        roi = spec.get("roi")
        width, height = self.client_size
        if not self._int_list(roi, 4):
            raise ValueError(f"Anchor {name}: roi must be [left, top, right, bottom]")
        left, top, right, bottom = roi
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError(f"Anchor {name}: roi falls outside client_size")
        threshold = spec.get("threshold", 0.9)
        margin = spec.get("min_margin", 0.03)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 1
        ):
            raise ValueError(f"Anchor {name}: threshold must be greater than 0 and at most 1")
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not 0 <= margin <= 1
        ):
            raise ValueError(f"Anchor {name}: min_margin must be between 0 and 1")
        click_safe = spec.get("click_safe", False)
        if not isinstance(click_safe, bool):
            raise ValueError(f"Anchor {name}: click_safe must be boolean")
        max_color_error = spec.get("max_color_error")
        if "max_color_error" in spec and (
            isinstance(max_color_error, bool)
            or not isinstance(max_color_error, (int, float))
            or not math.isfinite(max_color_error)
            or not 0 <= max_color_error <= 255
        ):
            raise ValueError(f"Anchor {name}: max_color_error must be finite and between 0 and 255")
        template_path = spec.get("template")
        if not isinstance(template_path, str) or not template_path:
            raise ValueError(f"Anchor {name}: template path is required")
        path = self.profile_path.parent / template_path
        # Pillow supports Unicode Windows paths and consistently defines RGB.
        try:
            with Image.open(path) as image:
                template_rgb = np.array(image.convert("RGB"), dtype=np.uint8)
        except (OSError, ValueError) as error:
            raise ValueError(f"Anchor {name}: cannot read template {path}: {error}") from error
        pixel_mode = spec.get("pixel_mode", "gray")
        if not isinstance(pixel_mode, str) or pixel_mode not in {"gray", "neutral_white"}:
            raise ValueError(f"Anchor {name}: unknown pixel_mode")
        template = _anchor_pixels(template_rgb, pixel_mode)
        template_height, template_width = template.shape
        if min(template.shape) < 4:
            raise ValueError(f"Anchor {name}: template must be at least 4 by 4 pixels")
        if template_width > right - left or template_height > bottom - top:
            raise ValueError(f"Anchor {name}: template is larger than its roi")
        if float(template.std()) < 3.0 or int(template.max()) - int(template.min()) < 10:
            raise ValueError(f"Anchor {name}: uniform or near-uniform template is unsafe")
        return _AnchorSpec(template, template_rgb, tuple(roi), float(threshold), float(margin),
                           click_safe, float(max_color_error) if max_color_error is not None else None, pixel_mode)

    def _invalid_frame(self, reason: str) -> Detection:
        return Detection(
            state="UNKNOWN",
            anchors={name: AnchorMatch(False, 0.0, None) for name in self._anchors},
            reasons=[reason],
            blocked=True,
        )

    def detect(self, rgb: np.ndarray) -> Detection:
        if not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3:
            return self._invalid_frame("Frame must be an RGB array with shape [height, width, 3]")
        actual_size = (rgb.shape[1], rgb.shape[0])
        if actual_size != self.client_size:
            return self._invalid_frame(
                f"Client size changed: expected {self.client_size}, received {actual_size}; input blocked"
            )
        if rgb.dtype != np.uint8:
            return self._invalid_frame("Frame must contain uint8 RGB pixels; input blocked")
        gray = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY)
        matches: dict[str, AnchorMatch] = {}
        reasons: list[str] = []
        for name, spec in self._anchors.items():
            match = self._match(gray, rgb, spec)
            matches[name] = match
            if match.ambiguous:
                reasons.append(f"Anchor {name} has competing non-overlapping matches")
            if spec.max_color_error is not None and match.color_error is not None and match.color_error > spec.max_color_error:
                reasons.append(f"Anchor {name} RGB error {match.color_error:.2f} exceeds {spec.max_color_error:.2f}")
        candidates = []
        for name, (required, excluded) in self._states.items():
            # An ambiguous forbidden anchor may be present, so cannot establish
            # absence.  This prevents a duplicated overlay from exposing HOME.
            if all(matches[item].found for item in required) and all(
                not matches[item].found and not matches[item].ambiguous for item in excluded
            ):
                candidates.append(name)
        if len(candidates) == 1:
            state = candidates[0]
            reasons.append(f"Exactly one state matched: {state}")
        elif not candidates:
            state = "UNKNOWN"
            reasons.append("No state has all required anchors and no excluded anchors")
        else:
            state = "UNKNOWN"
            reasons.append(f"Conflicting states matched: {', '.join(candidates)}")
        blocked = state == "UNKNOWN" or any(
            matches[name].ambiguous and spec.click_safe for name, spec in self._anchors.items()
        )
        if state != "UNKNOWN" and blocked:
            reasons.append("An anchor marked click_safe is ambiguous; input blocked")
        return Detection(state, matches, reasons, blocked)

    @staticmethod
    def _match(gray: np.ndarray, rgb: np.ndarray, spec: _AnchorSpec) -> AnchorMatch:
        left, top, right, bottom = spec.roi
        roi = (gray[top:bottom, left:right] if spec.pixel_mode == "gray"
               else _anchor_pixels(rgb[top:bottom, left:right], spec.pixel_mode))
        response = cv2.matchTemplate(roi, spec.template, cv2.TM_CCOEFF_NORMED)
        response = np.nan_to_num(response, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, score, _, location = cv2.minMaxLoc(response)
        score = float(np.clip(score, -1.0, 1.0))
        if score < spec.threshold:
            return AnchorMatch(False, score, None)
        x, y = location
        height, width = spec.template.shape
        competitors = response.copy()
        # Suppress every candidate box that overlaps the best box. Neighboring
        # pixels of one icon must not count as a second occurrence.
        competitors[
            max(0, y - height + 1) : min(competitors.shape[0], y + height),
            max(0, x - width + 1) : min(competitors.shape[1], x + width),
        ] = -np.inf
        second = float(np.max(competitors))
        ambiguous = np.isfinite(second) and score - second < max(spec.min_margin, 1e-6)
        bbox = [int(left + x), int(top + y), int(width), int(height)]
        patch = rgb[top + y : top + y + height, left + x : left + x + width]
        # Correlation normalizes brightness, so a modal's darkened background
        # can look identical in grayscale. Compare the complete RGB candidate
        # in physical pixels, without normalizing either image's colors.
        color_error = float(np.abs(patch.astype(np.float32) - spec.template_rgb.astype(np.float32)).mean())
        color_ok = spec.max_color_error is None or color_error <= spec.max_color_error
        return AnchorMatch(not bool(ambiguous) and color_ok, score, bbox, bool(ambiguous), color_error)

    def annotate(
        self, rgb: np.ndarray, detection: Detection, planned_action: Any = None
    ) -> Image.Image:
        return annotate(rgb, detection, planned_action)


def annotate(
    rgb: np.ndarray, detection: Detection, planned_action: Any = None
) -> Image.Image:
    """Return a marked frame with a readable footer outside the game pixels.

    ``planned_action`` may be a string or a dictionary containing ``label``
    (or ``name``), ``bbox`` and/or ``point`` in physical client pixels. The
    original frame is never scaled or shifted, so all source-coordinate boxes
    stay valid. Text lives below the frame and cannot obscure the game HUD.
    """
    frame = Image.fromarray(np.asarray(rgb, dtype=np.uint8)).convert("RGB")
    frame_draw = ImageDraw.Draw(frame)
    base_size = max(14, min(26, round(frame.width / 74)))
    font = _annotation_font(base_size)
    title_font = _annotation_font(max(18, min(34, round(frame.width / 56))))
    padding = max(10, round(frame.width / 100))
    line_height = base_size + 10
    title_height = title_font.size + 14
    found_labels = []
    for name, match in detection.anchors.items():
        if match.bbox is None:
            continue
        x, y, width, height = match.bbox
        color = "orange" if match.ambiguous else "lime" if match.found else "#ff6464"
        frame_draw.rectangle((x, y, x + width - 1, y + height - 1), outline=color, width=3)
        label = f"{name} {match.score:.3f}"
        if match.ambiguous:
            label += " AMBIGUOUS"
        elif not match.found:
            label += " REJECTED"
        if match.color_error is not None:
            label += f" RGB error {match.color_error:.1f}"
        found_labels.append(label)
    paragraphs = []
    if planned_action is not None:
        if isinstance(planned_action, dict):
            kind = str(planned_action.get("type", "action"))
            target = planned_action.get("target", planned_action.get("key", ""))
            label = str(planned_action.get("label", planned_action.get("name", f"{kind} {target}"))).strip()
            if kind in {"stop", "wait"} and planned_action.get("reason"):
                label += f": {planned_action['reason']}"
            bbox = planned_action.get("bbox")
            point = planned_action.get("point")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                x, y, width, height = bbox
                frame_draw.rectangle((x, y, x + width - 1, y + height - 1), outline="cyan", width=3)
            if isinstance(point, (list, tuple)) and len(point) == 2:
                x, y = point
                frame_draw.line((x - 10, y, x + 10, y), fill="cyan", width=3)
                frame_draw.line((x, y - 10, x, y + 10), fill="cyan", width=3)
                label += f" at client ({x}, {y})"
        else:
            label = str(planned_action)
        paragraphs.append((f"Preview only: {label}", "cyan"))
    paragraphs.append(("Raw similarity (not probability): " + ("  |  ".join(found_labels) or "no anchors matched"), "#d2e0e8"))
    if detection.reasons:
        paragraphs.append(("Reason: " + "  ".join(detection.reasons), "#bccbd4"))
    max_width = max(1, frame.width - 2 * padding)
    status = f"STATE: {detection.state} | {'BLOCKED' if detection.blocked else 'recognized'}"
    title_lines = _wrap_annotation_text(frame_draw, status, title_font, max_width)
    lines = [(line, color) for text, color in paragraphs
             for line in _wrap_annotation_text(frame_draw, text, font, max_width)]
    footer_height = 2 * padding + len(title_lines) * title_height + len(lines) * line_height
    image = Image.new("RGB", (frame.width, frame.height + footer_height), "#111d25")
    image.paste(frame, (0, 0))
    draw = ImageDraw.Draw(image)
    draw.line((0, frame.height, frame.width, frame.height), fill="#6c8793", width=2)
    y = frame.height + padding
    for line in title_lines:
        draw.text((padding, y), line, font=title_font, fill="orange" if detection.blocked else "lime")
        y += title_height
    for line, color in lines:
        draw.text((padding, y), line, font=font, fill=color)
        y += line_height
    return image


def _annotation_font(size: int):
    for name in ("C:/Windows/Fonts/segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _wrap_annotation_text(draw, text: str, font, max_width: int) -> list[str]:
    """Wrap long diagnostics, including identifiers with no spaces."""
    lines, current = [], ""
    for word in text.split():
        proposed = f"{current} {word}".strip()
        if current and draw.textlength(proposed, font=font) > max_width:
            lines.append(current)
            current = ""
        if draw.textlength(word, font=font) > max_width:
            for character in word:
                if current and draw.textlength(current + character, font=font) > max_width:
                    lines.append(current)
                    current = ""
                current += character
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]
