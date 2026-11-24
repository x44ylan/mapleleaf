"""Small deterministic facts from the fixed, visually verified PC layout."""
import numpy as np


def quickslot_page(rgb, profile, state):
    """Read all four page lights; reject anything except one orange + three grey.

    These tiny solid circles are better color probes than grayscale templates.
    Coordinates are client pixels in the profile's strictly fixed resolution.
    """
    markers = profile.get("quickslot_page_markers", [])
    size = profile.get("client_size", [])
    if state != "HOME" or len(markers) != 4 or len(size) != 2 or rgb.shape != (size[1], size[0], 3):
        return None
    values = []
    for x, y in markers:
        if not (2 <= x < size[0] - 2 and 2 <= y < size[1] - 2):
            return None
        value = np.median(rgb[y-2:y+3, x-2:x+3], axis=(0, 1))
        if np.max(np.abs(value - [255, 123, 80])) <= 10:
            values.append(True)
        elif np.max(np.abs(value - [80, 79, 79])) <= 10:
            values.append(False)
        else:
            return None
    return values.index(True) + 1 if values.count(True) == 1 else None
