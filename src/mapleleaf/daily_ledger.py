"""Per-character daily reservations and completion evidence, independent of input."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid


class DailyBlocked(RuntimeError):
    def __init__(self, message, *, status=None, run_id=None):
        super().__init__(message)
        self.status, self.run_id = status, run_id


def cycle_bounds(now: datetime, reset: dict) -> tuple[str, str]:
    if now.tzinfo is None:
        raise ValueError('A timezone-aware clock is required.')
    if not isinstance(reset, dict) or reset.get('verified') is not True:
        raise ValueError('Daily reset time must be verified before live entry.')
    hour, minute, offset = (reset.get(k) for k in ('hour', 'minute', 'utc_offset_minutes'))
    if any(type(v) is not int for v in (hour,minute,offset)) or not (0<=hour<24 and 0<=minute<60 and -840<=offset<=840):
        raise ValueError('Invalid fixed-offset reset time.')
    local = now.astimezone(timezone(timedelta(minutes=offset)))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc).isoformat(), (start+timedelta(days=1)).astimezone(timezone.utc).isoformat()


class DailyLedger:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS daily_reset_policy (scope TEXT PRIMARY KEY, reset_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_runs (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, character TEXT NOT NULL, activity TEXT NOT NULL,
                cycle_start TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL, evidence_json TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS daily_cycle_once
                ON daily_runs(scope,character,activity,cycle_start) WHERE cycle_start IS NOT NULL;
        ''')

    def reserve(self, scope: str, character: str, activity: str, reset: dict, now: datetime, evidence: dict) -> str:
        start,end = cycle_bounds(now,reset)
        if not all(isinstance(v,str) and v.strip() for v in (scope,character,activity)) or not evidence:
            raise ValueError('Daily entry needs account/world scope, character, activity and evidence.')
        stamp = now.astimezone(timezone.utc).isoformat()
        policy = json.dumps({k:reset[k] for k in ('hour','minute','utc_offset_minutes')},sort_keys=True)
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT reset_json FROM daily_reset_policy WHERE scope=?',(scope,)).fetchone()
            if previous and previous[0] != policy:
                raise DailyBlocked('Reset policy changed; reconcile existing daily history first.')
            self.db.execute('INSERT OR IGNORE INTO daily_reset_policy VALUES (?,?)',(scope,policy))
            # Reserved/uncertain attempts survive date changes. Importing an observed
            # pilot without a known reset still blocks the cycle containing its time.
            existing = self.db.execute('''SELECT id,status FROM daily_runs
                WHERE scope=? AND character=? AND activity=? AND
                (status IN ('reserved','running','uncertain') OR cycle_start=? OR
                 (started_at>=? AND started_at<?) OR started_at>?)''',
                (scope,character,activity,start,start,end,stamp)).fetchone()
            if existing:
                raise DailyBlocked(f"Daily entry already reserved or completed: {existing['id']} ({existing['status']}).",
                                   status=existing['status'],run_id=existing['id'])
            self.db.execute('INSERT INTO daily_runs VALUES (?,?,?,?,?,?,?,?,?)',
                (run_id,scope,character,activity,start,stamp,stamp,'reserved',json.dumps(evidence)))
        return run_id

    def check_available(self, scope: str, character: str, activity: str, reset: dict, now: datetime):
        """Read-only preflight; reserve repeats the checks in its transaction."""
        start,end=cycle_bounds(now,reset)
        policy=json.dumps({k:reset[k] for k in ('hour','minute','utc_offset_minutes')},sort_keys=True)
        previous=self.db.execute('SELECT reset_json FROM daily_reset_policy WHERE scope=?',(scope,)).fetchone()
        if previous and previous[0]!=policy:
            raise DailyBlocked('Reset policy changed; reconcile existing daily history first.')
        stamp=now.astimezone(timezone.utc).isoformat()
        row=self.db.execute('''SELECT id,status FROM daily_runs WHERE scope=? AND character=? AND activity=?
            AND (status IN ('reserved','running','uncertain') OR cycle_start=? OR
                 (started_at>=? AND started_at<?) OR started_at>?)''',(scope,character,activity,start,start,end,stamp)).fetchone()
        if row:
            raise DailyBlocked(f"Daily entry already reserved or completed: {row['id']} ({row['status']}).",
                               status=row['status'],run_id=row['id'])

    def transition(self, run_id: str, status: str, evidence: dict, now: datetime | None = None):
        allowed = {'reserved':{'running','uncertain'},'running':{'cleared','uncertain'},
                   'cleared':{'complete'},'uncertain':{'running','cleared'}}
        if not evidence:
            raise ValueError('A daily transition needs evidence.')
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT status,evidence_json FROM daily_runs WHERE id=?',(run_id,)).fetchone()
            if not row or status not in allowed.get(row['status'],set()):
                raise DailyBlocked('Invalid or repeated daily transition.')
            history=json.loads(row['evidence_json'])
            history.setdefault('transitions',[]).append({'status':status,'at':stamp,'evidence':evidence})
            self.db.execute('UPDATE daily_runs SET status=?,updated_at=?,evidence_json=? WHERE id=?',
                            (status,stamp,json.dumps(history),run_id))

    def import_completed(self, reference: str, scope: str, character: str, activity: str,
                         started_at: datetime, evidence: dict):
        """Explicit witnessed completion import; never infer this from a sent key."""
        if started_at.tzinfo is None or not evidence.get('clear') or not evidence.get('returned_home'):
            raise ValueError('Historical completion needs a dated clear and return-home evidence.')
        stamp=started_at.astimezone(timezone.utc).isoformat()
        payload=json.dumps(evidence,sort_keys=True)
        values=(reference,scope,character,activity,None,stamp,stamp,'complete',payload)
        with self.db:
            row=self.db.execute('SELECT * FROM daily_runs WHERE id=?',(reference,)).fetchone()
            if row and tuple(row)!=values:
                raise DailyBlocked('Completion reference already contains different evidence.')
            self.db.execute('INSERT OR IGNORE INTO daily_runs VALUES (?,?,?,?,?,?,?,?,?)',values)

    def get(self, run_id: str) -> dict | None:
        row=self.db.execute('SELECT * FROM daily_runs WHERE id=?',(run_id,)).fetchone()
        return dict(row) if row else None

    def summary(self, scope: str, character: str) -> list[dict]:
        return [dict(r) for r in self.db.execute('SELECT * FROM daily_runs WHERE scope=? AND character=? ORDER BY started_at',(scope,character))]

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()
