"""Read the taught Daily Task page without claiming or starting its objectives."""
from __future__ import annotations

import re

from .runner_base import GuardedRunner, QuestStopped
from .text_facts import integer, region_text


# Each value must be tied to its own exact label; reordered rows fail closed.
ROWS = (
    ('guild_attendance', 'Participate in Guild Attendance', 360, 280),
    ('elite', 'Clear an Elite Dungeon', 1145, 280),
    ('daily_dungeon', 'Clear a Daily Dungeon', 360, 475),
    ('dimension_reward', 'Dimension Invasion Normal Stage Reward Received', 1145, 475),
    ('auto_battle_hunt', 'Hunt monsters with auto-battle', 360, 670),
    ('boss', 'Clear a Boss', 1145, 670),
)


def read_tasks(document: dict) -> dict:
    rows = []
    for row_id, label, x, y in ROWS:
        seen_label = region_text(document, [x, y, x+350, y+90])
        counter = region_text(document, [x, y+115, x+210, y+158])
        action = region_text(document, [x+510, y+45, x+735, y+115])
        match = re.fullmatch(r'([0-9,]+)\s*/\s*([0-9,]+)', counter)
        done = integer(match[1]) if match else None
        total = integer(match[2]) if match else None
        valid = seen_label == label and done is not None and total is not None and total > 0
        rows.append({'id': row_id, 'label': seen_label, 'counter_text': counter,
                     'current': done if valid else None, 'required': total if valid else None,
                     'remaining_required': max(0,total-done) if valid else None,
                     'requirement_met': (done >= total) if valid else None,
                     'visible_action': action, 'claim_visible': action == 'Claim',
                     'verified': valid})
    return {'rows': rows, 'all_rows_verified': all(r['verified'] for r in rows),
            # Footer semantics have not been demonstrated; never use it as a clear count.
            'footer_text': region_text(document, [330, 890, 550, 942]),
            'reward_policy': 'manual', 'completion_source': 'individual labelled counters'}


class TaskStatusRunner(GuardedRunner):
    def read_stable_tasks(self):
        # The preset toast can briefly cover the upper rows after opening F3.
        # Wait for two complete matching reads; don't relax label verification.
        end = self.clock()+8
        previous = None
        while self.clock()<end:
            current = self.observe()
            if current['state']!='DAILY_TASKS' or current.get('blocked'):
                raise QuestStopped('Tasks screen changed while reading its rows.')
            facts = current.get('facts', {}).get('daily_tasks', {})
            if facts.get('all_rows_verified') is True:
                if facts==previous:
                    return current
                previous = facts
            else:
                previous = None
            self.io.sleep(.25)
        raise QuestStopped('Task rows could not all be read; no objective or claim was clicked.')

    def run(self, *, live=False, prepare_only=False):
        self.deadline = self.clock()+60
        home = self.stable('HOME', ('character_name',))
        if not live:
            return {'status': 'dry_run', 'activity': 'D15', 'input_sent': False,
                    'next_action': 'verify Preset 1, inspect Daily Task counters, return home'}
        if self.config.get('execution_enabled') is not True:
            raise QuestStopped('Task inspection is disabled.')
        proof = self.send({'type': 'key', 'key': 'GLOBAL_PRESET_1'}, home, {'HOME'},
                          operation='tasks_preset', toast=True, timeout=5)
        self.send({'type': 'key', 'key': 'F3'}, proof, {'DAILY_TASKS'}, operation='tasks_open')
        tasks = self.read_stable_tasks()
        status = tasks.get('facts', {}).get('daily_tasks', {})
        if status.get('all_rows_verified') is not True:
            raise QuestStopped('Task rows could not all be read; no objective or claim was clicked.')
        self.send({'type': 'click_anchor', 'target': 'screen_close'}, tasks, {'HOME'}, operation='tasks_close')
        return {'status': 'needs_definition', 'activity': 'D15', 'character': self.config['character_id'],
                'reason': 'Task progress observed; which extra requirements to automate is still undefined.',
                'task_progress': status, 'returned_home': True, 'inspection_only': True,
                'activity_started': False, 'rewards_claimed': False,
                'task_evidence': tasks.get('frame_path')}

    def resume(self, *, live=False):
        raise QuestStopped('Task status inspection has no activity to resume.')
