"""Action cache (tier 0) and plan cache.

actions: (domain, page, intent) -> {action, selectors, expect, text?}
plans:   task key -> list of {intent, value}
An entry that fails twice in a row is dropped, so the cache heals itself.
"""
import json
import os
import re
import sqlite3
import time

MAX_FAILS = 2


def norm_intent(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower()).rstrip(".")


class ActionCache:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS actions (
                domain TEXT, page TEXT, intent TEXT, action_json TEXT,
                hits INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, updated REAL,
                PRIMARY KEY (domain, page, intent));
            CREATE TABLE IF NOT EXISTS plans (
                key TEXT PRIMARY KEY, goal TEXT, plan_json TEXT,
                hits INTEGER DEFAULT 0, updated REAL);
            """
        )
        self.db.commit()

    # ---- actions -------------------------------------------------------
    def get(self, domain, page, intent):
        """Exact page match first, then the same intent anywhere on the domain (SPAs)."""
        row = self.db.execute(
            "SELECT page, action_json FROM actions WHERE domain=? AND page=? AND intent=?",
            (domain, page, intent),
        ).fetchone()
        if not row:
            row = self.db.execute(
                "SELECT page, action_json FROM actions WHERE domain=? AND intent=? "
                "ORDER BY hits DESC LIMIT 1",
                (domain, intent),
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def put(self, domain, page, intent, entry):
        self.db.execute(
            "INSERT INTO actions (domain, page, intent, action_json, hits, fails, updated) "
            "VALUES (?, ?, ?, ?, 0, 0, ?) "
            "ON CONFLICT(domain, page, intent) DO UPDATE SET "
            "action_json=excluded.action_json, fails=0, updated=excluded.updated",
            (domain, page, intent, json.dumps(entry), time.time()),
        )
        self.db.commit()

    def hit(self, domain, page, intent):
        self.db.execute(
            "UPDATE actions SET hits=hits+1, fails=0, updated=? WHERE domain=? AND page=? AND intent=?",
            (time.time(), domain, page, intent),
        )
        self.db.commit()

    def fail(self, domain, page, intent):
        self.db.execute(
            "UPDATE actions SET fails=fails+1 WHERE domain=? AND page=? AND intent=?",
            (domain, page, intent),
        )
        self.db.execute("DELETE FROM actions WHERE fails>=?", (MAX_FAILS,))
        self.db.commit()

    def list(self, domain=None):
        sql = "SELECT domain, page, intent, hits, fails, action_json FROM actions"
        args = ()
        if domain:
            sql += " WHERE domain=?"
            args = (domain,)
        return self.db.execute(sql + " ORDER BY domain, page, intent", args).fetchall()

    def clear(self, domain=None):
        if domain:
            self.db.execute("DELETE FROM actions WHERE domain=?", (domain,))
        else:
            self.db.execute("DELETE FROM actions")
            self.db.execute("DELETE FROM plans")
        self.db.commit()

    # ---- plans ---------------------------------------------------------
    def get_plan(self, key):
        row = self.db.execute("SELECT plan_json FROM plans WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE plans SET hits=hits+1, updated=? WHERE key=?", (time.time(), key))
        self.db.commit()
        return json.loads(row[0])

    def put_plan(self, key, goal, steps):
        self.db.execute(
            "INSERT OR REPLACE INTO plans (key, goal, plan_json, hits, updated) VALUES (?, ?, ?, 0, ?)",
            (key, goal, json.dumps(steps), time.time()),
        )
        self.db.commit()

    def drop_plan(self, key):
        self.db.execute("DELETE FROM plans WHERE key=?", (key,))
        self.db.commit()
