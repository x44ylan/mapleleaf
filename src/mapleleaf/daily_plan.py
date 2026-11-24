"""Validate the whole daily plan before opening a control session."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .daily_ledger import cycle_bounds
from .runners import DAILY_IMPLEMENTATIONS


COUNTED_ACTIVITIES = frozenset({'D02', 'D03', 'D07', 'D09', 'D10'})


def validate_plan(plan):
    if not isinstance(plan, dict) or type(plan.get('schema_version')) is not int or plan['schema_version'] != 1:
        raise ValueError('Daily plan schema is unsupported.')
    if plan.get('reward_collection') != 'manual':
        raise ValueError('Daily plan rewards must remain manual.')
    if not isinstance(plan.get('character_id'), str) or not plan['character_id'].strip():
        raise ValueError('A daily plan needs a character identity.')
    if plan.get('completion_policy') not in {None, 'fresh_remaining_entries'}:
        raise ValueError('Unknown daily completion policy.')
    activities = plan.get('activities')
    if not isinstance(activities, list) or not activities:
        raise ValueError('A daily plan needs an activity list.')
    seen = set()
    for activity in activities:
        if not isinstance(activity, dict):
            raise ValueError('Each daily activity must be an object.')
        for field in ('id', 'name'):
            if not isinstance(activity.get(field), str) or not activity[field].strip():
                raise ValueError('Each daily activity needs an ID and name.')
        if activity['id'] in seen:
            raise ValueError('Daily activities must not be duplicated.')
        seen.add(activity['id'])
        if type(activity.get('enabled')) is not bool:
            raise ValueError('Activity enabled must be a boolean.')
        if activity.get('config') is not None and (not isinstance(activity['config'], str) or not activity['config'].strip()):
            raise ValueError('Activity config must be a path or null.')
        if plan.get('completion_policy') == 'fresh_remaining_entries' and activity['enabled']:
            expected = activity['id'] in COUNTED_ACTIVITIES
            if activity.get('remaining_counter', False) is not expected:
                raise ValueError('Remaining-counter policy does not match activity '+activity['id'])
        scope = activity.get('completion_scope')
        if scope:
            cycle_bounds(datetime.now(timezone.utc), plan.get('reset_policies', {}).get(scope))


def project_file(root, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('A project-relative file path is required.')
    root = Path(root).resolve()
    relative = Path(value)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Missing file or path outside the project: '+value)
    return path


def load_daily_plan(path, root):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    validate_plan(plan)
    if plan.get('completion_policy') != 'fresh_remaining_entries':
        raise ValueError('The daily CLI requires fresh remaining-entry completion checks.')
    if not isinstance(plan.get('character_name'), str) or not plan['character_name'].strip():
        raise ValueError('The plan needs an exact character name.')

    def load_config(value):
        config = json.loads(project_file(root, value).read_text(encoding='utf-8'))
        if (config.get('character_id'), config.get('character_name')) != (plan['character_id'], plan['character_name']):
            raise ValueError('Quest configuration does not match the selected character.')
        if config.get('rewards') != 'manual':
            raise ValueError('Quest rewards must remain manual.')
        project_file(root, config.get('profile'))
        return config

    inspection = load_config(plan.get('inspection_config'))
    configs = {}
    for activity in plan['activities']:
        if not activity['enabled'] or not activity.get('config'):
            continue
        config = load_config(activity['config'])
        if config.get('id') != activity['id'] or config.get('implementation') != DAILY_IMPLEMENTATIONS.get(activity['id']):
            raise ValueError('Unsupported daily entry implementation for '+activity['id'])
        if config.get('execution_enabled') is not True:
            raise ValueError('Selected quest execution is disabled: '+activity['id'])
        duration = config.get('timeout_seconds')
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('Quest timeout must be positive and finite.')
        if activity['id'] in {'D03', 'D10'}:
            scope = activity.get('completion_scope')
            reset = plan.get('reset_policies', {}).get(scope)
            if not scope or config.get('scope') != scope:
                raise ValueError('Quest and daily ledger scope disagree.')
            cycle_bounds(datetime.now(timezone.utc), config.get('reset'))
            if any(config['reset'].get(k) != reset.get(k) for k in ('hour', 'minute', 'utc_offset_minutes')):
                raise ValueError('Quest and daily reset policies disagree.')
        configs[activity['id']] = config
    return plan, inspection, configs
