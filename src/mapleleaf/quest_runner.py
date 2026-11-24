"""Bounded single-quest supervisor. The game supplies all combat inputs.

The first taught implementation is Elite. Untaught activities never fall back
to an approximate script. Daily reservations and action intents precede input.
"""
from __future__ import annotations

from .daily_ledger import DailyBlocked
from .runner_base import GuardedRunner, QuestStopped
from .text_facts import valid_ticket_selection
from .runners import create_runner


class QuestRunner(GuardedRunner):
    running_state = 'ELITE_RUNNING'
    clear_state = 'ELITE_CLEAR'
    auto_anchor = 'elite_auto_infinity'
    exit_anchor = 'elite_result_exit'
    reward_status = 'mailbox_unclaimed'

    def run(self, *, live=False, prepare_only=False):
        cfg=self.config
        if cfg.get('id')!='D03' or cfg.get('implementation')!='elite-v1':
            raise QuestStopped('This activity has not been taught.')
        self.deadline=self.clock()+cfg.get('timeout_seconds',900)
        try:
            home=self.stable('HOME',('character_name',))
            if live and cfg.get('execution_enabled') is not True:
                raise QuestStopped('Quest execution is disabled.')
            # Explicit preparation exercises menus but cannot reserve/spend tickets.
            if not prepare_only:
                try:
                    self.daily.check_available(cfg['scope'],cfg['character_id'],cfg['id'],cfg['reset'],self.utcnow())
                except DailyBlocked as blocked:
                    if blocked.status not in {'cleared','complete'}:
                        raise
                    if live and blocked.status=='cleared':
                        self.daily.transition(blocked.run_id,'complete',{'returned_home':home.get('frame_path'),'reconciled':True})
                    return {'status':'already_complete','activity':cfg['id'],'character':cfg['character_id'],
                            'run_id':blocked.run_id,'input_sent':False,'reason':'One clear is already recorded for this cycle.'}
            if not live:
                return {'status':'dry_run','character':cfg['character_id'],'activity':cfg['id'],
                        'next_action':'verify global Preset 1, then open Dungeons','input_sent':False}
            proof = self.verify_preset(home)
            self.send({'type':'key','key':'F2'},proof,{'DUNGEONS'},operation='dungeons')
            catalogue=self.stable('DUNGEONS')
            self.send({'type':'click_anchor','target':'elite_tile'},catalogue,{'ELITE_INFO'},operation='elite')
            info=self.stable('ELITE_INFO',('stage_name','current_power','required_power'))
            facts=info['facts']
            if facts.get('stage_name') != cfg['stage_name'] or not all(type(facts.get(k)) is int for k in ('current_power','required_power')):
                raise QuestStopped('The taught stage and combat power could not be read.')
            if facts['current_power'] < facts['required_power']:
                raise QuestStopped('Character does not meet the entry requirement.')
            if not info['anchors'].get('elite_auto_setting_on',{}).get('found'):
                raise QuestStopped('Built-in Auto Battle is not visibly ON.')
            if prepare_only:
                self.send({'type':'click_anchor','target':'screen_close'},info,{'HOME'},operation='prepare_return')
                return {'status':'prepared','character':cfg['character_id'],'activity':cfg['id'],
                        'stage':facts['stage_name'],'entry_started':False,'tickets_spent':0}
            self.require_preset(info)
            # This tested Enter control only opens the multiplier dialog. The
            # daily budget is reserved just before its consuming Confirm action.
            self.send({'type':'click_anchor','target':'elite_enter'},info,{'ELITE_TICKETS'},operation='open_tickets')
            ticket=self.stable('ELITE_TICKETS',('owned_tickets','ticket_cap','selected_tickets','multiplier_label'))
            if not valid_ticket_selection(ticket['facts']):
                f=ticket['facts']
                if not (f.get('multiplier_label') is True and all(type(f.get(k)) is int and f[k]>0 for k in ('owned_tickets','ticket_cap','selected_tickets'))):
                    raise QuestStopped('Ticket quantity is missing, zero or unreadable.')
                self.send({'type':'click_anchor','target':'elite_ticket_max'},ticket,{'ELITE_TICKETS'},operation='max_tickets')
                ticket=self.stable('ELITE_TICKETS',('owned_tickets','ticket_cap','selected_tickets','multiplier_label'))
                if not valid_ticket_selection(ticket['facts']):
                    raise QuestStopped('Maximum ticket selection could not be verified.')
            quantity=ticket['facts']['selected_tickets']
            self.require_preset(ticket)
            self.run_id=self.daily.reserve(cfg['scope'],cfg['character_id'],cfg['id'],cfg['reset'],self.utcnow(),
                                           {'entry_screen':info.get('frame_path'),'ticket_screen':ticket.get('frame_path'),
                                            'tickets_selected':quantity,'preset':self.preset})
            started=self.send({'type':'click_anchor','target':'elite_ticket_confirm','consequential':'entry'},ticket,
                              {'ELITE_RUNNING','ELITE_CLEAR'},operation='confirm_entry',timeout=25)
            self.daily.transition(self.run_id,'running',{'entry':started.get('frame_path'),'tickets_used':quantity})
            return self.finish(started,quantity)
        except BaseException as exc:
            self.mark_uncertain(exc)
            raise
