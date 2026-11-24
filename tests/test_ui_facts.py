import numpy as np
from mapleleaf.ui_facts import quickslot_page


PROFILE={"client_size":[100,20],"quickslot_page_markers":[[10,10],[30,10],[50,10],[70,10]]}


def frame(page):
    rgb=np.zeros((20,100,3),dtype=np.uint8)
    for index,(x,y) in enumerate(PROFILE["quickslot_page_markers"],1):
        rgb[y-2:y+3,x-2:x+3]=[255,123,80] if index==page else [80,79,79]
    return rgb


def test_four_pages_and_non_home():
    for page in range(1,5):
        assert quickslot_page(frame(page),PROFILE,"HOME")==page
        assert quickslot_page(frame(page),PROFILE,"MAIN_MENU") is None


def test_rejects_dimmed_missing_or_multiple_lights():
    rgb=frame(1)
    assert quickslot_page((rgb*.5).astype(np.uint8),PROFILE,"HOME") is None
    assert quickslot_page(frame(0),PROFILE,"HOME") is None
    rgb[8:13,28:33]=[255,123,80]
    assert quickslot_page(rgb,PROFILE,"HOME") is None
    assert quickslot_page(rgb[:10],PROFILE,"HOME") is None
