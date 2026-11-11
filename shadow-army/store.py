"""Profile-owned transactions shared by plugin, worker and dashboard processes."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


class Store:
    def __init__(self, directory, namespace='real', on_event=None):
        self.path = Path(directory) / 'gates.sqlite3'
        self.namespace = namespace
        self.on_event = on_event

    @contextmanager
    def read(self):
        if not self.path.exists():
            yield None
            return
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0.3)
        try:
            db.execute('BEGIN')
            db.execute('SELECT 1 FROM records LIMIT 0')
            yield db
        finally:
            db.close()

    def prune(self, db):
        now = time.time()
        rows = db.execute("SELECT kind,key,data FROM records WHERE ns=? AND kind IN ('session','terminal_pending','check','refusal','usage')", (self.namespace,)).fetchall()
        for kind, key, data in rows:
            value = json.loads(data)
            stamp = max((r.get('observed_at', 0) for r in value), default=0) if isinstance(value, list) else value.get('observed_at', 0)
            if stamp < now - (7 * 86400 if kind == 'session' else 86400):
                db.execute('DELETE FROM records WHERE ns=? AND kind=? AND key=?', (self.namespace, kind, key))

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=0.3)
        try:
            version = sqlite3.sqlite_version_info
            safe = (version < (3, 7, 0) or version >= (3, 51, 3) or
                    (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0))
            mode = 'WAL' if safe else 'DELETE'
            if db.execute('PRAGMA journal_mode').fetchone()[0].upper() != mode:
                db.execute('PRAGMA journal_mode=' + mode)
            db.execute('CREATE TABLE IF NOT EXISTS records(ns TEXT, kind TEXT, key TEXT, data TEXT, PRIMARY KEY(ns,kind,key))')
            db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT, board TEXT, card TEXT, data TEXT)')
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            before = db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
            yield db
            changed = db.execute('SELECT board,card FROM events WHERE id>?', (before,)).fetchall()
            db.commit()
            if self.on_event:
                for board, card in changed:
                    try:
                        self.on_event({'board': board, 'card': card, 'demo': self.namespace == 'trial'})
                    except Exception:
                        pass
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, db, kind):
        if db is None:
            return []
        return [json.loads(row[0]) for row in db.execute('SELECT data FROM records WHERE ns=? AND kind=? ORDER BY rowid', (self.namespace, kind))]

    def get(self, db, kind, key):
        if db is None:
            return None
        row = db.execute('SELECT data FROM records WHERE ns=? AND kind=? AND key=?', (self.namespace, kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, db, kind, key, data):
        db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(ns,kind,key) DO UPDATE SET data=excluded.data',
                   (self.namespace, kind, key, json.dumps(data)))

    def event(self, db, key, data):
        db.execute('INSERT INTO events(ns,board,card,data) VALUES(?,?,?,?)', (self.namespace, *key.values(), json.dumps(data)))

    def history(self, db, key):
        if db is None:
            return []
        rows = db.execute('SELECT data FROM events WHERE ns=? AND board=? AND card=? ORDER BY id DESC LIMIT 100', (self.namespace, *key.values()))
        return list(reversed([json.loads(row[0]) for row in rows]))

    def revision(self, db):
        if db is None:
            return 0
        return db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE ns=?', (self.namespace,)).fetchone()[0]

    def reset(self, db):
        db.execute('DELETE FROM records WHERE ns=?', (self.namespace,))
        db.execute('DELETE FROM events WHERE ns=?', (self.namespace,))
