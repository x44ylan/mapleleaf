"""Shared guarded observation, dispatch and attempt reconciliation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import time

from .daily_ledger import DailyLedger
from .engine import ActionGuard, RunLedger


class QuestStopped(RuntimeError):
    pass


class GuardedRunner:
    """Common controls; activity subclasses supply their state and result anchors."""
    running_state = None
    clear_state = None
    auto_anchor = None
    exit_anchor = None
    reward_status = None

    def __init__(self, config: dict, io, daily: DailyLedger, actions: RunLedger, *,
                 monotonic=time.monotonic, utcnow=lambda: datetime.now(timezone.utc)):
        self.config, self.io, self.daily, self.actions = deepcopy(config), io, daily, actions
        self.clock, self.utcnow = monotonic, utcnow
        self.guard = None
        self.preset = None
        self.run_id = None
        self.last = None
        self.stopped = False
        self.deadline = None

    def stop(self):
        self.stopped = True
        self.preset = None
        if self.guard:
            self.guard.stop()

    def observe(self):
        if self.stopped or (self.deadline is not None and self.clock() >= self.deadline):
            raise QuestStopped('Stopped or maximum quest runtime reached.')
        observation = self.io.observe()
        self.last = observation
        window = observation.get('window', {})
        if not window.get('foreground') or window.get('minimized'):
            raise QuestStopped('Game lost foreground or became minimized.')
        if (window.get('client_width'),window.get('client_height'),window.get('dpi')) != (1920,1080,144):
            raise QuestStopped('Game resolution or scaling changed.')
        if self.guard is None:
            self.guard=ActionGuard(self.actions,window['identity'],clock=self.clock,wall_clock=lambda:self.utcnow().timestamp())
        if window['identity'] != self.guard.expected_window_identity:
            raise QuestStopped('Game window identity changed.')
        name=observation.get('facts',{}).get('character_name')
        if observation['state'] in {'HOME','ELITE_RUNNING','ELITE_CLEAR','DUNGEON_QUEUE','DIMENSION_RUNNING','DIMENSION_CLEAR','MULUNG_RUNNING','MULUNG_CLEAR'} and name != self.config['character_name']:
            raise QuestStopped('Current character could not be verified.')
        if self.preset and observation['context_epoch'] != self.preset['context_epoch']:
            self.preset=None
            raise QuestStopped('Character/profile context changed.')
        return observation

    def wait_for(self, states, timeout=12, *, toast=False):
        end=min(self.clock()+timeout,self.deadline or float('inf'))
        while self.clock()<end:
            observation=self.observe()
            if observation['state'] in states and not observation.get('blocked'):
                if not toast or observation.get('facts',{}).get('preset_one_toast') is True:
                    return observation
            # Only wait during a transition; never click in UNKNOWN.
            self.io.sleep(.25)
        raise QuestStopped('Expected screen did not appear: '+', '.join(states))

    def stable(self, state, fields=()):
        first=self.wait_for({state})
        self.io.sleep(.15)
        second=self.observe()
        if second['state']!=state or second.get('blocked'):
            raise QuestStopped('Screen changed while checking readiness.')
        for field in fields:
            if first.get('facts',{}).get(field) != second.get('facts',{}).get(field):
                raise QuestStopped('Observed value changed while checking '+field)
        return second

    def send(self, action, observation, expected, *, operation, timeout=12, toast=False):
        action={**action,'state':observation['state'],'execution_enabled':True,'pointer_mode':'settled'}
        if action['type']=='key':
            action['park_pointer_before']=True
        if action.get('consequential'):
            action['dedup_key']=self.run_id+':'+operation
            if self.preset:
                observation={**observation,'preset':deepcopy(self.preset)}
        if action['type']=='click_anchor':
            match=observation['anchors'].get(action['target'],{})
            if not match.get('found') or match.get('ambiguous'):
                raise QuestStopped('Target is not verified: '+action['target'])
            x,y,w,h=match['bbox']
            action['point']=[round(x+w/2),round(y+h/2)]
        decision=self.guard.authorize(action,observation,live_authorized=True,dry_run=False)
        if not decision.allowed:
            raise QuestStopped(decision.reason)
        try:
            self.io.send(action,observation)
            self.io.sleep(.2)
            result=self.wait_for(set(expected),timeout,toast=toast)
            self.actions.action_outcome(decision.intent_id,'confirmed',{'frame':result.get('frame_path'),'state':result['state']})
            return result
        except BaseException as exc:
            self.actions.action_outcome(decision.intent_id,'uncertain',{'error':str(exc),'last_frame':(self.last or {}).get('frame_path')})
            raise

    def require_preset(self, observation):
        if not self.preset or self.preset['context_epoch'] != observation['context_epoch'] or not 0 <= self.clock()-self.preset['verified_at'] <= 30:
            raise QuestStopped('Fresh Preset 1 proof is required before entry.')

    def verify_preset(self, home):
        """Record only the freshly observed global Preset 1 toast."""
        proof = self.send({'type': 'key', 'key': 'GLOBAL_PRESET_1'}, home, {'HOME'},
                          operation='preset', timeout=5, toast=True)
        self.preset = {
            'number': 1, 'kind': 'global', 'invalidated': False,
            'verified_at': proof['captured_at'],
            'window_identity': proof['window']['identity'],
            'context_epoch': proof['context_epoch'],
        }
        return proof

    def mark_uncertain(self, error):
        """Preserve an interrupted entry without downgrading a recorded clear."""
        self.preset = None
        if self.run_id and self.daily.get(self.run_id)['status'] in {'reserved', 'running'}:
            self.daily.transition(self.run_id, 'uncertain', {
                'error': str(error), 'last_frame': (self.last or {}).get('frame_path'),
            })

    def finish(self, started, quantity=None):
        # No combat or Fever inputs. A room-loading transition may be observed
        # briefly, but unknown UI never causes an input or another entry.
        if started['state']!=self.clear_state and not started['anchors'].get(self.auto_anchor,{}).get('found'):
            raise QuestStopped('In-game automatic battle indicator is not verified.')
        while started['state']!=self.clear_state:
            self.io.sleep(self.config.get('poll_seconds',10))
            started=self.observe()
            if started['state']=='UNKNOWN':
                started=self.wait_for({self.running_state,self.clear_state},timeout=12)
            if started['state'] not in {self.running_state,self.clear_state} or started.get('blocked'):
                raise QuestStopped('Unexpected screen during the activity.')
        clear=self.stable(self.clear_state,('character_name',))
        self.validate_clear(clear)
        if self.daily.get(self.run_id)['status']!='cleared':
            self.daily.transition(self.run_id,'cleared',{'clear':clear.get('frame_path'),'rewards':self.reward_status,'facts':clear.get('facts',{})})
        returned=self.send({'type':'click_anchor','target':self.exit_anchor},clear,{'HOME'},operation='exit')
        self.daily.transition(self.run_id,'complete',{'returned_home':returned.get('frame_path')})
        return {'status':'complete','run_id':self.run_id,'activity':self.config['id'],'character':self.config['character_id'],
                'tickets_used':quantity,'reward_status':self.reward_status,'returned_home':True,
                'clear_facts':clear.get('facts',{})}

    def validate_clear(self, observation):
        """Activity-specific result fields can strengthen the common clear guard."""

    def resume(self, *, live=False):
        """Reconcile one interrupted activity from fresh evidence; never re-enter."""
        cfg=self.config
        self.deadline=self.clock()+cfg.get('timeout_seconds',900)
        candidates=[r for r in self.daily.summary(cfg['scope'],cfg['character_id'])
                    if r['activity']==cfg['id'] and r['status'] in {'reserved','running','uncertain','cleared'}]
        if len(candidates)!=1:
            raise QuestStopped('Resume needs exactly one unresolved activity record.')
        current=self.observe()
        if not live:
            return {'status':'dry_run','resume_run_id':candidates[0]['id'],'state':current['state'],'input_sent':False}
        if cfg.get('execution_enabled') is not True or current.get('blocked'):
            raise QuestStopped('Resume is disabled or current state is unverified.')
        self.run_id=candidates[0]['id']
        status=candidates[0]['status']
        if current['state']=='HOME' and status=='cleared':
            self.daily.transition(self.run_id,'complete',{'returned_home':current.get('frame_path'),'reconciled':True})
            return {'status':'complete','run_id':self.run_id,'returned_home':True,'input_sent':False}
        if status=='cleared' and current['state']!=self.clear_state:
            raise QuestStopped('A previously cleared activity is now on an unexpected screen.')
        if current['state'] not in {self.running_state,self.clear_state}:
            raise QuestStopped('Cannot reconcile activity completion from this screen; no entry will be repeated.')
        if status in {'reserved','uncertain'}:
            self.daily.transition(self.run_id,'running',{'reconciled':current.get('frame_path')})
        return self.finish(current)
