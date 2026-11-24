"""Execute selected, taught activities and report every remaining gap honestly."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .capture import NovaSession
from .daily_ledger import DailyLedger, DailyBlocked
from .daily_plan import load_daily_plan, validate_plan
from .engine import RunLedger
from .runners import create_runner
from .quest_io import QuestIO
from .results import stopped_result
from .paths import ROOT
from .runtime import WindowLease, new_run, write_json, event
from .vision import VisionDetector
from .remaining import CatalogueCounters, RemainingInspector, RemainingSnapshot, reconcile_remaining


def recorded_daily_result(plan, activity, daily, now):
    """Read reservation/clear history; fresh counters decide current daily status."""
    scope=activity.get('completion_scope')
    if not scope:
        return None
    reset=plan['reset_policies'][scope]
    try:
        daily.check_available(scope,plan['character_id'],activity['id'],reset,now)
    except DailyBlocked as blocked:
        if blocked.status=='complete':
            return {'status':'already_complete','run_id':blocked.run_id,'source':'daily_ledger',
                    'input_sent':False,'reason':'Clear and return were recorded for this character and daily cycle.'}
        if blocked.run_id:
            return {'status':'stopped_after_clear' if blocked.status=='cleared' else 'stopped',
                    'run_id':blocked.run_id,'source':'daily_ledger','input_sent':False,
                    'reason':'A previous attempt must be reconciled before continuing.'}
        raise
    return None


def run_daily_tasks(plan: dict, execute, recorded_completion=None, remaining_count=None, checkpoint=None) -> dict:
    """The caller supplies one bounded activity executor, including its guards."""
    validate_plan(plan)
    results=[]
    stopped=False
    for activity in plan['activities']:
        if not activity.get('enabled'):
            continue
        item={'id':activity['id'],'name':activity['name']}
        if stopped:
            item.update(status='not_attempted',reason='A previous activity stopped; reconcile the current screen first.')
        else:
            try:
                counted = (plan.get('completion_policy')=='fresh_remaining_entries'
                           and activity.get('remaining_counter') is True)
                saved=recorded_completion(activity) if recorded_completion else None
                unresolved=saved and saved.get('status')!='already_complete'
                counter=None
                if counted and remaining_count and not unresolved:
                    counter=remaining_count(activity, False)
                    # A scan can cross reset; re-read the scoped budget afterwards.
                    saved=recorded_completion(activity) if recorded_completion else None
                valid_counter=(counter and counter.get('verified') is True
                               and type(counter.get('remaining_entries')) is int
                               and counter['remaining_entries'] >= 0)
                if saved and saved.get('status')!='already_complete':
                    outcome=saved
                elif counted and not valid_counter:
                    outcome={'status':'completion_unverified','input_sent':False,
                             'reason':'Fresh remaining-entry count could not be verified.'}
                elif counted and counter.get('remaining_entries')==0:
                    outcome=saved or {'status':'already_complete','input_sent':False}
                elif saved:
                    outcome=saved
                elif not activity.get('config'):
                    outcome={'status':'needs_definition','input_sent':False,
                             'reason':activity.get('missing','Workflow not taught.')}
                else:
                    outcome=execute(activity)
                    item.update(outcome)
                    if counted and outcome.get('status') in {'complete','already_complete'}:
                        # A clear cannot substitute for a fresh post-run counter.
                        counter=remaining_count(activity, True) if remaining_count else None
                if counted:
                    outcome=reconcile_remaining(outcome,counter)
                item.update(outcome)
                inspected = (outcome.get('status')=='needs_definition'
                             and outcome.get('inspection_only') is True
                             and outcome.get('returned_home') is True)
                no_navigation = outcome.get('input_sent') is False and outcome.get('status') in {
                    'needs_definition','not_done','completion_unverified'}
                if outcome.get('status') not in {'complete','already_complete','dry_run'} and not inspected and not no_navigation:
                    stopped=True
            except (Exception,KeyboardInterrupt) as error:
                cleared=item.get('status') in {'complete','already_complete'} or item.get('activity_complete') is True
                item.update(status='stopped_after_clear' if cleared else 'stopped',
                            activity_complete=cleared, daily_done=None,
                            interrupted=isinstance(error,KeyboardInterrupt),
                            reason=str(error) or 'Operator stopped')
                stopped=True
        results.append(item)
        if checkpoint:
            checkpoint({'status':'running','character':plan['character_id'],
                        'activities':list(results),'rewards':'manual','all_selected_completed':False})
    complete=bool(results) and all(r['status'] in {'complete','already_complete'} for r in results)
    return {'status':'complete' if complete else 'incomplete','character':plan['character_id'],
            'activities':results,'rewards':'manual','all_selected_completed':complete,
            'interrupted':any(r.get('interrupted') for r in results)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,default=ROOT/'config/daily-plan.json')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--focus',action='store_true')
    parser.add_argument('--validate',action='store_true',help='Validate all configurations and vision assets without attaching to the game.')
    args=parser.parse_args(argv)
    report_dir=new_run(ROOT,'daily-report')
    report={'status':'stopped','phase':'preflight','activities':[],
            'all_selected_completed':False,'rewards':'manual'}
    snapshot=None

    def save(value):
        nonlocal report
        report={**value,'updated_at':datetime.now(timezone.utc).isoformat()}
        write_json(report_dir/'summary.json',report)

    try:
        if args.validate and (args.live or args.focus):
            raise ValueError('--validate cannot be combined with --live or --focus.')
        plan,inspection_config,configs=load_daily_plan(args.plan,ROOT)
        # Load every selected profile and counter template before any control session.
        profiles={cfg['profile'] for cfg in [inspection_config,*configs.values()]}
        detectors={profile:VisionDetector(ROOT/profile) for profile in profiles}
        CatalogueCounters(ROOT)
        save({**report,'character':plan['character_id'],'phase':'validated'})
        if args.validate:
            save({**report,'status':'validated','input_sent':False})
        else:
            with WindowLease(),NovaSession(focus_on_attach=args.focus) as session, \
                 DailyLedger(ROOT/'data/progress.sqlite') as daily,RunLedger(ROOT/'data/progress.sqlite') as actions:
                def inspect_remaining():
                    evidence=new_run(ROOT,'remaining-inspection')
                    io=QuestIO(session,detectors[inspection_config['profile']],evidence)
                    inspector=RemainingInspector(inspection_config,io,daily,actions)
                    try:
                        result=inspector.scan()
                    except (Exception,KeyboardInterrupt) as error:
                        write_json(evidence/'result.json',{
                            'status':'stopped','reason':str(error) or 'Operator interruption',
                            'last_frame':(inspector.runner.last or {}).get('frame_path')})
                        raise
                    write_json(evidence/'result.json',result)
                    write_json(report_dir/'remaining.json',result)
                    return result

                snapshot=RemainingSnapshot(plan,inspect_remaining)

                def execute(activity):
                    config=configs[activity['id']]
                    evidence=new_run(ROOT,'daily-'+activity['id'])
                    io=QuestIO(session,detectors[config['profile']],evidence)
                    runner=create_runner(config,io,daily,actions)
                    try:
                        result=runner.run(live=args.live)
                    except (Exception,KeyboardInterrupt) as error:
                        saved=daily.get(runner.run_id) if runner.run_id else None
                        result=stopped_result(error, runner.run_id, saved)
                    result['evidence']=str(evidence)
                    write_json(evidence/'result.json',result)
                    return result

                result=run_daily_tasks(plan,execute,
                    lambda activity:recorded_daily_result(plan,activity,daily,datetime.now(timezone.utc)),
                    snapshot.get if args.live else None,checkpoint=save)
                save(result)
    except (Exception,KeyboardInterrupt) as error:
        save({**report,'status':'stopped','all_selected_completed':False,
              'interrupted':isinstance(error,KeyboardInterrupt),
              'reason':str(error) or 'Operator interruption'})
        event(report_dir,'stopped',reason=report['reason'])
    finally:
        if snapshot and snapshot.snapshot:
            save({**report,'remaining_inspection':snapshot.snapshot})
    print(json.dumps(report,indent=2))
    print('Report: '+str(report_dir/'summary.json'))
    if report.get('interrupted'):
        return 130
    return 0 if report.get('all_selected_completed') or report['status']=='validated' else 2


if __name__=='__main__':
    raise SystemExit(main())
