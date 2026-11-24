"""Synthetic observer checks; no game process, capture, or input is used."""

import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from mapleleaf.vision import VisionDetector, annotate


WIDTH, HEIGHT = 220, 150


def pattern(seed=1, size=(14, 18)):
    gray = np.random.default_rng(seed).integers(20, 235, size=size, dtype=np.uint8)
    return np.repeat(gray[:, :, None], 3, axis=2)


def background():
    return np.full((HEIGHT, WIDTH, 3), 60, dtype=np.uint8)


def paste(frame, template, x, y):
    height, width = template.shape[:2]
    frame[y : y + height, x : x + width] = template


def detector(tmp_path: Path, templates=None, states=None, overrides=None):
    templates = templates or {"menu": pattern()}
    anchors = {}
    for name, template in templates.items():
        Image.fromarray(template).save(tmp_path / f"{name}.png")
        anchors[name] = {
            "template": f"{name}.png",
            "roi": [0, 0, WIDTH, HEIGHT],
            "threshold": 0.90,
            "min_margin": 0.03,
            "click_safe": False,
        }
        anchors[name].update((overrides or {}).get(name, {}))
    profile = {
        "schema_version": 1,
        "client_size": [WIDTH, HEIGHT],
        "anchors": anchors,
        "states": states or {"HOME": {"all_of": ["menu"], "none_of": []}},
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return VisionDetector(path)


def test_positive_exact_location_and_serialization(tmp_path):
    vision = detector(tmp_path, overrides={"menu": {"roi": [30, 20, 190, 130]}})
    frame = background()
    paste(frame, pattern(), 53, 47)
    result = vision.detect(frame)
    assert result.state == "HOME"
    assert not result.blocked
    assert result.anchors["menu"].bbox == [53, 47, 18, 14]
    assert result.anchors["menu"].score == pytest.approx(1, abs=1e-5)
    assert result.anchors["menu"].found
    assert not result.anchors["menu"].ambiguous
    assert json.loads(json.dumps(result.as_dict()))["state"] == "HOME"


@pytest.mark.parametrize("negative", ["flat", "noise", "other_icon", "outside_roi"])
def test_negative_images_never_establish_home(tmp_path, negative):
    vision = detector(tmp_path, overrides={"menu": {"roi": [20, 20, 100, 100]}})
    frame = background()
    if negative == "noise":
        frame = np.random.default_rng(55).integers(0, 256, frame.shape, dtype=np.uint8)
    elif negative == "other_icon":
        paste(frame, pattern(9), 30, 30)
    elif negative == "outside_roi":
        paste(frame, pattern(), 150, 110)
    result = vision.detect(frame)
    assert result.state == "UNKNOWN"
    assert result.blocked
    assert not result.anchors["menu"].found
    assert result.anchors["menu"].bbox is None


def test_duplicate_non_overlapping_icon_is_ambiguous(tmp_path):
    vision = detector(tmp_path)
    frame = background()
    paste(frame, pattern(), 20, 40)
    paste(frame, pattern(), 100, 70)
    result = vision.detect(frame)
    assert result.state == "UNKNOWN"
    assert result.blocked
    assert result.anchors["menu"].ambiguous
    assert not result.anchors["menu"].found
    assert result.anchors["menu"].bbox is not None  # retain diagnostic evidence


def test_neighboring_peak_pixels_are_one_match(tmp_path):
    # Smooth texture creates many high neighboring scores. Suppressing only the
    # single best response pixel would incorrectly make this icon ambiguous.
    yy, xx = np.mgrid[0:30, 0:30]
    gray = (20 + 200 * np.exp(-((xx - 14) ** 2 + (yy - 15) ** 2) / 70)).astype(np.uint8)
    template = np.repeat(gray[:, :, None], 3, axis=2)
    vision = detector(tmp_path, {"menu": template}, overrides={"menu": {"min_margin": 0.08}})
    frame = background()
    paste(frame, template, 50, 55)
    result = vision.detect(frame)
    assert result.state == "HOME"
    assert not result.anchors["menu"].ambiguous


@pytest.mark.parametrize("frame", [np.zeros((149, 220, 3), np.uint8), np.zeros((150, 221, 3), np.uint8)])
def test_changed_resolution_blocks_without_rescaling(tmp_path, frame):
    result = detector(tmp_path).detect(frame)
    assert result.state == "UNKNOWN"
    assert result.blocked
    assert "Client size changed" in result.reasons[0]
    assert all(match.bbox is None for match in result.anchors.values())


@pytest.mark.parametrize("frame", [np.zeros((HEIGHT, WIDTH), np.uint8), np.zeros((HEIGHT, WIDTH, 3), np.float32)])
def test_malformed_frame_fails_closed(tmp_path, frame):
    result = detector(tmp_path).detect(frame)
    assert result.blocked and result.state == "UNKNOWN"


def test_multiple_state_matches_fail_closed(tmp_path):
    vision = detector(tmp_path, states={"HOME": {"all_of": ["menu"]}, "MENU": {"all_of": ["menu"]}})
    frame = background()
    paste(frame, pattern(), 60, 70)
    result = vision.detect(frame)
    assert result.state == "UNKNOWN" and result.blocked
    assert any("Conflicting states" in reason for reason in result.reasons)


@pytest.mark.parametrize("copies", [1, 2])
def test_forbidden_overlay_blocks_even_when_ambiguous(tmp_path, copies):
    templates = {"menu": pattern(), "overlay": pattern(2)}
    vision = detector(tmp_path, templates, {"HOME": {"all_of": ["menu"], "none_of": ["overlay"]}})
    frame = background()
    paste(frame, templates["menu"], 20, 20)
    paste(frame, templates["overlay"], 90, 50)
    if copies == 2:
        paste(frame, templates["overlay"], 160, 80)
    result = vision.detect(frame)
    assert result.state == "UNKNOWN" and result.blocked


def test_all_state_anchors_required(tmp_path):
    templates = {"hud": pattern(), "menu": pattern(2), "map": pattern(3)}
    vision = detector(tmp_path, templates, {"HOME": {"all_of": ["hud", "menu", "map"]}})
    frame = background()
    paste(frame, templates["hud"], 20, 20)
    paste(frame, templates["menu"], 80, 40)
    assert vision.detect(frame).state == "UNKNOWN"
    paste(frame, templates["map"], 150, 80)
    assert vision.detect(frame).state == "HOME"


def test_ambiguous_action_anchor_blocks_known_state(tmp_path):
    templates = {"menu": pattern(), "claim": pattern(2)}
    vision = detector(tmp_path, templates, overrides={"claim": {"click_safe": True}})
    frame = background()
    paste(frame, templates["menu"], 20, 20)
    paste(frame, templates["claim"], 80, 40)
    paste(frame, templates["claim"], 150, 80)
    result = vision.detect(frame)
    assert result.state == "HOME"
    assert result.blocked


@pytest.mark.parametrize("level", [0, 127, 254])
def test_uniform_templates_rejected(tmp_path, level):
    template = np.full((14, 18, 3), level, dtype=np.uint8)
    with pytest.raises(ValueError, match="uniform"):
        detector(tmp_path, {"menu": template})


def test_near_uniform_templates_rejected(tmp_path):
    template = np.random.default_rng(4).integers(125, 129, (14, 18, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="near-uniform"):
        detector(tmp_path, {"menu": template})


def test_unknown_state_anchor_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown anchor"):
        detector(tmp_path, states={"HOME": {"all_of": ["missing"]}})


def test_annotation_does_not_mutate_frame(tmp_path):
    vision = detector(tmp_path)
    frame = background()
    paste(frame, pattern(), 55, 70)
    original = frame.copy()
    result = vision.detect(frame)
    annotated = annotate(frame, result, {"label": "click menu", "point": [60, 75]})
    assert annotated.width == WIDTH
    assert annotated.height > HEIGHT
    assert np.array_equal(frame, original)
    marked = np.asarray(annotated)
    assert not np.array_equal(marked[:HEIGHT], original)
    # A footer must not shift coordinates or put a status banner over the HUD.
    assert np.array_equal(marked[:30], original[:30])
    assert tuple(marked[70, 55]) == (0, 255, 0)
    assert vision.annotate(frame, result).height > HEIGHT


def test_unknown_annotation_keeps_every_game_pixel_intact(tmp_path):
    vision = detector(tmp_path)
    frame = background()
    result = vision.detect(frame)
    annotated = annotate(frame, result, {"type": "stop", "reason": "Unknown screen"})
    assert np.array_equal(np.asarray(annotated)[:HEIGHT], frame)
    assert annotated.height > HEIGHT


def test_rgb_threshold_rejects_dimmed_background_despite_high_correlation(tmp_path):
    vision = detector(tmp_path, overrides={"menu": {"max_color_error": 18}})
    frame = background()
    dimmed = (pattern().astype(np.float32) * 0.45).astype(np.uint8)
    paste(frame, dimmed, 55, 70)
    result = vision.detect(frame)
    match = result.anchors["menu"]
    assert match.score > 0.99  # Normalized grayscale matching alone accepts it.
    assert match.color_error > 18
    assert not match.found and not match.ambiguous
    assert match.bbox == [55, 70, 18, 14]
    assert result.state == "UNKNOWN" and result.blocked
    assert any("RGB error" in reason for reason in result.reasons)
    assert result.as_dict()["anchors"]["menu"]["color_error"] == match.color_error


@pytest.mark.parametrize("brightness_delta", [0, 6])
def test_rgb_threshold_accepts_template_and_small_rendering_variation(tmp_path, brightness_delta):
    vision = detector(tmp_path, overrides={"menu": {"max_color_error": 18}})
    frame = background()
    paste(frame, pattern() + brightness_delta, 55, 70)
    result = vision.detect(frame)
    assert result.state == "HOME" and not result.blocked
    assert result.anchors["menu"].color_error == pytest.approx(brightness_delta)


def test_omitted_rgb_threshold_preserves_correlation_only_acceptance(tmp_path):
    vision = detector(tmp_path)
    frame = background()
    paste(frame, (pattern().astype(np.float32) * 0.45).astype(np.uint8), 55, 70)
    result = vision.detect(frame)
    assert result.state == "HOME"
    assert result.anchors["menu"].found
    assert result.anchors["menu"].color_error > 18


@pytest.mark.parametrize("threshold", [None, True, "18", -1, 256, float("nan"), float("inf"), float("-inf")])
def test_rgb_threshold_must_be_finite_byte_range(tmp_path, threshold):
    with pytest.raises(ValueError, match="max_color_error must be finite"):
        detector(tmp_path, overrides={"menu": {"max_color_error": threshold}})


def test_rgb_error_uses_full_rgb_patch_without_unsigned_wraparound(tmp_path):
    template = pattern()
    vision = detector(tmp_path, overrides={"menu": {"max_color_error": 18}})
    frame = background()
    changed = template.copy()
    changed[:, :, 0] -= 10
    changed[:, :, 1] += 5
    paste(frame, changed, 55, 70)
    result = vision.detect(frame)
    assert result.anchors["menu"].color_error == pytest.approx(5)
    assert result.anchors["menu"].found
