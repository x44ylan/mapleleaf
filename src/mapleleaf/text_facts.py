"""Strict, bounded text reads for variable values; no input or game attachment."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess


class WindowsTextReader:
    def __init__(self, script: Path | None = None):
        self.script = script or Path(__file__).resolve().parents[2] / 'scripts/read-text.ps1'

    def read(self, image: Path, *, identity_image: Path | None = None) -> dict:
        powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        command=[str(powershell), '-NoProfile', '-NonInteractive', '-File', str(self.script), '-ImagePath', str(image.resolve())]
        if identity_image is not None:
            command += ['-IdentityImagePath',str(identity_image.resolve())]
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding='utf-8', timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode:
            raise RuntimeError('Windows text recognition failed: '+result.stderr.strip()[:400])
        data = json.loads(result.stdout)
        if not isinstance(data.get('lines'), list):
            raise ValueError('OCR result is missing text lines.')
        if identity_image is not None and (not isinstance(data.get('identity'),dict) or not isinstance(data['identity'].get('lines'),list)):
            raise ValueError('OCR result is missing the requested identity field.')
        return data


IDENTITY_REGION = [108,10,350,49]
IDENTITY_IMAGE_SIZE = (968,156)


def prepare_dimmed_identity(rgb, output: Path) -> dict:
    """Isolate white HUD lettering in the entire taught name field, not a name-length crop."""
    import numpy as np
    from PIL import Image
    if rgb.shape != (1080,1920,3) or rgb.dtype != np.uint8:
        raise ValueError('Dimmed identity extraction requires the taught client frame.')
    left,top,right,bottom=IDENTITY_REGION
    field=rgb[top:bottom,left:right]
    low,high=field.min(axis=2),field.max(axis=2)
    neutral=(high-low)<=2
    if not neutral.any():
        raise ValueError('No neutral lettering could be isolated.')
    peak=int(low[neutral].max())
    mask=neutral & (low>=max(0,peak-2))
    Image.fromarray(np.where(mask,0,255).astype(np.uint8)).resize(IDENTITY_IMAGE_SIZE,Image.Resampling.NEAREST).save(output)
    return {'method':'neutral_white_peak_minus_two','region':IDENTITY_REGION.copy(),
            'peak':peak,'scale':4,'image':str(output)}


def identity_field_text(document: dict) -> str:
    if (document.get('image_width'),document.get('image_height')) != IDENTITY_IMAGE_SIZE:
        return ''
    entire=' '.join(document.get('text','').split())
    spatial=region_text(document,[0,0,*IDENTITY_IMAGE_SIZE])
    # Do not accept a prefix by silently dropping clipped or out-of-bounds words.
    return entire if entire and entire==spatial else ''


def region_text(document: dict, box: list[int]) -> str:
    """Only complete words inside the configured region; never guess O/0 or I/1."""
    left, top, right, bottom = box
    angle = document.get('text_angle')
    transform = None
    if angle is not None:
        if type(angle) not in (int,float) or not math.isfinite(angle) or abs(angle)>360:
            raise ValueError('OCR text angle is invalid.')
        if angle:
            width,height = document.get('image_width'),document.get('image_height')
            if any(type(v) is not int or v<=0 for v in (width,height)):
                raise ValueError('Rotated OCR coordinates require image dimensions.')
            # WinRT word boxes are deskewed. TextAngle rotates them clockwise
            # about the image centre to map them onto the original screenshot.
            # https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrresult.textangle
            radians=math.radians(angle)
            transform=(width/2,height/2,math.cos(radians),math.sin(radians))

    def inside(word):
        x,y,w,h = (word[k] for k in ('x','y','width','height'))
        if transform:
            cx,cy,c,s = transform
            corners=[(cx+c*(px-cx)-s*(py-cy),cy+s*(px-cx)+c*(py-cy))
                     for px,py in ((x,y),(x+w,y),(x,y+h),(x+w,y+h))]
            return all(left<=px<=right and top<=py<=bottom for px,py in corners)
        return left<=x and top<=y and x+w<=right and y+h<=bottom

    lines = []
    for line in document.get('lines', []):
        words = [word for word in line.get('words', [])
                 if inside(word)]
        if words:
            lines.append((min(w['y'] for w in words), min(w['x'] for w in words),
                          ' '.join(w['text'] for w in sorted(words, key=lambda w: w['x']))))
    return ' '.join(line[2] for line in sorted(lines))


def integer(text: str, *, maximum: int = 999999999) -> int | None:
    if not re.fullmatch(r'(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)', text):
        return None
    value = int(text.replace(',', ''))
    return value if value <= maximum else None


def read_facts(document: dict, state: str) -> dict:
    facts = {}
    if state == 'MULUNG_TICKETS':
        owned = region_text(document, [1180, 265, 1510, 320])
        terms = region_text(document, [1010, 440, 1560, 566])
        cap = re.search(r'You can use up to ([0-9]+) Tickets at once\.$', terms)
        return {
            'owned_tickets': integer(owned.removeprefix('Entry Tickets Owned: '), maximum=9999) if owned.startswith('Entry Tickets Owned: ') else None,
            'selected_tickets': integer(region_text(document, [1220, 350, 1360, 425]), maximum=99),
            'ticket_cap': integer(cap[1], maximum=99) if cap else None,
            'multiplier_label': region_text(document, [1110, 187, 1510, 230]) == 'Ticket Multiplier Reduction',
            'points_used_label': region_text(document, [438, 765, 620, 812]) == 'Points Used',
            'points_used': integer(region_text(document, [830, 765, 940, 812])),
        }
    if state == 'DAILY_TASKS':
        from .task_status import read_tasks
        return {'daily_tasks': read_tasks(document)}
    if state in {'HOME', 'ELITE_RUNNING', 'ELITE_CLEAR', 'DUNGEON_QUEUE', 'DIMENSION_RUNNING', 'DIMENSION_CLEAR', 'MULUNG_RUNNING', 'MULUNG_CLEAR'}:
        facts['character_name'] = region_text(document, [108, 10, 350, 49])
    if state == 'DIMENSION_CLEAR' and 'identity' in document:
        facts['character_name'] = identity_field_text(document['identity'])
        facts['character_name_source'] = 'full_fixed_field_neutral_white'
    if state in {'DIMENSION_RUNNING', 'DIMENSION_CLEAR'}:
        stage = region_text(document, [115, 285, 510, 350])
        match = re.fullmatch(r'(Stage|HiddenStage|Hidden Stage)(?: ([1-9][0-9]?))?', stage)
        facts['stage_text'] = stage
        facts['stage_kind'] = ('normal' if match[1] == 'Stage' else 'hidden') if match else None
        facts['stage_number'] = integer(match[2], maximum=99) if match and match[2] else None
        facts['hp'] = integer(region_text(document, [280, 48, 375, 80]))
        facts['time_remaining_text'] = region_text(document, [820, 120, 1100, 180])
        facts['auto_ended_toast'] = region_text(document, [790, 269, 1130, 313]) == 'Auto-battle has ended.'
    if state == 'MULUNG_RUNNING':
        facts['round'] = integer(region_text(document, [420, 285, 499, 340]), maximum=999)
        facts['time_remaining_text'] = region_text(document, [965, 130, 1080, 175])
    if state == 'MULUNG_CLEAR':
        floor = region_text(document, [1130, 311, 1270, 354])
        facts['clear_floor'] = integer(floor.removeprefix('Floor '), maximum=999) if floor.startswith('Floor ') else None
        facts['clear_time_text'] = region_text(document, [1165, 261, 1270, 303])
        facts['clear_points'] = integer(region_text(document, [1170, 408, 1270, 455]))
    if state in {'HOME', 'DUNGEON_QUEUE'}:
        # Exact text including the digit; never infer success from sending key 8.
        facts['preset_one_toast'] = region_text(document, [735, 275, 1200, 313]) == 'Changed to character setting 1.'
    if state == 'ELITE_INFO':
        facts['stage_name'] = re.sub(r'\s*-\s*', '-', region_text(document, [580, 150, 1230, 224]))
        facts['current_power'] = integer(region_text(document, [370, 177, 535, 235]))
        facts['required_power'] = integer(region_text(document, [961, 334, 1240, 385]))
    if state == 'ELITE_TICKETS':
        owned = region_text(document, [565, 593, 730, 635])
        facts['owned_tickets'] = integer(owned.removeprefix('Owned: '), maximum=9999) if owned.startswith('Owned: ') else None
        facts['ticket_cap'] = integer(region_text(document, [1090, 570, 1165, 615]), maximum=99)
        facts['selected_tickets'] = integer(region_text(document, [1007, 630, 1180, 687]), maximum=99)
        facts['multiplier_label'] = region_text(document, [730, 355, 1210, 411]) == 'Ticket Multiplier Reduction'
    return facts


def valid_ticket_selection(facts: dict) -> bool:
    keys = ('owned_tickets', 'ticket_cap', 'selected_tickets')
    if not all(type(facts.get(key)) is int and facts[key] > 0 for key in keys):
        return False
    return facts.get('multiplier_label') is True and facts['selected_tickets'] == min(facts['owned_tickets'], facts['ticket_cap'])


def ticket_template_facts(facts: dict, anchors: dict) -> dict:
    """Fill OCR omissions only for exact, reviewed whole-field templates.

    Windows OCR can omit a standalone digit. Other values remain unknown;
    conflicting OCR and template evidence invalidates the field.
    """
    facts = dict(facts)
    for field, anchor in [('selected_tickets','ticket_selected_3'), ('ticket_cap','ticket_cap_3')]:
        match = anchors.get(anchor, {})
        if match.get('found') is True and not match.get('ambiguous'):
            observed = facts.get(field)
            facts[field] = 3 if observed is None or type(observed) is int and observed == 3 else None
            facts[field+'_source'] = anchor if facts[field] == 3 else 'conflicting_evidence'
    return facts


def mulung_ticket_facts(facts: dict, anchors: dict) -> dict:
    """Confirm zero booster spend with a reviewed full-row image, never O/0 substitution."""
    facts = dict(facts)
    match = anchors.get('mulung_points_used_zero', {})
    zero = match.get('found') is True and not match.get('ambiguous')
    observed = facts.get('points_used')
    agrees = observed is None or type(observed) is int and observed == 0
    facts['no_booster_spend_verified'] = zero and agrees and facts.get('points_used_label') is True
    if facts['no_booster_spend_verified']:
        facts['points_used'] = 0
        facts['points_used_source'] = 'mulung_points_used_zero'
    return facts
