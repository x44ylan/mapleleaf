"""Command failures retain clear evidence and avoid unnecessary attachment."""
import json

import pytest

from mapleleaf import runner_cli
from mapleleaf.results import stopped_result


@pytest.mark.parametrize('status', ['cleared', 'complete'])
def test_interruption_after_clear_keeps_completion(status):
    result = stopped_result(KeyboardInterrupt(), 'attempt-1', {'status': status})
    assert result['status'] == 'stopped_after_clear'
    assert result['activity_complete'] is True
    assert result['interrupted'] is True
    assert result['run_id'] == 'attempt-1'


@pytest.mark.parametrize('saved', [None, {'status': 'reserved'}, {'status': 'running'},
                                  {'status': 'uncertain'}])
def test_uncertain_entry_is_never_reported_as_complete(saved):
    result = stopped_result(RuntimeError('Transition unverified'), 'attempt-1', saved)
    assert result['status'] == 'stopped'
    assert result['activity_complete'] is False
    assert result['reason'] == 'Transition unverified'


def test_conflicting_modes_rejected_before_driver_attachment(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_cli, 'new_run', lambda *args: tmp_path)
    monkeypatch.setattr(runner_cli, 'NovaSession', lambda **kw: pytest.fail('Unexpected attachment'))
    assert runner_cli.main(['--resume', '--prepare-only']) == 2
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['status'] == 'stopped'
    assert result['reason'] == 'Choose resume or prepare-only, not both.'
