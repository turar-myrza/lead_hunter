"""Additive control-center tables in the existing SalesStore database."""
import json
from app.services.sales_store import SalesStore, now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS cc_product(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_job(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES cc_product(id), revision INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', result TEXT, error TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_segment(id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES cc_job(id), product_id INTEGER NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cc_review(job_id INTEGER NOT NULL REFERENCES cc_job(id), candidate_id TEXT NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(job_id,candidate_id));
CREATE TABLE IF NOT EXISTS cc_audit(id INTEGER PRIMARY KEY, action TEXT NOT NULL, subject TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_usage(id INTEGER PRIMARY KEY, operation TEXT NOT NULL, status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_login(ip TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cc_handoff(job_id INTEGER NOT NULL, candidate_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id,candidate_id));
CREATE TABLE IF NOT EXISTS cc_outreach_call(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, product TEXT NOT NULL, lang TEXT NOT NULL, status TEXT NOT NULL, response TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cc_outreach_offer(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, product TEXT NOT NULL, lang TEXT NOT NULL, template TEXT NOT NULL, pdf_url TEXT, status TEXT NOT NULL, wamid TEXT, error TEXT, at TEXT NOT NULL);
'''

def unpack(row):
    if row is None: return None
    value = dict(row)
    for key in ('payload', 'result'):
        if value.get(key) is not None: value[key] = json.loads(value[key])
    return value

class ControlStore:
    def __init__(self, store: SalesStore):
        self.base = store
        with store.connect() as db: db.executescript(SCHEMA)

    def audit(self, action, subject):
        with self.base.connect() as db:
            db.execute('INSERT INTO cc_audit(action,subject,at) VALUES(?,?,?)',(action,str(subject),now().isoformat()))

    def product(self, ident):
        with self.base.connect() as db: return unpack(db.execute('SELECT * FROM cc_product WHERE id=?',(ident,)).fetchone())

    def save_product(self, payload, ident=None):
        with self.base.connect() as db:
            if ident is None:
                ident=db.execute('INSERT INTO cc_product(payload,updated) VALUES(?,?)',(json.dumps(payload),now().isoformat())).lastrowid
            else:
                if not db.execute('UPDATE cc_product SET payload=?,revision=revision+1,updated=? WHERE id=?',
                    (json.dumps(payload),now().isoformat(),ident)).rowcount: raise ValueError('product_not_found')
                db.execute('UPDATE cc_segment SET approved=0 WHERE product_id=?',(ident,))
        self.audit('product_saved',ident)
        return self.product(ident)

    def job(self, ident):
        with self.base.connect() as db: return unpack(db.execute('SELECT * FROM cc_job WHERE id=?',(ident,)).fetchone())

    def segment(self, ident):
        with self.base.connect() as db: return unpack(db.execute('SELECT * FROM cc_segment WHERE id=?',(ident,)).fetchone())

    def enqueue(self, kind, product, payload):
        with self.base.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM cc_job WHERE product_id=? AND status IN ('queued','running')",(product['id'],)).fetchone():
                raise ValueError('product_job_already_active')
            ident=db.execute('INSERT INTO cc_job(kind,product_id,revision,payload,created) VALUES(?,?,?,?,?)',
                (kind,product['id'],product['revision'],json.dumps(payload),now().isoformat())).lastrowid
        self.audit(kind+'_queued',ident)
        return self.job(ident)

    def finish(self, ident, status, result=None, error=None):
        with self.base.connect() as db:
            db.execute('UPDATE cc_job SET status=?,result=?,error=? WHERE id=?',
                       (status,json.dumps(result) if result is not None else None,error,ident))
        self.audit('job_'+status,ident)

    def log_outreach_call(self, phone: str, product: str, lang: str, status: str, response: str = ""):
        with self.base.connect() as db:
            db.execute('INSERT INTO cc_outreach_call(phone, product, lang, status, response, at) VALUES(?,?,?,?,?,?)',
                       (phone, product, lang, status, str(response), now().isoformat()))
        self.audit('outreach_call', f"{phone}:{product}:{lang}:{status}")

    def log_outreach_offer(self, phone: str, product: str, lang: str, template: str, pdf_url: str, status: str, wamid: str = "", error: str = ""):
        with self.base.connect() as db:
            db.execute('INSERT INTO cc_outreach_offer(phone, product, lang, template, pdf_url, status, wamid, error, at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (phone, product, lang, template, pdf_url, status, wamid, error, now().isoformat()))
        self.audit('outreach_offer', f"{phone}:{product}:{lang}:{status}")

    def outreach_history(self, limit: int = 50):
        with self.base.connect() as db:
            calls = [dict(r) for r in db.execute('SELECT * FROM cc_outreach_call ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]
            offers = [dict(r) for r in db.execute('SELECT * FROM cc_outreach_offer ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]
        return {"calls": calls, "offers": offers}

    def snapshot(self):
        with self.base.connect() as db:
            products=[unpack(r) for r in db.execute('SELECT * FROM cc_product ORDER BY id DESC LIMIT 200')]
            jobs=[unpack(r) for r in db.execute('SELECT * FROM cc_job ORDER BY id DESC LIMIT 100')]
            segments=[unpack(r) for r in db.execute('SELECT * FROM cc_segment ORDER BY id DESC LIMIT 500')]
            reviews=[unpack(r) for r in db.execute('SELECT * FROM cc_review ORDER BY updated DESC LIMIT 1000')]
            audit=[dict(r) for r in db.execute('SELECT * FROM cc_audit ORDER BY id DESC LIMIT 100')]
            usage=[dict(r) for r in db.execute('SELECT * FROM cc_usage ORDER BY id DESC LIMIT 100')]
            totals=dict(db.execute('SELECT status,count(*) FROM cc_job GROUP BY status').fetchall())
            handoffs=[unpack(r) for r in db.execute('SELECT * FROM cc_handoff ORDER BY job_id DESC LIMIT 500')]
            heartbeat=db.execute("SELECT value FROM setting WHERE key='control_worker_heartbeat'").fetchone()
            outreach=self.outreach_history(30)
        return dict(products=products,jobs=jobs,segments=segments,reviews=reviews,audit=audit,usage=usage,job_totals=totals,handoffs=handoffs,worker_heartbeat=heartbeat[0] if heartbeat else None,outreach=outreach)
