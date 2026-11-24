"""Read labelled catalogue entry counters; unknown pixels never mean zero."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone
import json

import cv2
import numpy as np
from PIL import Image

from .vision import VisionDetector
from .daily_ledger import cycle_bounds
from .paths import ROOT


class RemainingSnapshot:
    """In-memory evidence only; never load last run's counters as live state."""
    def __init__(self, plan, inspect, *, utcnow=lambda: datetime.now(timezone.utc)):
        self.plan, self.inspect, self.utcnow = plan, inspect, utcnow
        self.snapshot = None

    def _current(self, activity):
        snapshot = self.snapshot
        if not isinstance(snapshot, dict):
            return False
        if (snapshot.get('character_id'), snapshot.get('character_name'), snapshot.get('returned_home')) != (
                self.plan['character_id'], self.plan['character_name'], True):
            return False
        counter = snapshot.get('activities', {}).get(activity['id'])
        if not isinstance(counter, dict) or counter.get('activity') != activity['id']:
            return False
        try:
            observed = datetime.fromisoformat(counter['captured_utc'].replace('Z', '+00:00'))
            now = self.utcnow()
            if observed.tzinfo is None or not 0 <= (now-observed).total_seconds() <= 60:
                return False
            scope = activity.get('completion_scope')
            if scope:
                reset = self.plan['reset_policies'][scope]
                if cycle_bounds(observed, reset) != cycle_bounds(now, reset):
                    return False
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        return True

    def get(self, activity, refresh=False):
        if refresh or not self._current(activity):
            self.snapshot = self.inspect()
        # A slow, malformed or wrong-character refresh is not trusted either.
        if not self._current(activity):
            raise ValueError('Remaining-entry inspection is stale, incomplete or belongs to another character.')
        return deepcopy(self.snapshot['activities'][activity['id']])


def number_ink(rgb, mode='white'):
    if mode not in {'white', 'dark'}:
        raise ValueError('Unknown remaining-counter ink mode.')
    low, high = rgb.min(axis=2), rgb.max(axis=2)
    mask = (((low >= 235) & (high-low <= 15)) if mode == 'white' else high < 160).astype(np.uint8)
    ys, xs = np.nonzero(mask)
    if not len(xs) or xs.min() <= 1 or ys.min() <= 1 or xs.max() >= mask.shape[1]-2 or ys.max() >= mask.shape[0]-2:
        return None
    return mask[ys.min():ys.max()+1, xs.min():xs.max()+1]


def ink_similarity(a, b):
    """Full-field intersection/union, allowing translation but no dropped suffix."""
    height, width = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
    canvas = np.zeros((height+4, width+4), np.float32)
    canvas[2:2+a.shape[0], 2:2+a.shape[1]] = a
    intersections = cv2.matchTemplate(canvas, b.astype(np.float32), cv2.TM_CCORR)
    overlap = max(0.0, float(intersections.max()))
    union = float(a.sum()+b.sum())-overlap
    return overlap/union if union else 0.0


