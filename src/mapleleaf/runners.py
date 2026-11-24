"""The explicit allowlist shared by preflight validation and runner dispatch."""
from importlib import import_module
from types import MappingProxyType

from .runner_base import QuestStopped

# Recovery is registered for explicit resume, but never for daily entry.
IMPLEMENTATIONS = MappingProxyType({
    ('D03', 'elite-v1'): ('quest_runner', 'QuestRunner', True),
    ('D10', 'mulung-v1'): ('mulung', 'MuLungRunner', True),
    ('D15', 'tasks-status-v1'): ('task_status', 'TaskStatusRunner', True),
    ('D09', 'dimension-recovery-v1'): ('dimension', 'DimensionRecoveryRunner', False),
})
DAILY_IMPLEMENTATIONS = MappingProxyType({
    activity: implementation
    for (activity, implementation), (_, _, daily_enabled) in IMPLEMENTATIONS.items()
    if daily_enabled
})


def create_runner(config, *args, **kwargs):
    """Reject untaught pairs; never fall back to another activity's workflow."""
    target = IMPLEMENTATIONS.get((config.get('id'), config.get('implementation')))
    if target is None:
        raise QuestStopped('This activity has not been taught.')
    module, name, _ = target
    runner = getattr(import_module(f'.{module}', __package__), name)
    return runner(config, *args, **kwargs)
