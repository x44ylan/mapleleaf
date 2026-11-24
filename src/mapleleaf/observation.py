"""Build a fresh observation and its profile/preset context."""
from .ui_facts import quickslot_page


def make_observation(frame, detection, profile):
    context_epoch = f"{frame.window.identity}|{profile.get('profile_id', 'default')}|{profile.get('character_id', 'unspecified')}"
    observation = {
        **detection.as_dict(), "overlay": None, "overlay_status": "not_exhaustively_detected",
        "captured_at": frame.captured_at, "captured_utc": frame.captured_utc,
        "capture_latency_ms": round(frame.latency_ms, 1), "window": frame.window.as_dict(),
        "context_epoch": context_epoch,
        "character_profile": profile.get("character_id"),
        "preset": {"number": None, "invalidated": True},
        "quickslot_page": quickslot_page(frame.rgb, profile, detection.state),
    }
    anchor_ids = profile.get("preset_1_anchors", [profile.get("preset_1_anchor")])
    observation["global_preset_number"] = None
    if profile.get("preset_mapping_verified") is True and detection.state in {"HOME", "GLOBAL_PRESET_1"}:
        visible = []
        for anchor_id in anchor_ids:
            if anchor_id == "global_preset_1_badge" and not (
                observation["quickslot_page"] == 4 and observation["anchors"].get("global_preset_shortcut", {}).get("found")
            ):
                continue
            if anchor_id:
                visible.append(observation["anchors"].get(anchor_id, {}))
        if any(match.get("found") and not match.get("ambiguous") for match in visible):
            observation["preset"] = {
                "number": 1, "kind": "global", "verified_at": frame.captured_at,
                "window_identity": frame.window.identity, "invalidated": False,
                "context_epoch": context_epoch,
            }
            observation["global_preset_number"] = 1
    return observation
