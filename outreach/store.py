"""State store: SQLite engine, git-friendly JSONL source of truth.

* `state/leads.jsonl`   one lead per line, sorted by id. This is the committed file;
                        line-oriented, so git diffs and rebases stay clean.
* `state/events.jsonl`  append-only audit log (sends, state changes worth auditing).
* `state/meta.json`     small key/value facts (migration done, processed CSV hashes).

On open, the JSONL is loaded into an in-memory SQLite DB (indexed queries, transactional
updates). `checkpoint()` writes everything back atomically (temp file + fsync + os.replace),
so a crash can never leave a half-written state file.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from .models import Lead, LeadState, identity_keys, make_lead_id, utcnow

log = logging.getLogger(__name__)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.leads_path = self.dir / "leads.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.meta_path = self.dir / "meta.json"
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE leads (id TEXT PRIMARY KEY, state TEXT, updated_at TEXT, data TEXT)")
        self.db.execute("CREATE INDEX leads_state ON leads(state)")
        self.db.execute("CREATE TABLE lead_keys (key TEXT PRIMARY KEY, lead_id TEXT)")
        self.meta: dict = {}
        self._dirty = False
        self._pending_events: list[dict] = []
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.meta_path.exists():
            try:
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.error("meta.json is corrupt; starting with empty meta")
                self.meta = {}
        if not self.leads_path.exists():
            return
        bad = 0
        with open(self.leads_path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    lead = Lead.model_validate_json(line)
                except Exception as e:  # one corrupt line must not take the whole store down
                    bad += 1
                    log.error("leads.jsonl line %d unreadable (%s); skipped", n, str(e)[:120])
                    continue
                self._put(lead)
        if bad:
            log.error("%d lead lines could not be parsed", bad)
        self._dirty = False

    def checkpoint(self) -> None:
        """Flush leads/meta/events to disk atomically. Cheap at this scale (≤ a few k leads)."""
        if self._pending_events:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as f:
                for ev in self._pending_events:
                    f.write(json.dumps(ev, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._pending_events.clear()
        if not self._dirty:
            return
        rows = self.db.execute("SELECT data FROM leads ORDER BY id").fetchall()
        _atomic_write(self.leads_path, "".join(r[0] + "\n" for r in rows))
        _atomic_write(self.meta_path, json.dumps(self.meta, indent=2, sort_keys=True, default=str) + "\n")
        self._dirty = False

    # ── leads ───────────────────────────────────────────────────────────────
    def _put(self, lead: Lead) -> None:
        data = lead.model_dump_json(exclude_none=True)
        self.db.execute(
            "INSERT OR REPLACE INTO leads (id, state, updated_at, data) VALUES (?, ?, ?, ?)",
            (lead.id, lead.state.value, lead.updated_at.isoformat(), data),
        )
        extra = [f"ap:{lead.legacy['apollo_id']}"] if lead.legacy.get("apollo_id") else []
        for key in identity_keys(lead.person, lead.company, lead.email) + extra + [f"id:{lead.id}"]:
            self.db.execute("INSERT OR IGNORE INTO lead_keys (key, lead_id) VALUES (?, ?)", (key, lead.id))
        for cand in lead.candidates:
            self.db.execute("INSERT OR IGNORE INTO lead_keys (key, lead_id) VALUES (?, ?)",
                            (f"em:{cand.email.lower()}", lead.id))
        self._dirty = True

    def save(self, lead: Lead) -> None:
        lead.updated_at = utcnow()
        self._put(lead)

    def get(self, lead_id: str) -> Lead | None:
        row = self.db.execute("SELECT data FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return Lead.model_validate_json(row[0]) if row else None

    def find_existing(self, keys: Iterable[str]) -> Lead | None:
        for key in keys:
            row = self.db.execute("SELECT lead_id FROM lead_keys WHERE key = ?", (key,)).fetchone()
            if row:
                return self.get(row[0])
        return None

    def email_known(self, email: str, exclude_id: str = "") -> bool:
        row = self.db.execute("SELECT lead_id FROM lead_keys WHERE key = ?", (f"em:{email.lower()}",)).fetchone()
        return bool(row) and row[0] != exclude_id

    def new_id(self, keys: list[str]) -> str:
        lid = make_lead_id(keys)
        n = 1
        while self.db.execute("SELECT 1 FROM leads WHERE id = ?", (lid,)).fetchone():
            lid = make_lead_id([f"{keys[0] if keys else ''}#{n}"])
            n += 1
        return lid

    def by_state(self, *states: LeadState) -> list[Lead]:
        q = "SELECT data FROM leads WHERE state IN (%s) ORDER BY updated_at, id" % ",".join("?" * len(states))
        return [Lead.model_validate_json(r[0]) for r in self.db.execute(q, [s.value for s in states])]

    def all(self) -> Iterator[Lead]:
        for (data,) in self.db.execute("SELECT data FROM leads ORDER BY id").fetchall():
            yield Lead.model_validate_json(data)

    def count_by_state(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT state, COUNT(*) FROM leads GROUP BY state").fetchall())

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    # ── events & meta ───────────────────────────────────────────────────────
    def event(self, kind: str, lead_id: str = "", **data) -> None:
        self._pending_events.append({"ts": utcnow().isoformat(), "kind": kind, "lead_id": lead_id, **data})

    def flush_events(self) -> None:
        """Persist events immediately (used right after each live send)."""
        dirty = self._dirty
        self._dirty = False
        self.checkpoint()
        self._dirty = dirty

    def set_meta(self, key: str, value) -> None:
        if self.meta.get(key) != value:
            self.meta[key] = value
            self._dirty = True

    def sends_between(self, start: datetime, end: datetime) -> int:
        """Count real sends (v1 legacy sends included; dry runs never create SendRecords)."""
        n = 0
        for lead in self.by_state(LeadState.SENT, LeadState.REPLIED, LeadState.BOUNCED, LeadState.OPTED_OUT):
            n += sum(1 for s in lead.sends if start <= s.sent_at < end)
        return n
