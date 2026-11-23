"""SQLite persistence layer for Ads-Manager campaigns, audiences, leads, and dial queue."""

import json
from typing import Any, Iterable, Optional

from app.services.catalog import catalog_service_label
from app.services.sales_store import SalesStore, now

VERSION = "2.0.1"

ADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_audit(
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    subject TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS am_campaign(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    product_key TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT 'call_then_offer',
    lang TEXT NOT NULL DEFAULT 'kz',
    status TEXT NOT NULL DEFAULT 'active',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS am_audience(
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES am_campaign(id),
    name TEXT NOT NULL,
    segment TEXT NOT NULL,
    rules TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    auto_discovery INTEGER NOT NULL DEFAULT 1,
    run_requested INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    last_result TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS am_lead(
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES am_campaign(id),
    audience_id INTEGER NOT NULL REFERENCES am_audience(id),
    dedupe_key TEXT NOT NULL,
    company TEXT NOT NULL,
    bin TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    director TEXT NOT NULL DEFAULT '',
    phone_raw TEXT NOT NULL DEFAULT '',
    phone_e164 TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    contract_sum_kzt REAL,
    source TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    icp_score INTEGER NOT NULL DEFAULT 0,
    whitelisted INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',
    last_result TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE(campaign_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_am_lead_camp ON am_lead(campaign_id);
CREATE INDEX IF NOT EXISTS idx_am_lead_aud ON am_lead(audience_id);
CREATE TABLE IF NOT EXISTS am_dial_queue(
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES am_lead(id),
    campaign_id INTEGER NOT NULL REFERENCES am_campaign(id),
    lang TEXT NOT NULL,
    gap_seconds INTEGER NOT NULL DEFAULT 90,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT,
    not_before TEXT NOT NULL,
    created TEXT NOT NULL,
    finished TEXT
);
"""


def _unpack_json_fields(row: Any, keys: tuple[str, ...]) -> Optional[dict]:
    if row is None:
        return None
    data = dict(row)
    for k in keys:
        if data.get(k) is not None and isinstance(data[k], str):
            try:
                data[k] = json.loads(data[k])
            except json.JSONDecodeError:
                pass
    return data


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


class AdsStore:
    """Durable store for Ads-Manager campaigns, audience rule sets, leads, and dial queue."""

    def __init__(self, base: SalesStore):
        self.base = base
        with base.connect() as db:
            db.executescript(ADS_SCHEMA)

    def audit(self, action: str, subject: Any) -> None:
        with self.base.connect() as db:
            db.execute(
                "INSERT INTO cc_audit(action, subject, at) VALUES(?,?,?)",
                (action, str(subject), now().isoformat()),
            )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.base.connect() as db:
            row = db.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.base.connect() as db:
            db.execute(
                "INSERT INTO setting(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------
    # Campaigns
    # ------------------------------------------------------------------
    def list_campaigns(self) -> list[dict]:
        with self.base.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM am_campaign ORDER BY id DESC").fetchall()]
            aud_counts = dict(
                db.execute("SELECT campaign_id, COUNT(*) FROM am_audience GROUP BY campaign_id").fetchall()
            )
            lead_stats = {}
            for r in db.execute(
                """
                SELECT campaign_id,
                       COUNT(*) AS leads,
                       SUM(CASE WHEN status='called' THEN 1 ELSE 0 END) AS called,
                       SUM(CASE WHEN status='offer_sent' THEN 1 ELSE 0 END) AS offers,
                       SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued
                FROM am_lead GROUP BY campaign_id
                """
            ).fetchall():
                lead_stats[r["campaign_id"]] = dict(r)
        for c in rows:
            cid = c["id"]
            stats = lead_stats.get(cid, {})
            c["service"] = catalog_service_label(c["product_key"], c["lang"])
            c["audiences_count"] = aud_counts.get(cid, 0)
            c["leads_count"] = stats.get("leads", 0) or 0
            c["called_count"] = stats.get("called", 0) or 0
            c["offers_count"] = stats.get("offers", 0) or 0
            c["queued_count"] = stats.get("queued", 0) or 0
        return rows

    def get_campaign(self, campaign_id: int) -> Optional[dict]:
        with self.base.connect() as db:
            row = db.execute("SELECT * FROM am_campaign WHERE id=?", (campaign_id,)).fetchone()
        return dict(row) if row else None

    def create_campaign(self, name: str, product_key: str, objective: str, lang: str) -> dict:
        ts = now().isoformat()
        with self.base.connect() as db:
            cid = db.execute(
                "INSERT INTO am_campaign(name, product_key, objective, lang, status, created, updated) VALUES(?,?,?,?,?,?,?)",
                (name, product_key, objective, lang, "active", ts, ts),
            ).lastrowid
        self.audit("am_campaign_created", cid)
        return next(c for c in self.list_campaigns() if c["id"] == cid)

    def update_campaign(self, campaign_id: int, name: str, product_key: str, objective: str, lang: str) -> dict:
        ts = now().isoformat()
        with self.base.connect() as db:
            rc = db.execute(
                "UPDATE am_campaign SET name=?, product_key=?, objective=?, lang=?, updated=? WHERE id=?",
                (name, product_key, objective, lang, ts, campaign_id),
            ).rowcount
            if not rc:
                raise ValueError("campaign_not_found")
            new_service = catalog_service_label(product_key, lang)
            db.execute("UPDATE am_lead SET service=? WHERE campaign_id=?", (new_service, campaign_id))
        self.audit("am_campaign_updated", campaign_id)
        return next(c for c in self.list_campaigns() if c["id"] == campaign_id)

    def toggle_campaign(self, campaign_id: int, enabled: bool) -> dict:
        status = "active" if enabled else "paused"
        ts = now().isoformat()
        with self.base.connect() as db:
            rc = db.execute(
                "UPDATE am_campaign SET status=?, updated=? WHERE id=?",
                (status, ts, campaign_id),
            ).rowcount
            if not rc:
                raise ValueError("campaign_not_found")
        self.audit("am_campaign_toggled", f"{campaign_id}:{status}")
        return next(c for c in self.list_campaigns() if c["id"] == campaign_id)

    def duplicate_campaign(self, campaign_id: int) -> dict:
        src = self.get_campaign(campaign_id)
        if not src:
            raise ValueError("campaign_not_found")
        ts = now().isoformat()
        with self.base.connect() as db:
            new_id = db.execute(
                "INSERT INTO am_campaign(name, product_key, objective, lang, status, created, updated) VALUES(?,?,?,?,?,?,?)",
                (src["name"] + " (Copy)", src["product_key"], src["objective"], src["lang"], "paused", ts, ts),
            ).lastrowid
            audiences = db.execute("SELECT * FROM am_audience WHERE campaign_id=? ORDER BY id", (campaign_id,)).fetchall()
            for a in audiences:
                db.execute(
                    "INSERT INTO am_audience(campaign_id, name, segment, rules, status, auto_discovery, run_requested, last_run, last_result, created) VALUES(?,?,?,?,?,?,0,NULL,NULL,?)",
                    (new_id, a["name"], a["segment"], a["rules"], a["status"], a["auto_discovery"], ts),
                )
        self.audit("am_campaign_duplicated", f"{campaign_id}->{new_id}")
        return next(c for c in self.list_campaigns() if c["id"] == new_id)

    # ------------------------------------------------------------------
    # Audiences
    # ------------------------------------------------------------------
    def list_audiences(self, campaign_ids: Optional[list[int]] = None) -> list[dict]:
        query = """
            SELECT a.*, c.name AS campaign_name, c.product_key, c.lang AS campaign_lang, c.status AS campaign_status
            FROM am_audience a
            JOIN am_campaign c ON c.id = a.campaign_id
        """
        params: list[Any] = []
        if campaign_ids:
            query += f" WHERE a.campaign_id IN ({_placeholders(campaign_ids)})"
            params.extend(campaign_ids)
        query += " ORDER BY a.id DESC"
        with self.base.connect() as db:
            rows = [_unpack_json_fields(r, ("rules", "last_result")) for r in db.execute(query, params).fetchall()]
            lead_counts = dict(
                db.execute("SELECT audience_id, COUNT(*) FROM am_lead GROUP BY audience_id").fetchall()
            )
        for a in rows:
            a["leads_count"] = lead_counts.get(a["id"], 0)
            a["auto_discovery"] = bool(a["auto_discovery"])
            a["run_requested"] = bool(a["run_requested"])
        return rows

    def get_audience(self, audience_id: int) -> Optional[dict]:
        with self.base.connect() as db:
            row = db.execute("SELECT * FROM am_audience WHERE id=?", (audience_id,)).fetchone()
        data = _unpack_json_fields(row, ("rules", "last_result"))
        if data:
            data["auto_discovery"] = bool(data["auto_discovery"])
            data["run_requested"] = bool(data["run_requested"])
        return data

    def create_audience(
        self,
        campaign_id: int,
        name: str,
        segment: str,
        rules: dict,
        auto_discovery: bool = True,
    ) -> dict:
        if not self.get_campaign(campaign_id):
            raise ValueError("campaign_not_found")
        ts = now().isoformat()
        with self.base.connect() as db:
            aid = db.execute(
                "INSERT INTO am_audience(campaign_id, name, segment, rules, status, auto_discovery, run_requested, created) VALUES(?,?,?,?,?,?,0,?)",
                (campaign_id, name, segment, json.dumps(rules, ensure_ascii=False), "active", int(auto_discovery), ts),
            ).lastrowid
        self.audit("am_audience_created", aid)
        return next(a for a in self.list_audiences() if a["id"] == aid)

    def update_audience(
        self,
        audience_id: int,
        campaign_id: int,
        name: str,
        segment: str,
        rules: dict,
        auto_discovery: bool = True,
    ) -> dict:
        if not self.get_campaign(campaign_id):
            raise ValueError("campaign_not_found")
        with self.base.connect() as db:
            rc = db.execute(
                "UPDATE am_audience SET campaign_id=?, name=?, segment=?, rules=?, auto_discovery=? WHERE id=?",
                (campaign_id, name, segment, json.dumps(rules, ensure_ascii=False), int(auto_discovery), audience_id),
            ).rowcount
            if not rc:
                raise ValueError("audience_not_found")
        self.audit("am_audience_updated", audience_id)
        return next(a for a in self.list_audiences() if a["id"] == audience_id)

    def toggle_audience(self, audience_id: int, enabled: bool) -> dict:
        status = "active" if enabled else "paused"
        with self.base.connect() as db:
            rc = db.execute("UPDATE am_audience SET status=? WHERE id=?", (status, audience_id)).rowcount
            if not rc:
                raise ValueError("audience_not_found")
        self.audit("am_audience_toggled", f"{audience_id}:{status}")
        return next(a for a in self.list_audiences() if a["id"] == audience_id)

    def request_discovery(self, audience_id: int) -> dict:
        aud = self.get_audience(audience_id)
        if not aud:
            raise ValueError("audience_not_found")
        with self.base.connect() as db:
            db.execute("UPDATE am_audience SET run_requested=1 WHERE id=?", (audience_id,))
        self.audit("am_discovery_requested", audience_id)
        return next(a for a in self.list_audiences() if a["id"] == audience_id)

    def next_due_audience(self, stale_before_iso: str) -> Optional[dict]:
        with self.base.connect() as db:
            row = db.execute(
                """
                SELECT a.*
                FROM am_audience a
                JOIN am_campaign c ON c.id = a.campaign_id
                WHERE a.status = 'active'
                  AND c.status = 'active'
                  AND (a.run_requested = 1 OR (a.auto_discovery = 1 AND (a.last_run IS NULL OR a.last_run < ?)))
                ORDER BY a.run_requested DESC, COALESCE(a.last_run, '') ASC, a.id ASC
                LIMIT 1
                """,
                (stale_before_iso,),
            ).fetchone()
        return _unpack_json_fields(row, ("rules", "last_result"))

    def record_discovery_run(self, audience_id: int, result: dict) -> None:
        with self.base.connect() as db:
            db.execute(
                "UPDATE am_audience SET run_requested=0, last_run=?, last_result=? WHERE id=?",
                (now().isoformat(), json.dumps(result, ensure_ascii=False), audience_id),
            )
        self.audit("am_discovery_finished", f"{audience_id}:found={result.get('found',0)},new={result.get('new',0)}")

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------
    def upsert_leads(self, rows: list[dict]) -> dict[str, int]:
        if not rows:
            return {"inserted": 0, "updated": 0}
        ts = now().isoformat()
        inserted = 0
        updated = 0
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            camp_ids = sorted({r["campaign_id"] for r in rows})
            existing_keys = set()
            for r in db.execute(
                f"SELECT campaign_id, dedupe_key FROM am_lead WHERE campaign_id IN ({_placeholders(camp_ids)})",
                camp_ids,
            ).fetchall():
                existing_keys.add((r[0], r[1]))

            for r in rows:
                bin_clean = (r.get("bin") or "").strip()
                comp_clean = (r.get("company") or "").strip()
                if not comp_clean and not bin_clean:
                    continue
                dedupe_key = bin_clean if bin_clean else comp_clean.lower()
                is_existing = (r["campaign_id"], dedupe_key) in existing_keys
                existing_keys.add((r["campaign_id"], dedupe_key))
                db.execute(
                    """
                    INSERT INTO am_lead(
                        campaign_id, audience_id, dedupe_key, company, bin, city, category, service,
                        director, phone_raw, phone_e164, email, contract_sum_kzt, source, source_url,
                        icp_score, whitelisted, enabled, status, last_result, created, updated
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'new',NULL,?,?)
                    ON CONFLICT(campaign_id, dedupe_key) DO UPDATE SET
                        bin = CASE WHEN am_lead.bin='' THEN excluded.bin ELSE am_lead.bin END,
                        city = CASE WHEN am_lead.city='' THEN excluded.city ELSE am_lead.city END,
                        category = CASE WHEN am_lead.category='' THEN excluded.category ELSE am_lead.category END,
                        service = CASE WHEN am_lead.service='' THEN excluded.service ELSE am_lead.service END,
                        director = CASE WHEN am_lead.director='' THEN excluded.director ELSE am_lead.director END,
                        phone_raw = CASE WHEN am_lead.phone_raw='' THEN excluded.phone_raw ELSE am_lead.phone_raw END,
                        phone_e164 = CASE WHEN am_lead.phone_e164='' THEN excluded.phone_e164 ELSE am_lead.phone_e164 END,
                        email = CASE WHEN am_lead.email='' THEN excluded.email ELSE am_lead.email END,
                        contract_sum_kzt = COALESCE(am_lead.contract_sum_kzt, excluded.contract_sum_kzt),
                        source_url = CASE WHEN am_lead.source_url='' THEN excluded.source_url ELSE am_lead.source_url END,
                        icp_score = MAX(am_lead.icp_score, excluded.icp_score),
                        whitelisted = excluded.whitelisted,
                        updated = excluded.updated
                    """,
                    (
                        r["campaign_id"],
                        r["audience_id"],
                        dedupe_key,
                        comp_clean,
                        bin_clean,
                        (r.get("city") or "").strip(),
                        (r.get("category") or "").strip(),
                        (r.get("service") or "").strip(),
                        (r.get("director") or "").strip(),
                        (r.get("phone_raw") or "").strip(),
                        (r.get("phone_e164") or "").strip(),
                        (r.get("email") or "").strip(),
                        r.get("contract_sum_kzt"),
                        (r.get("source") or "").strip(),
                        (r.get("source_url") or "").strip(),
                        int(r.get("icp_score") or 0),
                        int(bool(r.get("whitelisted"))),
                        ts,
                        ts,
                    ),
                )
                if is_existing:
                    updated += 1
                else:
                    inserted += 1
        return {"inserted": inserted, "updated": updated}

    def refresh_whitelist_flags(self, allowed_numbers: set[str]) -> None:
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id, phone_e164, whitelisted FROM am_lead").fetchall()
            for r in rows:
                wl = 1 if (r["phone_e164"] and r["phone_e164"] in allowed_numbers) else 0
                if wl != r["whitelisted"]:
                    db.execute("UPDATE am_lead SET whitelisted=? WHERE id=?", (wl, r["id"]))

    def list_leads(
        self,
        campaign_ids: Optional[list[int]] = None,
        audience_ids: Optional[list[int]] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        where = ["1=1"]
        params: list[Any] = []
        if audience_ids:
            where.append(f"l.audience_id IN ({_placeholders(audience_ids)})")
            params.extend(audience_ids)
        elif campaign_ids:
            where.append(f"l.campaign_id IN ({_placeholders(campaign_ids)})")
            params.extend(campaign_ids)
        if status and status != "all":
            where.append("l.status = ?")
            params.append(status)
        if q and q.strip():
            like = f"%{q.strip()}%"
            where.append("(l.company LIKE ? OR l.bin LIKE ? OR l.phone_e164 LIKE ? OR l.phone_raw LIKE ? OR l.category LIKE ?)")
            params.extend([like, like, like, like, like])

        where_sql = " AND ".join(where)
        base_sql = f"""
            FROM am_lead l
            JOIN am_campaign c ON c.id = l.campaign_id
            JOIN am_audience a ON a.id = l.audience_id
            WHERE {where_sql}
        """
        with self.base.connect() as db:
            total = db.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()[0]
            rows = [
                dict(r)
                for r in db.execute(
                    f"""
                    SELECT l.*, c.name AS campaign_name, c.product_key, c.objective, c.lang AS campaign_lang,
                           c.status AS campaign_status, a.name AS audience_name, a.segment
                    {base_sql}
                    ORDER BY l.whitelisted DESC, l.icp_score DESC, l.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, limit, offset],
                ).fetchall()
            ]
        for r in rows:
            r["enabled"] = bool(r["enabled"])
            r["whitelisted"] = bool(r["whitelisted"])
        return {"items": rows, "total": total, "limit": limit, "offset": offset}

    def get_lead(self, lead_id: int) -> Optional[dict]:
        with self.base.connect() as db:
            row = db.execute(
                """
                SELECT l.*, c.name AS campaign_name, c.product_key, c.objective, c.lang AS campaign_lang,
                       c.status AS campaign_status, a.name AS audience_name, a.segment
                FROM am_lead l
                JOIN am_campaign c ON c.id = l.campaign_id
                JOIN am_audience a ON a.id = l.audience_id
                WHERE l.id=?
                """,
                (lead_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["whitelisted"] = bool(data["whitelisted"])
        return data

    def toggle_lead(self, lead_id: int, enabled: bool) -> dict:
        with self.base.connect() as db:
            rc = db.execute(
                "UPDATE am_lead SET enabled=?, updated=? WHERE id=?",
                (int(enabled), now().isoformat(), lead_id),
            ).rowcount
            if not rc:
                raise ValueError("lead_not_found")
        self.audit("am_lead_toggled", f"{lead_id}:{int(enabled)}")
        return self.get_lead(lead_id)

    def update_lead_status(self, lead_id: int, status: str, last_result: Optional[str] = None) -> None:
        with self.base.connect() as db:
            db.execute(
                "UPDATE am_lead SET status=?, last_result=?, updated=? WHERE id=?",
                (status, last_result, now().isoformat(), lead_id),
            )

    # ------------------------------------------------------------------
    # Dial Queue
    # ------------------------------------------------------------------
    def enqueue(self, lead_ids: list[int], override_lang: Optional[str], gap_seconds: int, start_at=None) -> dict[str, Any]:
        from datetime import timedelta

        base_dt = start_at or now()
        queued = []
        skipped = []
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            offset_idx = 0
            for lid in lead_ids:
                lead = db.execute(
                    """
                    SELECT l.*, c.lang AS campaign_lang, c.status AS campaign_status
                    FROM am_lead l JOIN am_campaign c ON c.id = l.campaign_id
                    WHERE l.id=?
                    """,
                    (lid,),
                ).fetchone()
                if not lead:
                    skipped.append({"id": lid, "reason": "lead_not_found"})
                    continue
                if not lead["enabled"]:
                    skipped.append({"id": lid, "reason": "lead_disabled"})
                    continue
                if lead["campaign_status"] != "active":
                    skipped.append({"id": lid, "reason": "campaign_paused"})
                    continue
                if not lead["phone_e164"]:
                    skipped.append({"id": lid, "reason": "missing_phone"})
                    continue
                if not lead["whitelisted"]:
                    skipped.append({"id": lid, "reason": "not_whitelisted"})
                    continue
                already = db.execute(
                    "SELECT 1 FROM am_dial_queue WHERE lead_id=? AND status IN ('queued','running')",
                    (lid,),
                ).fetchone()
                if already:
                    skipped.append({"id": lid, "reason": "already_queued"})
                    continue

                due_dt = base_dt + timedelta(seconds=offset_idx * gap_seconds)
                lang = override_lang or lead["campaign_lang"] or "kz"
                qid = db.execute(
                    """
                    INSERT INTO am_dial_queue(lead_id, campaign_id, lang, gap_seconds, status, not_before, created)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (lid, lead["campaign_id"], lang, gap_seconds, "queued", due_dt.isoformat(), base_dt.isoformat()),
                ).lastrowid
                db.execute(
                    "UPDATE am_lead SET status='queued', updated=? WHERE id=?",
                    (base_dt.isoformat(), lid),
                )
                queued.append(qid)
                offset_idx += 1

        self.audit("am_queue_enqueued", f"queued={len(queued)},skipped={len(skipped)}")
        return {"queued": len(queued), "queue_ids": queued, "skipped": skipped}

    def next_due_queue_item(self, now_iso: str) -> Optional[dict]:
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT q.*
                FROM am_dial_queue q
                WHERE q.status='queued' AND q.not_before <= ?
                ORDER BY q.not_before ASC, q.id ASC
                LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE am_dial_queue SET status='running' WHERE id=?", (row["id"],))
            return dict(row)

    def finish_queue_item(
        self,
        queue_id: int,
        status: str,
        result: str,
        finished_iso: str,
        next_not_before_iso: Optional[str] = None,
    ) -> None:
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE am_dial_queue SET status=?, result=?, finished=? WHERE id=?",
                (status, result, finished_iso, queue_id),
            )
            if next_not_before_iso:
                nxt = db.execute(
                    "SELECT id, not_before FROM am_dial_queue WHERE status='queued' ORDER BY not_before ASC, id ASC LIMIT 1"
                ).fetchone()
                if nxt and nxt["not_before"] < next_not_before_iso:
                    db.execute("UPDATE am_dial_queue SET not_before=? WHERE id=?", (next_not_before_iso, nxt["id"]))

    def count_calls_today(self, date_prefix: str) -> int:
        with self.base.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM am_dial_queue WHERE status IN ('done','failed') AND substr(finished,1,10)=?",
                (date_prefix,),
            ).fetchone()
        return row[0] if row else 0

    def queue_snapshot(self, limit: int = 100) -> dict[str, Any]:
        paused = self.get_setting("am_queue_paused", "0") == "1"
        with self.base.connect() as db:
            counts = dict(db.execute("SELECT status, COUNT(*) FROM am_dial_queue GROUP BY status").fetchall())
            items = [
                dict(r)
                for r in db.execute(
                    """
                    SELECT q.*, l.company, l.phone_e164, l.service, c.name AS campaign_name
                    FROM am_dial_queue q
                    JOIN am_lead l ON l.id = q.lead_id
                    JOIN am_campaign c ON c.id = q.campaign_id
                    ORDER BY q.id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
        return {"paused": paused, "counts": counts, "items": items}

    def clear_queue(self) -> int:
        ts = now().isoformat()
        with self.base.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lead_ids = [r[0] for r in db.execute("SELECT lead_id FROM am_dial_queue WHERE status='queued'").fetchall()]
            rc = db.execute(
                "UPDATE am_dial_queue SET status='cancelled', result='cleared_by_operator', finished=? WHERE status='queued'",
                (ts,),
            ).rowcount
            if lead_ids:
                db.execute(
                    f"UPDATE am_lead SET status='new', updated=? WHERE id IN ({_placeholders(lead_ids)}) AND status='queued'",
                    [ts, *lead_ids],
                )
        self.audit("am_queue_cleared", rc)
        return rc
