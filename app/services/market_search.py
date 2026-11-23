"""Ultra-fast SQLite FTS5 (BM25) Market Seed Search & Audience Volume Estimator."""

from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from app.services.sales_store import SalesStore, now

VERSION = "2.9.1"

MARKET_FTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS am_market_seeds(
    id INTEGER PRIMARY KEY,
    bin TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT 'all',
    category TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    contract_sum_kzt REAL NOT NULL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'seed',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_am_seeds_region ON am_market_seeds(region);
CREATE INDEX IF NOT EXISTS idx_am_seeds_budget ON am_market_seeds(contract_sum_kzt);

CREATE VIRTUAL TABLE IF NOT EXISTS am_market_seeds_fts USING fts5(
    company, category, activity, city, region, bin,
    content='am_market_seeds',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS am_market_seeds_ai AFTER INSERT ON am_market_seeds BEGIN
    INSERT INTO am_market_seeds_fts(rowid, company, category, activity, city, region, bin)
    VALUES (new.id, new.company, new.category, new.activity, new.city, new.region, new.bin);
END;

CREATE TRIGGER IF NOT EXISTS am_market_seeds_ad AFTER DELETE ON am_market_seeds BEGIN
    INSERT INTO am_market_seeds_fts(am_market_seeds_fts, rowid, company, category, activity, city, region, bin)
    VALUES ('delete', old.id, old.company, old.category, old.activity, old.city, old.region, old.bin);
END;

CREATE TRIGGER IF NOT EXISTS am_market_seeds_au AFTER UPDATE ON am_market_seeds BEGIN
    INSERT INTO am_market_seeds_fts(am_market_seeds_fts, rowid, company, category, activity, city, region, bin)
    VALUES ('delete', old.id, old.company, old.category, old.activity, old.city, old.region, old.bin);
    INSERT INTO am_market_seeds_fts(rowid, company, category, activity, city, region, bin)
    VALUES (new.id, new.company, new.category, new.activity, new.city, new.region, new.bin);
END;
"""


VALID_REGIONS = {"almaty", "astana", "shymkent", "karaganda", "aktobe", "atyrau"}


def _normalize_region(city_or_region: str) -> str:
    txt = (city_or_region or "").lower()
    if any(k in txt for k in ("almaty", "алмат")):
        return "almaty"
    if any(k in txt for k in ("astana", "астан", "nur-sultan", "нур-султан", "нұр-сұлтан")):
        return "astana"
    if any(k in txt for k in ("shymkent", "шымкент", "чимкент")):
        return "shymkent"
    if any(k in txt for k in ("karaganda", "караганд", "қарағанд")):
        return "karaganda"
    if any(k in txt for k in ("aktobe", "актоб", "ақтөб")):
        return "aktobe"
    if any(k in txt for k in ("atyrau", "атырау")):
        return "atyrau"
    return "all"


def sanitize_fts5_query(raw_query: str) -> str:
    """Convert arbitrary user input into a safe FTS5 prefix query, preventing FTS5 syntax errors & injection."""
    tokens = re.findall(r"[\w\-]+", (raw_query or "").strip(), flags=re.UNICODE)
    # Filter out standalone FTS5 boolean keywords if typed maliciously or accidentally
    safe_tokens = [f'"{t}"*' for t in tokens if t and t.upper() not in {"AND", "OR", "NOT", "NEAR"}]
    return " ".join(safe_tokens[:12])


class MarketSearchEngine:
    """Parameterized SQLite FTS5 BM25 search engine over B2B/B2G procurement and registry seeds."""

    def __init__(self, base: SalesStore):
        self.base = base
        with base.connect() as db:
            db.executescript(MARKET_FTS_SCHEMA)

    def count_indexed(self) -> int:
        with self.base.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM am_market_seeds").fetchone()[0])

    def bulk_insert_seeds(self, records: Iterable[dict[str, Any]]) -> int:
        ts = now().isoformat()
        rows = []
        for r in records:
            company = str(r.get("company") or "").strip()
            if not company:
                continue
            city = str(r.get("city") or "").strip()
            region = str(r.get("region") or "").strip().lower()
            if not region or region not in (VALID_REGIONS | {"all"}):
                region = _normalize_region(city)
            try:
                budget = float(r.get("contract_sum_kzt") or 0.0)
            except (ValueError, TypeError):
                budget = 0.0
            rows.append((
                str(r.get("bin") or "").strip()[:24],
                company[:250],
                city[:120],
                region,
                str(r.get("category") or "").strip()[:160],
                str(r.get("activity") or "").strip()[:400],
                str(r.get("phone") or "").strip()[:60],
                str(r.get("email") or "").strip()[:120],
                budget,
                str(r.get("source") or "seed").strip()[:60],
                ts,
            ))
        if not rows:
            return 0
        with self.base.connect() as db:
            db.executemany(
                """
                INSERT INTO am_market_seeds(
                    bin, company, city, region, category, activity,
                    phone, email, contract_sum_kzt, source, created
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        return len(rows)

    def upsert_seeds(self, records: Iterable[dict[str, Any]]) -> int:
        """Insert or update market seeds while preventing duplicate entries by BIN or (company, source)."""
        ts = now().isoformat()
        incoming = list(records)
        if not incoming:
            return 0
        added = 0
        with self.base.connect() as db:
            for r in incoming:
                company = str(r.get("company") or "").strip()[:250]
                if not company:
                    continue
                bin_val = str(r.get("bin") or "").strip()[:24]
                city = str(r.get("city") or "").strip()[:120]
                region = str(r.get("region") or "").strip().lower()
                if not region or region not in (VALID_REGIONS | {"all"}):
                    region = _normalize_region(city)
                category = str(r.get("category") or "").strip()[:160]
                activity = str(r.get("activity") or "").strip()[:400]
                phone = str(r.get("phone") or "").strip()[:60]
                email = str(r.get("email") or "").strip()[:120]
                source = str(r.get("source") or "seed").strip()[:60]
                try:
                    budget = float(r.get("contract_sum_kzt") or 0.0)
                except (ValueError, TypeError):
                    budget = 0.0

                existing = None
                if bin_val:
                    existing = db.execute("SELECT id, phone, email, activity FROM am_market_seeds WHERE bin=? LIMIT 1", (bin_val,)).fetchone()
                if not existing:
                    existing = db.execute(
                        "SELECT id, phone, email, activity FROM am_market_seeds WHERE lower(company)=lower(?) AND source=? LIMIT 1",
                        (company, source),
                    ).fetchone()

                if existing:
                    db.execute(
                        """
                        UPDATE am_market_seeds
                        SET bin = CASE WHEN bin='' THEN ? ELSE bin END,
                            city = CASE WHEN city='' THEN ? ELSE city END,
                            region = CASE WHEN region='all' THEN ? ELSE region END,
                            phone = CASE WHEN phone='' THEN ? ELSE phone END,
                            email = CASE WHEN email='' THEN ? ELSE email END,
                            activity = CASE WHEN activity='' THEN ? ELSE activity END,
                            contract_sum_kzt = MAX(contract_sum_kzt, ?)
                        WHERE id=?
                        """,
                        (bin_val, city, region, phone, email, activity, budget, existing["id"]),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO am_market_seeds(
                            bin, company, city, region, category, activity,
                            phone, email, contract_sum_kzt, source, created
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (bin_val, company, city, region, category, activity, phone, email, budget, source, ts),
                    )
                    added += 1
        return added

    def index_csv_seeds(self, data_dir: Path) -> int:
        """Load CSV seed files from data_dir into am_market_seeds if not already indexed."""
        if not data_dir.exists():
            return 0
        existing = self.count_indexed()
        if existing > 0:
            return existing

        loaded_records: list[dict[str, Any]] = []
        for csv_path in sorted(data_dir.glob("*.csv")):
            if "_semicolon" in csv_path.name:
                continue
            try:
                with csv_path.open("r", encoding="utf-8-sig", errors="replace") as fh:
                    sample = fh.read(2048)
                    fh.seek(0)
                    delim = ";" if sample.count(";") > sample.count(",") else ","
                    reader = csv.DictReader(fh, delimiter=delim)
                    for row in reader:
                        norm = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
                        comp = (
                            norm.get("company")
                            or norm.get("name")
                            or norm.get("supplier_name")
                            or norm.get("organization")
                            or norm.get("наименование")
                            or ""
                        )
                        if not comp:
                            continue
                        loaded_records.append({
                            "bin": norm.get("bin") or norm.get("iin_bin") or "",
                            "company": comp,
                            "city": norm.get("city") or norm.get("region") or "",
                            "category": norm.get("category") or norm.get("segment") or "B2B/B2G",
                            "activity": norm.get("activity") or norm.get("product") or norm.get("subject") or norm.get("note") or "",
                            "phone": norm.get("phone") or norm.get("phone_raw") or norm.get("mobile") or "",
                            "email": norm.get("email") or "",
                            "contract_sum_kzt": norm.get("contract_sum_kzt") or norm.get("sum") or 0.0,
                            "source": csv_path.stem,
                        })
            except Exception:
                continue

        return self.bulk_insert_seeds(loaded_records)

    def search(
        self,
        query: str = "",
        region: str = "",
        min_budget_kzt: float = 0.0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Execute parameterized FTS5 BM25 search with latency measurement."""
        t0 = time.monotonic()
        safe_limit = max(1, min(int(limit), 200))
        fts_expr = sanitize_fts5_query(query)
        norm_region = (region or "").strip().lower()
        if norm_region == "all":
            norm_region = ""

        params: list[Any] = []
        where_clauses: list[str] = []

        if fts_expr:
            sql_base = (
                "SELECT s.id, s.bin, s.company, s.city, s.region, s.category, "
                "s.activity, s.phone, s.email, s.contract_sum_kzt, s.source, "
                "bm25(am_market_seeds_fts) AS rank "
                "FROM am_market_seeds_fts f "
                "JOIN am_market_seeds s ON s.id = f.rowid "
            )
            where_clauses.append("am_market_seeds_fts MATCH ?")
            params.append(fts_expr)
            order_by = "ORDER BY rank ASC, s.contract_sum_kzt DESC"
        else:
            sql_base = (
                "SELECT s.id, s.bin, s.company, s.city, s.region, s.category, "
                "s.activity, s.phone, s.email, s.contract_sum_kzt, s.source, "
                "0.0 AS rank "
                "FROM am_market_seeds s "
            )
            order_by = "ORDER BY s.contract_sum_kzt DESC, s.id DESC"

        if norm_region in VALID_REGIONS:
            where_clauses.append("s.region = ?")
            params.append(norm_region)

        if min_budget_kzt and float(min_budget_kzt) > 0:
            where_clauses.append("s.contract_sum_kzt >= ?")
            params.append(float(min_budget_kzt))

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        full_sql = f"{sql_base} {where_sql} {order_by} LIMIT ?"
        params.append(safe_limit)

        with self.base.connect() as db:
            rows = [dict(r) for r in db.execute(full_sql, tuple(params)).fetchall()]
            total_indexed = int(db.execute("SELECT COUNT(*) FROM am_market_seeds").fetchone()[0])

        query_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return {
            "items": rows,
            "count": len(rows),
            "total_indexed": total_indexed,
            "query_ms": query_ms,
            "fts_query": fts_expr,
        }

    def estimate_segment_volume(self, keywords: list[str], regions: Optional[list[str]] = None) -> int:
        """Count matching seeds for a list of AI-suggested segment keywords."""
        safe_terms = [sanitize_fts5_query(kw) for kw in (keywords or []) if kw]
        safe_terms = [t for t in safe_terms if t]
        if not safe_terms:
            return 0
        fts_or = " OR ".join(f"({t})" for t in safe_terms[:5])
        params: list[Any] = [fts_or]
        region_sql = ""
        clean_regions = [r for r in (regions or []) if r in VALID_REGIONS]
        if clean_regions:
            placeholders = ",".join("?" for _ in clean_regions)
            region_sql = f" AND s.region IN ({placeholders})"
            params.extend(clean_regions)
        sql = (
            "SELECT COUNT(*) FROM am_market_seeds_fts f "
            "JOIN am_market_seeds s ON s.id = f.rowid "
            f"WHERE am_market_seeds_fts MATCH ?{region_sql}"
        )
        with self.base.connect() as db:
            return int(db.execute(sql, tuple(params)).fetchone()[0])