class CatalogueCounters:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.detector = VisionDetector(self.root/'config/remaining-catalogue.json')
        self.spec = json.loads((self.root/'config/remaining-fields.json').read_text(encoding='utf-8'))
        if type(self.spec.get('schema_version')) is not int or self.spec['schema_version'] != 1:
            raise ValueError('Remaining-counter schema is unsupported.')
        self.templates = []
        for item in self.spec['numbers']:
            if type(item.get('value')) is not int or item['value'] < 0:
                raise ValueError('Count templates require nonnegative integer values.')
            rgb = np.asarray(Image.open(self.root/item['image']).convert('RGB'))
            mode = item.get('mode', 'white')
            ink = number_ink(rgb, mode)
            if ink is None:
                raise ValueError('Invalid count-number template: '+item['image'])
            self.templates.append((item['value'], ink, mode))

    def number(self, rgb, mode='white'):
        ink = number_ink(rgb, mode)
        if ink is None:
            return None
        scores = {}
        for value, template, template_mode in self.templates:
            if template_mode != mode:
                continue
            scores[value] = max(scores.get(value, 0), ink_similarity(ink, template))
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if not ranked or ranked[0][1] < .95 or len(ranked)>1 and ranked[0][1]-ranked[1][1]<.03:
            return None
        return ranked[0][0]

    def read(self, rgb):
        detection = self.detector.detect(rgb)
        results = {}
        for activity, spec in self.spec['activities'].items():
            item = {'activity': activity, 'name': spec['name'], 'verified': False,
                    'remaining_entries': None, 'daily_done': None}
            match = detection.anchors[spec['anchor']]
            if detection.state != 'DUNGEONS' or detection.blocked or not match.found:
                item['reason'] = 'Catalogue or activity title is not verified in this frame.'
                results[activity] = item
                continue
            x, y, _, _ = match.bbox
            clipped = False
            for field in ('free_remaining', 'owned_remaining'):
                l,t,r,b = spec[field]
                l,t,r,b = x+l,y+t,x+r,y+b
                if not (0 <= l < r <= rgb.shape[1] and 0 <= t < b <= rgb.shape[0]):
                    clipped = True
                    item[field] = None
                else:
                    item[field] = self.number(rgb[t:b,l:r])
                item[field+'_region'] = [l,t,r,b]
            if all(type(item.get(k)) is int for k in ('free_remaining','owned_remaining')):
                item['verified'] = True
                item['remaining_entries'] = item['free_remaining'] + item['owned_remaining']
                item['daily_done'] = item['remaining_entries'] == 0
                item['reason'] = 'Zero remaining entries.' if item['daily_done'] else 'Entries remain; daily is not done.'
            else:
                item['reason'] = 'Counter is clipped.' if clipped else 'Counter is unreadable; it is not treated as zero.'
            results[activity] = item
        return results

    def read_dimension_detail(self, rgb):
        item = {'activity':'D09', 'name':'Dimension Invasion', 'verified':False,
                'remaining_entries':None, 'daily_done':None}
        if self.detector.detect(rgb).state != 'DIMENSION_DETAIL':
            return {**item, 'reason':'Dimension remaining-ticket labels are not verified.'}
        for field in ('free_remaining','owned_remaining'):
            l,t,r,b = self.spec['dimension_detail'][field]
            item[field] = self.number(rgb[t:b,l:r], 'dark')
            item[field+'_region'] = [l,t,r,b]
        if all(type(item.get(k)) is int for k in ('free_remaining','owned_remaining')):
            item['verified'] = True
            item['remaining_entries'] = item['free_remaining'] + item['owned_remaining']
            item['daily_done'] = item['remaining_entries'] == 0
        item['reason'] = ('Zero remaining entries.' if item['daily_done'] else 'Entries remain; daily is not done.') if item['verified'] else 'Counter unreadable; not treated as zero.'
        return item


def reconcile_remaining(outcome, counter):
    """Game counts decide daily status; history still protects against repeat input."""
    result = dict(outcome)
    result['remaining_counter'] = counter
    if not counter or counter.get('verified') is not True or type(counter.get('remaining_entries')) is not int:
        result['daily_done'] = None
        if result.get('status') in {'complete','already_complete'}:
            result.update(status='completion_unverified', reason='Fresh remaining-entry count is not verified.')
    elif counter['remaining_entries'] > 0:
        result['daily_done'] = False
        if result.get('status') in {'complete','already_complete'}:
            result.update(status='not_done', reason='The game still shows remaining entries despite a recorded clear.',
                          recorded_clear=True, entry_budget_requires_reconciliation=True)
    elif counter['remaining_entries'] == 0:
        result['daily_done'] = True
        # An unresolved attempt cannot be erased by tickets already being spent.
        if result.get('status') in {'complete','already_complete','needs_definition'}:
            result.update(status='already_complete', completion_source='game_remaining_entries',
                          reason='The game shows zero remaining entries.', input_sent=result.get('input_sent',False))
    else:
        raise ValueError('Remaining entries cannot be negative.')
    return result


class RemainingInspector:
    """Use the same guarded navigation runner; inspection cannot reserve an entry."""
    def __init__(self, config, io, daily, actions, **kwargs):
        from .runner_base import GuardedRunner
        self.runner = GuardedRunner(config, io, daily, actions, **kwargs)

    def scan(self):
        r = self.runner
        r.deadline = r.clock()+60
        home = r.stable('HOME', ('character_name',))
        proof = r.send({'type':'key','key':'GLOBAL_PRESET_1'},home,{'HOME'},operation='remaining_preset',timeout=5,toast=True)
        r.send({'type':'key','key':'F2'},proof,{'DUNGEONS'},operation='remaining_catalogue')
        catalogue = r.stable('DUNGEONS', ('remaining_entries',))
        results = catalogue['facts']['remaining_entries']
        for item in results.values():
            item.update(frame_path=catalogue['frame_path'],captured_utc=catalogue['captured_utc'])
        if not results['D09']['verified']:
            r.send({'type':'click_anchor','target':'dimension_tile'},catalogue,
                   {'DIMENSION_INFO','DIMENSION_UNAVAILABLE'},operation='remaining_dimension')
            state = r.last['state']
            detail = r.stable(state, ('remaining_entries',))
            results['D09'] = {**detail['facts']['remaining_entries']['D09'],
                              'frame_path':detail['frame_path'],'captured_utc':detail['captured_utc']}
            last = detail
        else:
            last = catalogue
        returned = r.send({'type':'click_anchor','target':'screen_close'},last,{'HOME'},operation='remaining_return')
        return {'character_id':r.config['character_id'],'character_name':returned['facts']['character_name'],
                'activities':results,'returned_home':True,'home_evidence':returned['frame_path'],
                'activity_started':False,'rewards_claimed':False}
