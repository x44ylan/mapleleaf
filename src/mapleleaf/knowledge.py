"""Local, versioned game knowledge. Historical observations never authorize input."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys


KINDS = {"policy", "menu", "activity", "binding", "lesson", "question", "route"}
STATUSES = {"user_confirmed", "observed", "documented", "tested", "unknown", "stale"}
SOURCE_KINDS = {"user", "screenshot", "documentation", "note", "observation", "run"}
FIELDS = {"id", "kind", "title", "status", "details", "sources", "scope"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _validate(record):
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise ValueError(f"A record requires exactly: {', '.join(sorted(FIELDS))}")
    if not isinstance(record["id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", record["id"]):
        raise ValueError("Invalid stable record ID.")
    if record["kind"] not in KINDS or record["status"] not in STATUSES:
        raise ValueError("Unknown record kind or evidence status.")
    if not isinstance(record["title"], str) or not record["title"].strip():
        raise ValueError("A nonempty title is required.")
    if not isinstance(record["details"], dict) or not isinstance(record["scope"], dict):
        raise ValueError("Details and scope must be objects.")
    if not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in record["scope"].items()):
        raise ValueError("Scope keys and values must be nonempty strings.")
    sources = record["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("At least one source is required.")
    for source in sources:
        if (not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS
                or not isinstance(source.get("reference"), str) or not source["reference"].strip()):
            raise ValueError("Each source needs a known kind and nonempty reference.")
    if record["status"] == "user_confirmed" and not any(s["kind"] == "user" for s in sources):
        raise ValueError("User-confirmed records need a user source.")
    if record["status"] == "tested" and not any(s["kind"] in {"run", "observation"} for s in sources):
        raise ValueError("Tested behavior needs run or observation evidence.")
    return json.loads(_json(record))


class RevisionConflict(ValueError):
    pass


class KnowledgeStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            with self.connection:
                self.connection.execute("CREATE TABLE IF NOT EXISTS knowledge_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)")
                self.connection.execute("INSERT OR IGNORE INTO knowledge_schema VALUES (1, 1)")
                if self.connection.execute("SELECT version FROM knowledge_schema WHERE id=1").fetchone()[0] != 1:
                    raise ValueError("Unsupported knowledge schema version; preserve the database and migrate explicitly.")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_revisions (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(id, revision))""")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_observations (
                    source_path TEXT PRIMARY KEY, indexed_at TEXT NOT NULL, document TEXT NOT NULL)""")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    @staticmethod
    def _decode(row):
        return {**json.loads(row["document"]), "revision": row["revision"], "recorded_at": row["recorded_at"]}

    def get(self, record_id):
        row = self.connection.execute("SELECT * FROM knowledge_revisions WHERE id=? ORDER BY revision DESC LIMIT 1", (record_id,)).fetchone()
        return self._decode(row) if row else None

    def history(self, record_id):
        return [self._decode(row) for row in self.connection.execute("SELECT * FROM knowledge_revisions WHERE id=? ORDER BY revision", (record_id,))]

    def records(self, *, query="", kind=None, status=None):
        rows = self.connection.execute("""SELECT k.* FROM knowledge_revisions k
            JOIN (SELECT id, MAX(revision) AS revision FROM knowledge_revisions GROUP BY id) latest
            ON k.id=latest.id AND k.revision=latest.revision ORDER BY k.id""")
        return [record for row in rows if (record := self._decode(row))
                and (kind is None or record["kind"] == kind)
                and (status is None or record["status"] == status)
                and query.casefold() in _json(record).casefold()]

    def _append(self, record, revision):
        self.connection.execute("INSERT INTO knowledge_revisions VALUES (?, ?, ?, ?)",
                                (record["id"], revision, datetime.now(timezone.utc).isoformat(), _json(record)))

    def put(self, record, *, expected_revision=0):
        record = _validate(record)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a nonnegative integer; 0 creates a record.")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            previous = self.get(record["id"])
            actual = previous["revision"] if previous else 0
            if actual != expected_revision:
                raise RevisionConflict(f"{record['id']}: expected revision {expected_revision}, current revision {actual}.")
            if previous and all(previous[key] == record[key] for key in FIELDS):
                return previous
            if (previous and previous["status"] == "stale" and record["status"] in {"observed", "tested"}
                    and not any(source not in previous["sources"] for source in record["sources"])):
                raise ValueError("Revalidating stale knowledge requires new evidence.")
            self._append(record, actual + 1)
            return self.get(record["id"])

    def seed(self, document):
        if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise ValueError("Seed requires schema_version 1.")
        if not isinstance(document.get("records"), list):
            raise ValueError("Seed requires a records list.")
        records = [_validate(record) for record in document["records"]]
        if len({record["id"] for record in records}) != len(records):
            raise ValueError("Seed contains duplicate IDs.")
        added = 0
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for record in records:
                if self.get(record["id"]) is None:
                    self._append(record, 1)
                    added += 1
        return {"added": added, "preserved": len(records) - added}

    def invalidate(self, scope_key, scope_value, reason):
        if not scope_key or not scope_value or not reason.strip():
            raise ValueError("Invalidation requires a scope key/value and a reason.")
        changed = 0
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for old in self.records():
                if old["scope"].get(scope_key) != scope_value or old["status"] == "stale":
                    continue
                record = {key: old[key] for key in FIELDS}
                record["details"]["invalidation"] = {"reason": reason, "previous_status": old["status"]}
                record["status"] = "stale"
                self._append(record, old["revision"] + 1)
                changed += 1
        return changed

    def record_observation(self, source_path, observation):
        if not isinstance(observation, dict) or not isinstance(observation.get("state"), str):
            raise ValueError("An observation needs a state label.")
        source = str(Path(source_path).resolve())
        # This table deliberately has no route to the live action guard.
        summary = {key: observation[key] for key in (
            "state", "blocked", "captured_utc", "window", "preset", "quickslot_page",
            "reasons", "frame_path", "annotated_path", "plan") if key in observation}
        summary["historical_only"] = True
        body = _json(summary)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            old = self.connection.execute("SELECT document FROM knowledge_observations WHERE source_path=?", (source,)).fetchone()
            if old:
                if old[0] != body:
                    raise RevisionConflict("Existing observation evidence changed; save it under a new path.")
                return False
            self.connection.execute("INSERT INTO knowledge_observations VALUES (?, ?, ?)",
                                    (source, datetime.now(timezone.utc).isoformat(), body))
        return True

    def observations(self, limit=5):
        return [{"source_path": row["source_path"], "indexed_at": row["indexed_at"], **json.loads(row["document"])}
                for row in self.connection.execute("SELECT * FROM knowledge_observations ORDER BY indexed_at DESC LIMIT ?", (limit,))]

    def context(self, topic="", limit=12):
        policies = self.records(kind="policy")
        candidates = self.records(query=topic) if topic else [r for r in self.records() if r["kind"] in {"lesson", "question"}]
        candidates.sort(key=lambda r: (r["kind"] != "lesson", r["id"]))
        chosen = {r["id"]: r for r in policies}
        added = 0
        for record in candidates:
            if record["id"] in chosen:
                continue
            if added >= limit:
                break
            chosen[record["id"]] = record
            added += 1
        return {"current_game_state": "UNVERIFIED — capture afresh before any input",
                "notice": "Saved facts are evidence, not input authorization. Historical observations are excluded.",
                "records": list(chosen.values()),
                "matching_records": len({r["id"] for r in policies + candidates})}

    def export_markdown(self):
        def cell(value):
            text = value if isinstance(value, str) else _json(value)
            return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        lines = ["# Mapleleaf persistent game knowledge", "",
                 "Generated from the local knowledge store. Observed means seen; tested means behavior evidence exists.",
                 "Current game state is unverified. These records do not authorize input.", ""]
        for kind in ("policy", "activity", "menu", "binding", "route", "lesson", "question"):
            records = self.records(kind=kind)
            if not records:
                continue
            lines.extend([f"## {kind.title()}", "", "| ID | Name | Evidence status | Details | Revision |",
                          "|---|---|---|---|---|"])
            for record in records:
                lines.append("| " + " | ".join(cell(record[key]) for key in ("id", "title", "status", "details", "revision")) + " |")
            lines.extend(["", "Sources:", ""])
            for record in records:
                lines.append(f"- {record['id']}: " + "; ".join(cell(s["kind"] + ": " + s["reference"]) for s in record["sources"]))
            lines.append("")
        return "\n".join(lines)


def main(argv=None, *, root=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=root / "data/progress.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Import seed without replacing saved corrections.")
    init.add_argument("--seed", type=Path, default=root / "knowledge/seed.json")
    listing = commands.add_parser("list")
    listing.add_argument("--query", default="")
    listing.add_argument("--kind", choices=sorted(KINDS))
    listing.add_argument("--status", choices=sorted(STATUSES))
    for name in ("show", "history"):
        commands.add_parser(name).add_argument("id")
    put = commands.add_parser("put", help="Save one reviewed record; existing records require their current revision.")
    put.add_argument("--file", type=Path, required=True)
    put.add_argument("--expected-revision", type=int, default=0)
    context = commands.add_parser("context")
    context.add_argument("--topic", default="")
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("--scope", required=True, help="Exact scope key=value")
    invalidate.add_argument("--reason", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    commands.add_parser("index-runs", help="Index existing observation files; never attach to the game.")
    commands.add_parser("observations", help="Historical evidence only, never current state.")
    args = parser.parse_args(argv)
    try:
        with KnowledgeStore(args.db) as store:
            if args.command == "init":
                result = store.seed(json.loads(args.seed.read_text(encoding="utf-8-sig")))
            elif args.command == "list":
                result = store.records(query=args.query, kind=args.kind, status=args.status)
            elif args.command in {"show", "history"}:
                result = store.get(args.id) if args.command == "show" else store.history(args.id)
                if not result:
                    raise ValueError(f"Unknown record: {args.id}")
            elif args.command == "put":
                result = store.put(json.loads(args.file.read_text(encoding="utf-8-sig")), expected_revision=args.expected_revision)
            elif args.command == "context":
                result = store.context(args.topic)
            elif args.command == "invalidate":
                key, separator, value = args.scope.partition("=")
                if not separator:
                    raise ValueError("Scope must be key=value.")
                result = {"invalidated": store.invalidate(key, value, args.reason)}
            elif args.command == "export":
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(store.export_markdown(), encoding="utf-8")
                result = {"exported": str(args.output.resolve())}
            elif args.command == "index-runs":
                added = existing = 0
                for path in sorted((root / "runs").glob("*/observation-*.json")):
                    if store.record_observation(path, json.loads(path.read_text(encoding="utf-8"))):
                        added += 1
                    else:
                        existing += 1
                result = {"indexed": added, "already_indexed": existing}
            else:
                result = store.observations()
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"Knowledge error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
