"""Ads-Manager discovery adapters, whitelist cache, offer helper, and gated dialer worker."""

import asyncio
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

from app.core.config import Settings
from app.models.ads_manager import AudienceRules, CITY_ALIASES, KATO_MAP
from app.services.ads_store import AdsStore
from app.services.catalog import catalog_service_label, resolve_catalog_item
from app.services.control_store import ControlStore
from app.services.sales_store import SalesStore, now

VERSION = "2.9.1"

KZ_LIVE_SOURCES = {"satu_kz", "uchet_pk", "twogis", "egov_open_data", "statsnet", "e_ondiris"}


def _run_coro_sync(coro):
    """Execute an async coroutine safely from synchronous code, even if a loop is active in caller thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

ALMATY_TZ = ZoneInfo("Asia/Almaty")
MOBILE_PREFIX_RE = re.compile(r"^\+7(?:70\d|747|75\d|76\d|77[15678]|78\d)")
PHONE_CHUNK_RE = re.compile(r"(?<!\d)(?:\+?7|8)(?:[\s\-().]*\d){10}(?!\d)|(?<!\d)7(?:[\s\-().]*\d){9}(?!\d)")


class SourceUnavailable(RuntimeError):
    """Raised when an external discovery source cannot be queried cleanly."""


def normalize_kz_phones(raw: str) -> list[str]:
    """Extract and normalize Kazakhstan phone numbers into E.164, ranking mobiles before landlines."""
    if not raw:
        return []
    seen: list[str] = []
    for match in PHONE_CHUNK_RE.finditer(str(raw)):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) == 11 and digits[0] in ("7", "8"):
            norm = "+7" + digits[1:]
        elif len(digits) == 10 and digits[0] == "7":
            norm = "+7" + digits
        else:
            continue
        if norm not in seen:
            seen.append(norm)
    mobiles = [p for p in seen if MOBILE_PREFIX_RE.match(p)]
    landlines = [p for p in seen if not MOBILE_PREFIX_RE.match(p)]
    return mobiles + landlines


def parse_contract_sum(raw: Any) -> Optional[float]:
    """Parse formatted contract amount strings like '184 500.00' into float."""
    if raw is None or isinstance(raw, (int, float)):
        return float(raw) if raw is not None else None
    text = str(raw).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def compute_icp_score(lead: dict, rules: AudienceRules) -> int:
    """Score evidence completeness and rule fit on a 0-100 scale."""
    score = 0
    phones = normalize_kz_phones(lead.get("phone_raw") or lead.get("phone_e164") or "")
    if phones:
        if any(MOBILE_PREFIX_RE.match(p) for p in phones):
            score += 30
        else:
            score += 10
    if (lead.get("director") or "").strip():
        score += 20
    bin_digits = re.sub(r"\D", "", lead.get("bin") or "")
    if len(bin_digits) == 12:
        score += 15
    sum_val = lead.get("contract_sum_kzt")
    if rules.min_contract_kzt is None and rules.max_contract_kzt is None:
        score += 15
    elif sum_val is not None:
        ok_min = rules.min_contract_kzt is None or sum_val >= rules.min_contract_kzt
        ok_max = rules.max_contract_kzt is None or sum_val <= rules.max_contract_kzt
        if ok_min and ok_max:
            score += 15
    if rules.keywords:
        haystack = ((lead.get("category") or "") + " " + (lead.get("company") or "")).casefold()
        if any(kw.casefold() in haystack for kw in rules.keywords):
            score += 20
    return max(0, min(100, score))


def _matches_regions(city: str, address: str, regions: list[str]) -> bool:
    if not regions or "all" in regions:
        return True
    haystack = f"{city} {address}".casefold()
    for reg in regions:
        for alias in CITY_ALIASES.get(reg, (reg,)):
            if alias in haystack:
                return True
    return False


def _matches_keywords(company: str, category: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{company} {category}".casefold()
    return any(kw.casefold() in haystack for kw in keywords)


def _matches_sum(sum_val: Optional[float], rules: AudienceRules) -> bool:
    if rules.min_contract_kzt is None and rules.max_contract_kzt is None:
        return True
    if sum_val is None:
        return False
    if rules.min_contract_kzt is not None and sum_val < rules.min_contract_kzt:
        return False
    if rules.max_contract_kzt is not None and sum_val > rules.max_contract_kzt:
        return False
    return True


def is_dial_window(now_dt: Optional[datetime] = None) -> bool:
    """Return True if now_dt is within Mon-Fri 09:00-19:00 Asia/Almaty."""
    dt = now_dt or now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ALMATY_TZ)
    return local.weekday() < 5 and 9 <= local.hour < 19


def dispatch_offer(
    phone: str,
    catalog_item_or_key: Any,
    lang: str,
    settings: Settings,
    store: Optional[ControlStore] = None,
) -> dict[str, Any]:
    """Send approved Meta template 'commercial_offer_pdf_doc' with attached catalog PDF and log to control store."""
    from integrations.whatsapp_sender import send_whatsapp_template

    matched = (
        catalog_item_or_key
        if isinstance(catalog_item_or_key, dict)
        else resolve_catalog_item(str(catalog_item_or_key))
    )
    meta_lang = "kk" if lang == "kz" else "ru"
    doc_url = matched["pdf_kz"] if meta_lang == "kk" else matched["pdf_ru"]
    prod_name = matched["name"] if meta_lang == "kk" else matched["name_ru"]
    doc_title = prod_name + ".pdf"

    components = [
        {
            "type": "header",
            "parameters": [
                {
                    "type": "document",
                    "document": {
                        "link": doc_url,
                        "filename": doc_title,
                    },
                }
            ],
        },
        {
            "type": "body",
            "parameters": [{"type": "text", "text": prod_name}],
        },
    ]

    ctrl = store or ControlStore(SalesStore(settings.lead_hunter_db))
    try:
        res = send_whatsapp_template(
            to_phone=phone,
            template_name="commercial_offer_pdf_doc",
            language_code=meta_lang,
            components=components,
            phone_number_id=settings.meta_phone_id,
            access_token=settings.meta_access_token.get_secret_value() if settings.meta_access_token else None,
        )
        wamid = res.get("messages", [{}])[0].get("id", "sent")
        ctrl.log_outreach_offer(phone, prod_name, lang, "commercial_offer_pdf_doc", doc_url, "sent", wamid)
        return {"status": "sent", "wamid": wamid, "phone": phone, "pdf_url": doc_url, "product": prod_name}
    except Exception as exc:
        ctrl.log_outreach_offer(phone, prod_name, lang, "commercial_offer_pdf_doc", doc_url, "failed", "", str(exc))
        raise


def _load_csv_seed(rules: AudienceRules, seed_dir: Path) -> list[dict]:
    """Read local UTF-8 CSV seed files and filter rows by audience rules."""
    if not seed_dir.exists() or not seed_dir.is_dir():
        return []
    results = []
    for path in sorted(seed_dir.glob("*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                company = (row.get("company_name") or row.get("company") or row.get("name") or "").strip()
                bin_iin = re.sub(r"\D", "", row.get("bin_iin") or row.get("bin") or "")
                city = (row.get("city") or "").strip()
                address = (row.get("address") or "").strip()
                category = (row.get("sample_item") or row.get("category") or "").strip()
                director = (row.get("director_name") or row.get("contact_person") or row.get("director") or "").strip()
                phone_raw = (
                    row.get("best_phone_to_call")
                    or row.get("real_contract_phone")
                    or row.get("default_profile_phone")
                    or row.get("phone")
                    or ""
                ).strip()
                email = (row.get("email") or "").strip()
                sum_val = parse_contract_sum(row.get("contract_sum_kzt"))
                source_url = (row.get("anno_url") or row.get("contract_pdf_url") or "").strip()
                if not company and not bin_iin:
                    continue
                if not _matches_regions(city, address, rules.regions):
                    continue
                if not _matches_keywords(company, category, rules.keywords):
                    continue
                if not _matches_sum(sum_val, rules):
                    continue
                phones = normalize_kz_phones(phone_raw)
                if rules.require_phone and not phones:
                    continue
                results.append(
                    {
                        "company": company or bin_iin,
                        "bin": bin_iin,
                        "city": city,
                        "category": category,
                        "director": director,
                        "phone_raw": phone_raw,
                        "email": email,
                        "contract_sum_kzt": sum_val,
                        "source": "csv_seed",
                        "source_url": source_url,
                    }
                )
    return results


def _parse_manual_import(rules: AudienceRules) -> list[dict]:
    """Parse operator-supplied CSV text for B2B/B2G/B2C audiences."""
    raw_text = (rules.manual_rows or "").strip()
    if not raw_text:
        return []
    reader = csv.DictReader(io.StringIO(raw_text))
    results = []
    for row in reader:
        norm = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        company = norm.get("company") or norm.get("name") or norm.get("company_name") or ""
        bin_val = re.sub(r"\D", "", norm.get("bin") or norm.get("bin_iin") or "")
        city = norm.get("city") or ""
        director = norm.get("director") or norm.get("contact") or norm.get("director_name") or ""
        phone_raw = norm.get("phone") or norm.get("phone_raw") or ""
        email = norm.get("email") or ""
        category = norm.get("category") or norm.get("sample_item") or "Manual import"
        sum_val = parse_contract_sum(norm.get("contract_sum_kzt"))
        if not company and not bin_val:
            continue
        if not _matches_regions(city, "", rules.regions):
            continue
        if not _matches_keywords(company, category, rules.keywords):
            continue
        phones = normalize_kz_phones(phone_raw)
        if rules.require_phone and not phones:
            continue
        results.append(
            {
                "company": company or bin_val,
                "bin": bin_val,
                "city": city,
                "category": category,
                "director": director,
                "phone_raw": phone_raw,
                "email": email,
                "contract_sum_kzt": sum_val,
                "source": "manual_import",
                "source_url": "",
            }
        )
    return results


def _probe_and_scrape_goszakup(rules: AudienceRules) -> list[dict]:
    """Probe goszakup.gov.kz without bypassing login; scrape up to 20 lots only if open."""
    probe_url = "https://goszakup.gov.kz/ru/search/lots?filter%5Bname%5D=x"
    try:
        with httpx.Client(follow_redirects=False, timeout=12.0) as client:
            resp = client.get(probe_url, headers={"User-Agent": "LeadHunter/1.7"})
        loc = resp.headers.get("location", "").lower()
        if resp.status_code in (301, 302, 303, 307, 308) and "login" in loc:
            raise SourceUnavailable("login_required")
        if resp.status_code == 200 and "/user/login" in resp.text[:4000].lower():
            raise SourceUnavailable("login_required")
        if resp.status_code >= 400:
            raise SourceUnavailable(f"http_{resp.status_code}")
    except SourceUnavailable:
        raise
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"network_error:{exc.__class__.__name__}") from exc

    import goszakup_lead_hunter as gz

    kato_targets = (
        [(reg, KATO_MAP[reg]) for reg in ("almaty", "astana", "shymkent")]
        if "all" in rules.regions
        else [(reg, KATO_MAP[reg]) for reg in rules.regions if reg in KATO_MAP]
    )
    keywords = rules.keywords or ["центрифуга"]
    lots_collected = []
    seen_annos = set()
    for city_slug, kato in kato_targets:
        for kw in keywords:
            if len(lots_collected) >= 20:
                break
            lots = gz.get_completed_lots(keyword=kw, kato=kato, year=str(rules.year), count=20)
            for lot in lots:
                if len(lots_collected) >= 20:
                    break
                aid = lot.get("anno_id")
                if aid and aid not in seen_annos:
                    seen_annos.add(aid)
                    lots_collected.append((city_slug.title(), lot))

    leads = []
    for city_name, lot in lots_collected:
        winners = gz.get_winner_from_announcement(lot["anno_id"])
        for w in winners:
            prof = gz.get_supplier_profile_details(w["supplier_id"]) if w.get("supplier_id") else {}
            sum_val = parse_contract_sum(lot.get("lot_sum"))
            if not _matches_sum(sum_val, rules):
                continue
            phone_raw = prof.get("profile_phone", "")
            if rules.require_phone and not normalize_kz_phones(phone_raw):
                continue
            leads.append(
                {
                    "company": prof.get("name_ru") or w.get("company_name") or "",
                    "bin": w.get("bin_iin") or prof.get("bin", ""),
                    "city": city_name,
                    "category": lot.get("lot_name", ""),
                    "director": prof.get("profile_fio", ""),
                    "phone_raw": phone_raw,
                    "email": prof.get("profile_email", ""),
                    "contract_sum_kzt": sum_val,
                    "source": "goszakup",
                    "source_url": lot.get("anno_url", ""),
                }
            )
    return leads


class AdsManager:
    """Orchestrates Ads-Manager discovery, PBX whitelist caching, and gated auto-dialer ticks."""

    def __init__(
        self,
        store: AdsStore,
        settings: Settings,
        clock: Callable[[], datetime] = now,
        goszakup_fn: Optional[Callable[[AudienceRules], list[dict]]] = None,
        call_fn: Optional[Callable[..., dict]] = None,
        offer_fn: Optional[Callable[..., dict]] = None,
        whitelist_fn: Optional[Callable[[], set[str]]] = None,
        harvester_transport: Any = None,
    ):
        self.store = store
        self.settings = settings
        self.clock = clock
        self._goszakup_fn = goszakup_fn or _probe_and_scrape_goszakup
        self._call_fn = call_fn
        self._offer_fn = offer_fn or dispatch_offer
        self._whitelist_fn = whitelist_fn
        self._harvester_transport = harvester_transport

    def _call_pbx(self, **kwargs) -> dict:
        if self._call_fn is not None:
            return self._call_fn(**kwargs)
        from calling.pbx_caller import call_lead

        return call_lead(**kwargs)

    def fetch_whitelist(self, force: bool = False) -> set[str]:
        """Fetch read-only PBX whitelist over SSH and cache for 10 minutes in SQLite setting."""
        if self._whitelist_fn is not None:
            return self._whitelist_fn()

        raw_cache = self.store.get_setting("am_whitelist_cache")
        cached_numbers: set[str] = set()
        cache_fresh = False
        if raw_cache:
            try:
                parsed = json.loads(raw_cache)
                cached_numbers = set(parsed.get("numbers") or [])
                if time.time() - float(parsed.get("fetched_at", 0)) < 600:
                    cache_fresh = True
            except (ValueError, TypeError):
                cached_numbers = set()

        if cache_fresh and not force:
            return cached_numbers

        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
        ]
        key_path = "/var/lib/lead-hunter/.ssh/id_ed25519"
        if Path(key_path).exists():
            cmd.extend(["-i", key_path])
        cmd.extend([f"{self.settings.telephony_ssh_user}@{self.settings.telephony_host}", "cat /home/debian/bitrix_pbx/whitelist.json"])

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and res.stdout.strip():
                payload = json.loads(res.stdout)
                numbers = sorted(set(payload.get("allowed_numbers") or []))
                self.store.set_setting(
                    "am_whitelist_cache",
                    json.dumps({"fetched_at": time.time(), "numbers": numbers}),
                )
                return set(numbers)
        except Exception as exc:
            self.store.audit("am_whitelist_fetch_failed", str(exc)[:200])

        return cached_numbers

    def run_discovery(self, audience_id: int) -> dict[str, Any]:
        """Execute enabled discovery sources for an audience, upsert up to 200 leads, and record errors."""
        aud = self.store.get_audience(audience_id)
        if not aud:
            raise ValueError("audience_not_found")
        camp = self.store.get_campaign(aud["campaign_id"])
        if not camp:
            raise ValueError("campaign_not_found")
        if aud["status"] != "active" or camp["status"] != "active":
            return {"skipped": "paused", "found": 0, "new": 0, "errors": {}}

        rules = AudienceRules.model_validate(aud["rules"])
        service_label = catalog_service_label(camp["product_key"], camp["lang"])
        whitelist = self.fetch_whitelist()
        collected: list[dict] = []
        errors: dict[str, str] = {}
        source_status_updates: dict[str, str] = {}

        # Run all selected Kazakhstan live multi-source adapters SIMULTANEOUSLY via asyncio.gather
        live_sources = [s for s in rules.sources if s in KZ_LIVE_SOURCES]
        if live_sources:
            try:
                from app.services.discovery.kz_multi_source import KazakhstanMultiSourceHarvester
                from app.services.market_search import MarketSearchEngine
                from app.services.platform_store import PlatformStore

                pstore = PlatformStore(self.store.base)
                mengine = MarketSearchEngine(self.store.base)
                harvester = KazakhstanMultiSourceHarvester(
                    market_engine=mengine,
                    api_keys=pstore.get_data_source_keys(masked=False),
                    transport=self._harvester_transport,
                )
                h_res = _run_coro_sync(
                    harvester.discover_simultaneous(
                        sources=live_sources,
                        keywords=rules.keywords or [service_label or camp["product_key"]],
                        regions=rules.regions,
                        require_phone=rules.require_phone,
                        auto_enrich_bin=True,
                        index_to_fts=True,
                        limit=100,
                    )
                )
                for rec in h_res.get("items") or []:
                    if not _matches_sum(rec.get("contract_sum_kzt"), rules):
                        continue
                    collected.append(rec)
                for src_id, rep in (h_res.get("source_report") or {}).items():
                    st = rep.get("status", "ok")
                    if st == "ok":
                        source_status_updates[src_id] = "ok"
                    else:
                        errors[src_id] = st
                        source_status_updates[src_id] = st
            except Exception as exc:
                for src_id in live_sources:
                    errors[src_id] = f"error:{exc.__class__.__name__}"
                    source_status_updates[src_id] = errors[src_id]

        for src in rules.sources:
            if src in KZ_LIVE_SOURCES:
                continue
            try:
                if src == "csv_seed":
                    collected.extend(_load_csv_seed(rules, self.settings.ads_seed_dir))
                    source_status_updates["csv_seed"] = "ok"
                elif src == "manual_import":
                    collected.extend(_parse_manual_import(rules))
                    source_status_updates["manual_import"] = "ok"
                elif src == "goszakup":
                    collected.extend(self._goszakup_fn(rules))
                    source_status_updates["goszakup"] = "ok"
            except SourceUnavailable as exc:
                errors[src] = f"source_unavailable:{exc}"
                source_status_updates[src] = errors[src]
            except Exception as exc:
                errors[src] = f"error:{exc.__class__.__name__}"
                source_status_updates[src] = errors[src]

        if source_status_updates:
            prev_raw = self.store.get_setting("am_source_status")
            prev_map: dict[str, str] = {}
            if prev_raw:
                try:
                    prev_map = json.loads(prev_raw)
                except json.JSONDecodeError:
                    prev_map = {}
            prev_map.update(source_status_updates)
            self.store.set_setting("am_source_status", json.dumps(prev_map, ensure_ascii=False))

        rows_to_upsert: list[dict] = []
        for raw in collected[:200]:
            phones = normalize_kz_phones(raw.get("phone_raw") or "")
            phone_e164 = phones[0] if phones else ""
            raw["phone_e164"] = phone_e164
            score = compute_icp_score(raw, rules)
            rows_to_upsert.append(
                {
                    "campaign_id": camp["id"],
                    "audience_id": aud["id"],
                    "company": raw["company"],
                    "bin": raw.get("bin", ""),
                    "city": raw.get("city", ""),
                    "category": raw.get("category", ""),
                    "service": service_label,
                    "director": raw.get("director", ""),
                    "phone_raw": raw.get("phone_raw", ""),
                    "phone_e164": phone_e164,
                    "email": raw.get("email", ""),
                    "contract_sum_kzt": raw.get("contract_sum_kzt"),
                    "source": raw.get("source", ""),
                    "source_url": raw.get("source_url", ""),
                    "icp_score": score,
                    "whitelisted": phone_e164 in whitelist if phone_e164 else False,
                }
            )

        counts = self.store.upsert_leads(rows_to_upsert)
        self.store.refresh_whitelist_flags(whitelist)
        summary = {
            "found": len(rows_to_upsert),
            "new": counts["inserted"],
            "updated": counts["updated"],
            "errors": errors,
        }
        self.store.record_discovery_run(audience_id, summary)
        return summary

    def _execute_lead_call(self, lead: dict, lang: str) -> dict[str, Any]:
        """Shared call gate & execution helper (call_one + dialer)."""
        whitelist = self.fetch_whitelist()
        phone = lead.get("phone_e164") or ""
        if not lead.get("enabled", True):
            raise ValueError("lead_disabled")
        if lead.get("campaign_status") != "active":
            raise ValueError("campaign_paused")
        if not phone:
            raise ValueError("missing_phone")
        if phone not in whitelist:
            self.store.update_lead_status(lead["id"], "blocked_whitelist", "Number is not in PBX whitelist")
            raise ValueError("not_whitelisted")

        if lead.get("objective") == "offer_then_call":
            item = resolve_catalog_item(lead["product_key"])
            self._offer_fn(phone, item, lang, self.settings, None)

        from app.services.ai_engine import build_call_script_preview
        from app.services.platform_store import PlatformStore

        pstore = PlatformStore(self.store.base)
        telephony = pstore.get_telephony_config()
        daily_cap = int(telephony.get("daily_call_cap") or 200)
        cstore = ControlStore(self.store.base)
        if cstore.today_outreach_call_count() >= daily_cap:
            self.store.update_lead_status(lead["id"], "deferred_cap", f"Daily call cap ({daily_cap}) reached")
            raise ValueError("daily_call_cap_reached")

        prod_record = pstore.get_product(lead["product_key"])
        company = (
            pstore.get_company_entity(prod_record["company_id"])
            if prod_record and prod_record.get("company_id")
            else None
        ) or pstore.get_company_profile()
        prod_for_script = prod_record or {
            "name": lead["service"] or catalog_service_label(lead["product_key"], lang),
            "name_ru": lead["service"] or catalog_service_label(lead["product_key"], "ru"),
            "price_range": "",
            "specs": "",
        }
        isolated = build_call_script_preview(
            product=prod_for_script,
            company=company,
            telephony=telephony,
            lang=lang,
            custom_greeting=(telephony.get("custom_greeting_kz") if lang == "kz" else telephony.get("custom_greeting_ru")) or "",
            custom_prompt=(telephony.get("custom_prompt_kz") if lang == "kz" else telephony.get("custom_prompt_ru")) or "",
            ceo=lead.get("director") or "",
            biz=lead.get("company") or "",
        )

        res = self._call_pbx(
            phone=phone,
            products=[isolated["product_name"]],
            details=isolated["details_sent_to_pbx"],
            ceo=lead.get("director") or "",
            biz=lead.get("company") or "",
            lang=lang,
            greeting=isolated["greeting"],
            seller=isolated["seller_company"],
            voice=isolated["voice_name"],
            custom_prompt=isolated["system_prompt"],
            transfer_target=isolated["transfer_extension"],
            host=self.settings.telephony_host,
            user=self.settings.telephony_ssh_user,
            remote_script=self.settings.telephony_caller_script,
        )
        ok = bool(res.get("success"))
        output = (res.get("stdout") or res.get("stderr") or "")[:500]
        new_status = "called" if ok else "failed"
        self.store.update_lead_status(lead["id"], new_status, output or new_status)
        ControlStore(self.store.base).log_outreach_call(
            phone,
            lead["service"] or lead["product_key"],
            lang,
            "initiated" if ok else "failed",
            output,
        )
        return {"success": ok, "status": new_status, "output": output}

    def call_one_lead(self, lead_id: int) -> dict[str, Any]:
        lead = self.store.get_lead(lead_id)
        if not lead:
            raise ValueError("lead_not_found")
        whitelist = self.fetch_whitelist()
        self.store.refresh_whitelist_flags(whitelist)
        lead = self.store.get_lead(lead_id)
        lang = lead.get("campaign_lang") or "kz"
        res = self._execute_lead_call(lead, lang)
        if not res["success"]:
            raise RuntimeError(res["output"] or "telephony_call_failed")
        return {"status": "initiated", "lead_id": lead_id, "phone": lead["phone_e164"], "output": res["output"]}

    def send_offer_to_lead(self, lead_id: int) -> dict[str, Any]:
        lead = self.store.get_lead(lead_id)
        if not lead:
            raise ValueError("lead_not_found")
        if not lead.get("phone_e164"):
            raise ValueError("missing_phone")
        lang = lead.get("campaign_lang") or "kz"
        item = resolve_catalog_item(lead["product_key"])
        res = self._offer_fn(lead["phone_e164"], item, lang, self.settings, ControlStore(self.store.base))
        self.store.update_lead_status(lead_id, "offer_sent", res.get("wamid"))
        return res

    async def worker_tick(self) -> bool:
        """Run at most one pending discovery job AND at most one due dial-queue call per tick."""
        did_work = False
        now_dt = self.clock()
        stale_before = (now_dt - timedelta(minutes=30)).isoformat()

        due_aud = self.store.next_due_audience(stale_before)
        if due_aud:
            did_work = True
            await asyncio.to_thread(self.run_discovery, due_aud["id"])

        if self.store.get_setting("am_queue_paused", "0") == "1":
            return did_work
        if not is_dial_window(now_dt):
            return did_work

        utc_now = now_dt if now_dt.tzinfo is not None else now_dt.replace(tzinfo=timezone.utc)
        today_prefix = utc_now.astimezone(ALMATY_TZ).date().isoformat()
        if self.store.count_calls_today(today_prefix) >= self.settings.ads_daily_call_cap:
            return did_work

        item = self.store.next_due_queue_item(now_dt.isoformat())
        if not item:
            return did_work

        did_work = True
        lead = self.store.get_lead(item["lead_id"])
        finish_dt = self.clock()
        next_due = (finish_dt + timedelta(seconds=int(item.get("gap_seconds") or 90))).isoformat()

        if not lead:
            self.store.finish_queue_item(item["id"], "cancelled", "lead_not_found", finish_dt.isoformat(), None)
            return True

        whitelist = self.fetch_whitelist()
        self.store.refresh_whitelist_flags(whitelist)
        lead = self.store.get_lead(item["lead_id"])

        if not lead["enabled"] or lead["campaign_status"] != "active":
            reason = "lead_disabled" if not lead["enabled"] else "campaign_paused"
            self.store.finish_queue_item(item["id"], "cancelled", reason, finish_dt.isoformat(), None)
            return True

        if not lead["phone_e164"] or lead["phone_e164"] not in whitelist:
            self.store.update_lead_status(lead["id"], "blocked_whitelist", "Number not in PBX whitelist")
            self.store.finish_queue_item(item["id"], "blocked", "not_whitelisted", finish_dt.isoformat(), None)
            return True

        try:
            res = await asyncio.to_thread(self._execute_lead_call, lead, item["lang"])
            q_status = "done" if res["success"] else "failed"
            self.store.finish_queue_item(item["id"], q_status, res["output"] or q_status, self.clock().isoformat(), next_due)
        except Exception as exc:
            err = str(exc)[:300]
            self.store.update_lead_status(lead["id"], "failed", err)
            self.store.finish_queue_item(item["id"], "failed", err, self.clock().isoformat(), next_due)
        return True
