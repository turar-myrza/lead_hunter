"""Local durable queue and limited event log for the sales team."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3

DEFAULT_DB = Path('data/sales_team/state.sqlite3')
SCHEMA = """
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY, origin TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS step (
    run_id INTEGER NOT NULL REFERENCES run(id), subject TEXT NOT NULL,
    role TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(run_id,subject,role));
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run(id),
    kind TEXT NOT NULL, subject TEXT NOT NULL, at TEXT NOT NULL,
    external_id TEXT UNIQUE, variant TEXT, language TEXT, segment TEXT);
CREATE TABLE IF NOT EXISTS identity (
    key TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES run(id), company TEXT NOT NULL);
-- The adaptive ledger is intentionally separate from `step`: interrupted
-- universal runs clear `step` values before a safe restart, while decisions
-- and completed research attempts must remain auditable and resumable.
CREATE TABLE IF NOT EXISTS adaptive_campaign (
    run_id INTEGER PRIMARY KEY REFERENCES run(id), goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned', stop_reason TEXT,
    created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adaptive_hypothesis (
    run_id INTEGER NOT NULL REFERENCES run(id), hypothesis_id TEXT NOT NULL,
    revision INTEGER NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY(run_id,hypothesis_id));
CREATE TABLE IF NOT EXISTS adaptive_strategy (
    run_id INTEGER NOT NULL REFERENCES run(id), strategy_id TEXT NOT NULL,
    payload TEXT NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY(run_id,strategy_id));
CREATE TABLE IF NOT EXISTS adaptive_attempt (
    run_id INTEGER NOT NULL REFERENCES run(id), sequence INTEGER NOT NULL,
    payload TEXT NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY(run_id,sequence));
CREATE TABLE IF NOT EXISTS adaptive_experience (
    run_id INTEGER NOT NULL REFERENCES run(id), experience_id TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(run_id,experience_id));
CREATE TABLE IF NOT EXISTS adaptive_journal (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES run(id),
    sequence INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    created TEXT NOT NULL, UNIQUE(run_id,sequence));
"""
OBSERVED_EVENTS = frozenset({
    'email_sent', 'email_delivered', 'call_connected', 'interested_reply',
    'declined', 'opt_out', 'meeting_agreed', 'meeting_booked', 'sale',
})


def now():
    return datetime.now(timezone.utc)


class SalesStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
        fd = os.open(self.path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=12)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def worker_lock(self, run_id=None):
        suffix = '.lock' if run_id is None else f'.{run_id}.lock'
        file = self.path.with_suffix(suffix)
        fd = os.open(file, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)

    def set_stopped(self, value):
        with self.connect() as db:
            db.execute("INSERT INTO setting VALUES('stopped',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(int(value)),))

    def stopped(self):
        with self.connect() as db:
            value = db.execute("SELECT value FROM setting WHERE key='stopped'").fetchone()
            return value is not None and value[0] == '1'

    def enqueue(self, origin, payload=None):
        with self.connect() as db:
            return db.execute('INSERT INTO run(origin,payload,created) VALUES(?,?,?)',
                              (origin, json.dumps(payload, ensure_ascii=False), now().isoformat())).lastrowid

    def tick(self, interval):
        """Schedule once per interval, in the same transaction as the queue insertion."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            settings = dict(db.execute('SELECT key,value FROM setting').fetchall())
            if settings.get('stopped') == '1' or now().timestamp() < float(settings.get('next_search', '0')):
                return
            if db.execute("SELECT 1 FROM run WHERE origin='automatic' AND status IN ('queued','running')").fetchone():
                return
            db.execute("INSERT INTO run(origin,payload,created) VALUES('automatic','null',?)", (now().isoformat(),))
            db.execute("INSERT INTO setting VALUES('next_search',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(now().timestamp() + interval),))

    def claim(self):
        # Worker lock is held by the caller. Recover the oldest interrupted run.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            check = db.execute("SELECT value FROM setting WHERE key='stopped'").fetchone()
            if check and check[0] == '1':
                return None
            row = db.execute("SELECT * FROM run WHERE status IN ('queued','running') "
                             "AND origin IN ('automatic','public_batch','simulation') ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE run SET status='running' WHERE id=?", (row['id'],))
                return dict(row)

    def get_run(self, run_id, origin):
        with self.connect() as db:
            row = db.execute('SELECT * FROM run WHERE id=? AND origin=?', (run_id, origin)).fetchone()
            return dict(row) if row else None

    def claim_run(self, run_id, origin):
        """Claim a specific run under its worker lock. Restart an interrupted run from its input."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM run WHERE id=? AND origin=?', (run_id, origin)).fetchone()
            if row is None or row['status'] not in {'queued', 'running'}:
                return None
            if row['status'] == 'running':
                db.execute('DELETE FROM step WHERE run_id=?', (run_id,))
            db.execute("UPDATE run SET status='running' WHERE id=?", (run_id,))
            return dict(row)

    def step(self, run_id, subject, role):
        with self.connect() as db:
            row = db.execute('SELECT value FROM step WHERE run_id=? AND subject=? AND role=?',
                             (run_id, subject, role)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, run_id, subject, role, value):
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO step VALUES(?,?,?,?)',
                                (run_id, subject, role, json.dumps(value, ensure_ascii=False)))
            if cursor.rowcount:
                db.execute('INSERT INTO event(run_id,kind,subject,at) VALUES(?,?,?,?)',
                           (run_id, role, subject, now().isoformat()))
            return bool(cursor.rowcount)

    def finish(self, run_id, status, error=None):
        with self.connect() as db:
            db.execute('UPDATE run SET status=? WHERE id=?', (status, run_id))
            if error:
                db.execute('INSERT INTO event(run_id,kind,subject,at) VALUES(?,?,?,?)',
                           (run_id, error, '', now().isoformat()))

    # ---- Adaptive LeadHunter ledger -------------------------------------
    # These methods intentionally use concise structured payloads. They are
    # append-only where a historical explanation matters and avoid storing
    # unstructured model traces or private CRM data.
    def adaptive_start(self, run_id, goal, experience=()):
        stamp = now().isoformat()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO adaptive_campaign(run_id,goal,status,created,updated) VALUES(?,?,?,?,?)',
                       (run_id, json.dumps(goal, ensure_ascii=False), 'planned', stamp, stamp))
            db.executemany('INSERT OR IGNORE INTO adaptive_experience(run_id,experience_id,payload) VALUES(?,?,?)',
                           [(run_id, item['id'], json.dumps(item, ensure_ascii=False)) for item in experience])

    def adaptive_set_campaign(self, run_id, *, status=None, stop_reason=None):
        with self.connect() as db:
            row = db.execute('SELECT status,stop_reason FROM adaptive_campaign WHERE run_id=?', (run_id,)).fetchone()
            if not row:
                return False
            db.execute('UPDATE adaptive_campaign SET status=?,stop_reason=?,updated=? WHERE run_id=?',
                       (status if status is not None else row['status'],
                        stop_reason if stop_reason is not None else row['stop_reason'], now().isoformat(), run_id))
        return True

    def adaptive_save_hypothesis(self, run_id, payload):
        with self.connect() as db:
            db.execute('INSERT INTO adaptive_hypothesis VALUES(?,?,?,?,?) '
                       'ON CONFLICT(run_id,hypothesis_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,updated=excluded.updated',
                       (run_id, payload['id'], payload['revision'], json.dumps(payload, ensure_ascii=False), now().isoformat()))

    def adaptive_save_strategy(self, run_id, payload):
        with self.connect() as db:
            db.execute('INSERT INTO adaptive_strategy VALUES(?,?,?,?) '
                       'ON CONFLICT(run_id,strategy_id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                       (run_id, payload['id'], json.dumps(payload, ensure_ascii=False), now().isoformat()))

    def adaptive_save_attempt(self, run_id, payload):
        with self.connect() as db:
            db.execute('INSERT INTO adaptive_attempt VALUES(?,?,?,?) '
                       'ON CONFLICT(run_id,sequence) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                       (run_id, payload['sequence'], json.dumps(payload, ensure_ascii=False), now().isoformat()))

    def adaptive_journal(self, run_id, kind, payload):
        stamp = now().isoformat()
        with self.connect() as db:
            sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM adaptive_journal WHERE run_id=?',
                                  (run_id,)).fetchone()[0]
            db.execute('INSERT INTO adaptive_journal(run_id,sequence,kind,payload,created) VALUES(?,?,?,?,?)',
                       (run_id, sequence, kind, json.dumps(payload, ensure_ascii=False), stamp))
        return sequence

    def adaptive_snapshot(self, run_id):
        with self.connect() as db:
            campaign = db.execute('SELECT * FROM adaptive_campaign WHERE run_id=?', (run_id,)).fetchone()
            if not campaign:
                return None
            hypotheses = [json.loads(row['payload']) for row in db.execute(
                'SELECT payload FROM adaptive_hypothesis WHERE run_id=? ORDER BY hypothesis_id', (run_id,))]
            strategies = [json.loads(row['payload']) for row in db.execute(
                'SELECT payload FROM adaptive_strategy WHERE run_id=? ORDER BY strategy_id', (run_id,))]
            attempts = [json.loads(row['payload']) for row in db.execute(
                'SELECT payload FROM adaptive_attempt WHERE run_id=? ORDER BY sequence', (run_id,))]
            experience = [json.loads(row['payload']) for row in db.execute(
                'SELECT payload FROM adaptive_experience WHERE run_id=? ORDER BY experience_id', (run_id,))]
            journal = [{'sequence': row['sequence'], 'kind': row['kind'], 'payload': json.loads(row['payload']), 'created': row['created']}
                       for row in db.execute('SELECT sequence,kind,payload,created FROM adaptive_journal WHERE run_id=? ORDER BY sequence',
                                             (run_id,))]
        return {'run_id': run_id, 'status': campaign['status'], 'goal': json.loads(campaign['goal']),
                'stop_reason': campaign['stop_reason'], 'hypotheses': hypotheses, 'strategies': strategies,
                'attempts': attempts, 'experience': experience, 'journal': journal}

    def outcome_experience_summary(self):
        """Return only aggregate observed outcome counts for planning context."""
        with self.connect() as db:
            counts = dict(db.execute("SELECT kind,count(*) FROM event WHERE kind IN ('sale','meeting_booked','interested_reply','declined','opt_out') GROUP BY kind"))
        return counts

    def prior(self, run_id, keys):
        with self.connect() as db:
            for key in keys:
                row = db.execute('SELECT company,run_id FROM identity WHERE key=?', (key,)).fetchone()
                if row and row['run_id'] != run_id:
                    return row['company']
        return None

    def register(self, run_id, company, keys):
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO identity VALUES(?,?,?)',
                           [(key, run_id, company) for key in keys])

    def record(self, event):
        if event.kind not in OBSERVED_EVENTS:
            raise ValueError('Неподдерживаемый вид наблюдения')
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM step JOIN run ON run.id=step.run_id WHERE role='commercial_researcher' AND subject=? AND run_id=? AND run.origin!='simulation'",
                              (event.company, event.run_id)).fetchone():
                raise ValueError('Компания отсутствует в указанном запуске')
            values = (event.run_id, event.kind, event.company, event.at.isoformat(),
                      event.event_id, event.variant, event.language, event.segment)
            cursor = db.execute('INSERT OR IGNORE INTO event(run_id,kind,subject,at,external_id,variant,language,segment) '
                                'VALUES(?,?,?,?,?,?,?,?)', values)
            if not cursor.rowcount:
                old = db.execute('SELECT run_id,kind,subject,at,external_id,variant,language,segment FROM event WHERE external_id=?',
                                 (event.event_id,)).fetchone()
                if not old or tuple(old) != values:
                    raise ValueError('Идентификатор уже связан с другим наблюдением')
            return bool(cursor.rowcount)

    def stats(self):
        with self.connect() as db:
            runs = [dict(row) for row in db.execute('SELECT id,origin,status,created FROM run ORDER BY id DESC LIMIT 30')]
            counts = dict(db.execute('SELECT kind,count(*) FROM event GROUP BY kind'))
            metrics = [dict(row) for row in db.execute(
                'SELECT variant,language,segment,kind,count(DISTINCT run_id || ":" || subject) AS n '
                'FROM event WHERE external_id IS NOT NULL GROUP BY variant,language,segment,kind')]
            stage_events = dict(db.execute('SELECT kind,count(*) FROM event JOIN run ON run.id=event.run_id '
                                           'WHERE run.origin!=? GROUP BY kind', ('simulation',)))
            simulations = db.execute('SELECT count(*) FROM event JOIN run ON run.id=event.run_id '
                                     'WHERE run.origin=? AND kind=?', ('simulation','ai_voice_sdr')).fetchone()[0]
            portfolio = []
            for row in db.execute("SELECT run_id,subject,value FROM step WHERE role='qualification_analyst' "
                                  'ORDER BY run_id DESC LIMIT 200'):
                result = json.loads(row['value'])
                portfolio.append({'run': row['run_id'], 'company': row['subject'],
                                  'group': result['status'], 'product': result.get('product')})
        return {'stopped': self.stopped(), 'runs': runs, 'events': counts, 'metrics': metrics, 'portfolio': portfolio, 'stage_events': stage_events, 'simulated_dialogues': simulations}
