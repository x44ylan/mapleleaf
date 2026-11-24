"""Report interruptions without losing persisted completion evidence."""


def stopped_result(error, run_id=None, saved=None):
    cleared = bool(saved and saved['status'] in {'cleared', 'complete'})
    return {
        'status': 'stopped_after_clear' if cleared else 'stopped',
        'activity_complete': cleared,
        'run_id': run_id,
        'interrupted': isinstance(error, KeyboardInterrupt),
        'reason': str(error) or 'Operator interruption',
    }
