"""One Mu Lung auto attempt, using the maximum owned tickets and no boosters."""
from .daily_ledger import DailyBlocked
from .runner_base import GuardedRunner, QuestStopped
from .text_facts import valid_ticket_selection


class MuLungRunner(GuardedRunner):
    running_state = 'MULUNG_RUNNING'
    clear_state = 'MULUNG_CLEAR'
    auto_anchor = 'mulung_auto_infinity'
    exit_anchor = 'mulung_result_exit'
    reward_status = 'manual_claims_untouched'

    def validate_clear(self, observation):
        floor = observation.get('facts', {}).get('clear_floor')
        if type(floor) is not int or floor <= 0:
            raise QuestStopped('Mu Lung clear floor could not be verified.')

    def run(self, *, live=False, prepare_only=False):
        cfg = self.config
        if (cfg.get('id'),cfg.get('implementation')) != ('D10','mulung-v1') or cfg.get('goal') != 'one_natural_auto_attempt' or cfg.get('rewards') != 'manual':
            raise QuestStopped('Mu Lung goal or reward policy is not the taught workflow.')
        self.deadline = self.clock()+cfg.get('timeout_seconds', 720)
        try:
            home = self.stable('HOME', ('character_name',))
            if live and cfg.get('execution_enabled') is not True:
                raise QuestStopped('Mu Lung execution is disabled.')
            if not prepare_only:
                try:
                    self.daily.check_available(cfg['scope'],cfg['character_id'],cfg['id'],cfg['reset'],self.utcnow())
                except DailyBlocked as blocked:
                    if blocked.status not in {'cleared','complete'}:
                        raise
                    if live and blocked.status=='cleared':
                        self.daily.transition(blocked.run_id,'complete',{'returned_home':home.get('frame_path'),'reconciled':True})
                    return {'status':'already_complete','activity':cfg['id'],'run_id':blocked.run_id,'input_sent':False}
            if not live:
                return {'status':'dry_run','activity':cfg['id'],'input_sent':False,
                        'next_action':'verify Preset 1, Mu Lung auto setting, maximum owned tickets and zero booster spend'}
            proof = self.verify_preset(home)
            catalogue = self.send({'type':'key','key':'F2'},proof,{'DUNGEONS'},operation='dungeons')
            info = self.send({'type':'click_anchor','target':'mulung_tile'},catalogue,{'MULUNG_INFO'},operation='mulung')
            if not info['anchors'].get('mulung_auto_on',{}).get('found'):
                raise QuestStopped('Mu Lung built-in auto is not visibly ON.')
            if prepare_only:
                self.send({'type':'click_anchor','target':'screen_close'},info,{'HOME'},operation='prepare_return')
                return {'status':'prepared','activity':cfg['id'],'entry_started':False,'tickets_spent':0}
            self.require_preset(info)
            self.send({'type':'click_anchor','target':'mulung_enter'},info,{'MULUNG_TICKETS'},operation='open_tickets')
            fields = ('owned_tickets','ticket_cap','selected_tickets','no_booster_spend_verified')
            ticket = self.stable('MULUNG_TICKETS',fields)
            if ticket['facts'].get('no_booster_spend_verified') is not True:
                raise QuestStopped('Zero booster spend could not be verified.')
            # Only the initial selection used in the pilot is taught. The Max
            # button has not been exercised, so a non-max default stops safely.
            if not valid_ticket_selection(ticket['facts']):
                raise QuestStopped('Maximum owned ticket selection is not verified; no confirmation sent.')
            self.require_preset(ticket)
            quantity = ticket['facts']['selected_tickets']
            self.run_id = self.daily.reserve(cfg['scope'],cfg['character_id'],cfg['id'],cfg['reset'],self.utcnow(),
                                             {'entry_screen':info.get('frame_path'),'ticket_screen':ticket.get('frame_path'),
                                              'tickets_selected':quantity,'points_used':0,'preset':self.preset})
            started = self.send({'type':'click_anchor','target':'mulung_ticket_confirm','consequential':'entry'},ticket,
                                {self.running_state,self.clear_state},operation='confirm_entry',timeout=30)
            self.daily.transition(self.run_id,'running',{'entry':started.get('frame_path'),'tickets_used':quantity})
            return self.finish(started,quantity)
        except BaseException as error:
            self.mark_uncertain(error)
            raise
