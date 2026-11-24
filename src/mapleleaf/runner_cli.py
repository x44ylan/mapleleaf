"""Start one bounded taught activity; dry-run unless --live is explicit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .capture import NovaSession
from .daily_ledger import DailyLedger
from .engine import RunLedger
from .runners import create_runner
from .runtime import WindowLease, new_run, write_json, event
from .vision import VisionDetector
from .quest_io import QuestIO
from .results import stopped_result
from .paths import ROOT


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'quests/elite.json')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--prepare-only',action='store_true',help='Verify preset and entry screen, then return home without entering.')
    parser.add_argument('--focus',action='store_true')
    parser.add_argument('--resume',action='store_true',help='Reconcile an interrupted run without repeating entry.')
    args=parser.parse_args(argv)
    run_dir=new_run(ROOT,'quest')
    runner=None
    try:
        if args.resume and args.prepare_only:
            raise ValueError('Choose resume or prepare-only, not both.')
        config=json.loads(args.config.read_text(encoding='utf-8'))
        detector=VisionDetector(ROOT/config['profile'])
        with WindowLease(), NovaSession(focus_on_attach=args.focus) as session, \
             DailyLedger(ROOT/'data/progress.sqlite') as daily, RunLedger(ROOT/'data/progress.sqlite') as actions:
            io=QuestIO(session,detector,run_dir)
            runner=create_runner(config,io,daily,actions)
            result=runner.resume(live=args.live) if args.resume else runner.run(live=args.live,prepare_only=args.prepare_only)
        write_json(run_dir/'result.json',result)
        print(json.dumps(result,indent=2))
        print('Evidence: '+str(run_dir))
        return 0
    except (Exception,KeyboardInterrupt) as error:
        saved=None
        if runner and runner.run_id:
            with DailyLedger(ROOT/'data/progress.sqlite') as daily:
                saved=daily.get(runner.run_id)
        result=stopped_result(error, runner.run_id if runner else None, saved)
        write_json(run_dir/'result.json',result)
        event(run_dir,'stopped',**result)
        print(json.dumps(result,indent=2),file=sys.stderr)
        print('Evidence: '+str(run_dir))
        return 130 if isinstance(error,KeyboardInterrupt) else 2


if __name__=='__main__':
    raise SystemExit(main())
