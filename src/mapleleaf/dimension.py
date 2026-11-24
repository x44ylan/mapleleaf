"""Resume an existing Dimension attempt; entry and hidden-stage control are untaught."""
from .runner_base import GuardedRunner, QuestStopped


class DimensionRecoveryRunner(GuardedRunner):
    running_state = 'DIMENSION_RUNNING'
    clear_state = 'DIMENSION_CLEAR'
    exit_anchor = 'dimension_exit'
    reward_status = 'mailbox_unclaimed'

    def run(self, *, live=False, prepare_only=False):
        # Never silently turn a recovery configuration into an entry workflow.
        raise QuestStopped('Dimension entry is not taught. Recovery requires --resume and an existing attempt.')

    def resume(self, *, live=False):
        cfg = self.config
        if ((cfg.get('id'), cfg.get('implementation')) != ('D09', 'dimension-recovery-v1')
                or cfg.get('entry_enabled') is not False or cfg.get('rewards') != 'manual'):
            raise QuestStopped('Dimension recovery policy is not the taught workflow.')
        try:
            return super().resume(live=live)
        except BaseException as error:
            self.mark_uncertain(error)
            raise

    def finish(self, started, quantity=None):
        # HP zero and the auto-ended toast were followed by later normal stages
        # in the pilot. Observe only: neither proves clear nor authorizes revival.
        while started['state'] != self.clear_state:
            if started['state'] != self.running_state or started.get('blocked'):
                raise QuestStopped('Untaught Dimension screen; no recovery input sent.')
            self.io.sleep(self.config.get('poll_seconds', 10))
            started = self.observe()
            if started['state'] == 'UNKNOWN':
                started = self.wait_for({self.running_state, self.clear_state}, timeout=12)
        return super().finish(started, quantity)

    def validate_clear(self, observation):
        facts = observation.get('facts', {})
        if facts.get('stage_kind') != 'hidden' or type(facts.get('stage_number')) is not int or facts['stage_number'] != 2:
            raise QuestStopped('The taught Dimension Hidden Stage 2 result is not verified.')
