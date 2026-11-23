"""Simultaneous Kazakhstan Multi-Source Company Discovery & Waterfall Enrichment Engine.

Supports concurrent sourcing across:
1. satu_kz        — Satu.kz B2B Marketplace (Direct mobile/WhatsApp phones & emails, zero API key)
2. uchet_pk       — PK.Uchet.kz Legal Registry (Official 12-digit BIN, Director, KATO, active status, zero API key)
3. twogis         — 2GIS Places / Catalog API v3 (Direct phones, WhatsApp, addresses, categories)
4. egov_open_data — data.egov.kz ГБД ЮЛ Open Data API v4 (Ministry of Justice legal entities)
5. statsnet       — Statsnet.co REST API v1 (Kazakhstan taxes, solvency, court risk, AI Markdown dossier)
6. e_ondiris      — e-Ondiris / Samruk-Kazyna Domestic Manufacturers (КТП) Registry + local KTP seeds
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from typing import Any, Optional
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from app.services.market_search import MarketSearchEngine
from app.services.web_scraper import SSRFBlockedError, validate_public_url

VERSION = "2.9.0"

# Strict Kazakhstan mobile & landline prefixes to avoid extracting random numeric IDs from HTML
KZ_MOBILE_PREFIXES = {
    "700", "701", "702", "705", "706", "707", "708",
    "747", "771", "775", "776", "777", "778",
}
KZ_LANDLINE_PREFIXES = {
    "710", "711", "712", "713", "714", "715", "716", "717", "718",
    "721", "722", "723", "724", "725", "726", "727", "728", "729",
}
KZ_VALID_PREFIXES = KZ_MOBILE_PREFIXES | KZ_LANDLINE_PREFIXES

_PHONE_CANDIDATE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s\-\(\.]*(7\d{2})[\s\-\)\.]*(\d{3})[\s\-\.]*(\d{2})[\s\-\.]*(\d{2})(?!\d)"
)
_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
_BIN_RE = re.compile(r"\b(\d{12})\b")

CITY_NAMES_RU = {
    "almaty": "Алматы",
    "astana": "Астана",
    "shymkent": "Шымкент",
    "karaganda": "Караганда",
    "aktobe": "Актобе",
    "atyrau": "Атырау",
}

KATO_PREFIX_TO_CITY = {
    "75": ("Алматы", "almaty"),
    "71": ("Астана", "astana"),
    "79": ("Шымкент", "shymkent"),
    "35": ("Караганда", "karaganda"),
    "15": ("Актобе", "aktobe"),
    "23": ("Атырау", "atyrau"),
    "11": ("Акмолинская область", "all"),
    "19": ("Алматинская область", "almaty"),
    "27": ("Западно-Казахстанская область", "all"),
    "31": ("Жамбылская область", "all"),
    "39": ("Костанайская область", "all"),
    "43": ("Кызылординская область", "all"),
    "47": ("Мангистауская область", "all"),
    "55": ("Павлодарская область", "all"),
    "59": ("Северо-Казахстанская область", "all"),
    "61": ("Туркестанская область", "shymkent"),
    "63": ("Восточно-Казахстанская область", "all"),
}

_LEGAL_FORM_RE = re.compile(
    r'\b(?:товарищество\s+с\s+ограниченной\s+ответственностью|акционерное\s+общество|'
    r'индивидуальный\s+предприниматель|государственное\s+учреждение|'
    r'коммунальное\s+государственное\s+предприятие|тоо|жшс|ао|ақ|ип|жк|кгп|ргп|гкп|llp|ltd|inc)\b',
    re.IGNORECASE | re.UNICODE,
)

KZ_SOURCES_META = [
    {
        "id": "satu_kz",
        "label": "Satu.kz B2B Marketplace (Phones & Emails)",
        "category": "commercial",
        "requires_key": False,
        "key_field": "",
        "description": "Live Kazakhstan B2B suppliers & distributors with direct mobile phones and emails (zero API key).",
    },
    {
        "id": "uchet_pk",
        "label": "PK.Uchet.kz Legal Registry (BIN & Director)",
        "category": "registry",
        "requires_key": False,
        "key_field": "",
        "description": "Live Kazakhstan legal entity search & BIN resolver returning official Director FIO, KATO, and active status (zero API key).",
    },
    {
        "id": "twogis",
        "label": "2GIS Kazakhstan Directory (Phones & WhatsApp)",
        "category": "commercial",
        "requires_key": True,
        "key_field": "twogis_api_key",
        "description": "2GIS Places API v3 — verified mobile numbers, WhatsApp links, street addresses, and business rubrics.",
    },
    {
        "id": "egov_open_data",
        "label": "data.egov.kz Open Registry (ГБД ЮЛ)",
        "category": "registry",
        "requires_key": True,
        "key_field": "egov_api_key",
        "description": "Official Kazakhstan Open Data Portal API v4 for Ministry of Justice legal entity registrations and OKED codes.",
    },
    {
        "id": "statsnet",
        "label": "Statsnet Corporate Intel (Taxes & Solvency)",
        "category": "intelligence",
        "requires_key": True,
        "key_field": "statsnet_api_key",
        "description": "Statsnet REST API v1 (jurisdiction: kz) — 5-year tax volume, risk signals, affiliations, and AI dossiers.",
    },
    {
        "id": "e_ondiris",
        "label": "e-Ondiris / Samruk-Kazyna (Domestic Producers)",
        "category": "procurement",
        "requires_key": False,
        "key_field": "",
        "description": "Kazakhstan domestic manufacturers registry (КТП) & Samruk-Kazyna industrial supplier index.",
    },
    {
        "id": "csv_seed",
        "label": "Local FTS5 Market Seeds & Verified CSVs",
        "category": "local",
        "requires_key": False,
        "key_field": "",
        "description": "Sub-50ms SQLite FTS5 BM25 search across locally indexed Kazakhstan company registries and bulk dumps.",
    },
    {
        "id": "goszakup",
        "label": "Goszakup State Procurement",
        "category": "procurement",
        "requires_key": False,
        "key_field": "",
        "description": "State budget procurement lots and winners (requires open access / session on goszakup.gov.kz).",
    },
]


class SourceAdapterError(RuntimeError):
    """Raised when an external Kazakhstan source fails or requires credentials."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def extract_valid_kz_phones(text: str) -> list[str]:
    """Extract strictly valid Kazakhstan E.164 phone numbers, ranking mobile (WhatsApp-capable) first."""
    if not text:
        return []
    mobiles: list[str] = []
    landlines: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        prefix, p1, p2, p3 = match.groups()
        if prefix not in KZ_VALID_PREFIXES:
            continue
        e164 = f"+7{prefix}{p1}{p2}{p3}"
        if e164 in seen:
            continue
        seen.add(e164)
        if prefix in KZ_MOBILE_PREFIXES:
            mobiles.append(e164)
        else:
            landlines.append(e164)
    return mobiles + landlines


def is_kz_mobile(phone_e164: str) -> bool:
    digits = re.sub(r"\D", "", phone_e164 or "")
    return len(digits) == 11 and digits[0] == "7" and digits[1:4] in KZ_MOBILE_PREFIXES


def extract_emails(text: str) -> list[str]:
    if not text:
        return []
    blocked_domains = {"sentry.io", "example.com", "w3.org", "schema.org", "satu.kz", "prom.ua"}
    out: list[str] = []
    for em in _EMAIL_RE.findall(text):
        low = em.lower()
        dom = low.split("@")[-1]
        if any(dom == b or dom.endswith("." + b) for b in blocked_domains):
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".js", ".css")):
            continue
        if low not in out:
            out.append(low)
    return out


def normalize_company_key(name: str) -> str:
    """Normalize legal company name for cross-source deduplication (strips ТОО/ЖШС/quotes/punctuation)."""
    cleaned = _LEGAL_FORM_RE.sub(" ", (name or "").casefold())
    cleaned = re.sub(r'[«»"\'`.,;:()\[\]{}\-_/\\]+', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_company_search_term(name: str) -> str:
    """Produce a concise legal name suitable for querying PK.Uchet.kz / registries."""
    cleaned = _LEGAL_FORM_RE.sub(" ", (name or ""))
    cleaned = re.sub(r'[«»"\'`]+', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:60]


def city_from_kato_or_address(kato: Any, address: str) -> tuple[str, str]:
    """Resolve human-readable city and normalized region slug from KATO code or street address."""
    kato_str = re.sub(r"\D", "", str(kato or ""))
    if len(kato_str) >= 2 and kato_str[:2] in KATO_PREFIX_TO_CITY:
        return KATO_PREFIX_TO_CITY[kato_str[:2]]
    low = (address or "").casefold()
    for slug, ru_name in CITY_NAMES_RU.items():
        if slug in low or ru_name.casefold() in low:
            return ru_name, slug
    if "нур-султан" in low or "нұр-сұлтан" in low:
        return "Астана", "astana"
    return "", "all"


def matches_requested_regions(city: str, address: str, regions: list[str]) -> bool:
    if not regions or "all" in regions:
        return True
    _, detected_slug = city_from_kato_or_address("", f"{city} {address}")
    haystack = f"{city} {address}".casefold()
    for reg in regions:
        if reg == detected_slug:
            return True
        ru_name = CITY_NAMES_RU.get(reg, reg).casefold()
        if reg in haystack or ru_name in haystack:
            return True
    return False


def _sanitize_bin(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if len(digits) == 12 else ""


# =====================================================================
# 1. Satu.kz B2B Marketplace Adapter (Zero API Key)
# =====================================================================
class SatuKzAdapter:
    """Scrapes Satu.kz B2B search results & seller profiles for verified phones and emails."""

    source_id = "satu_kz"

    def __init__(self, transport: Any = None, timeout: float = 10.0, resolve_dns: bool = False):
        self.transport = transport
        self.timeout = timeout
        self.resolve_dns = resolve_dns

    async def search(
        self,
        keywords: list[str],
        regions: list[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        terms = [k.strip() for k in keywords if k.strip()] or ["оборудование"]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,kk;q=0.8",
        }
        companies: dict[str, dict[str, Any]] = {}

        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=True) as client:
            for term in terms[:3]:
                search_url = f"https://satu.kz/search?search_term={quote(term)}"
                validate_public_url(search_url, resolve_dns=self.resolve_dns)
                resp = await client.get(search_url, headers=headers)
                if resp.status_code != 200:
                    raise SourceAdapterError(f"http_{resp.status_code}", f"Satu.kz returned HTTP {resp.status_code}")

                soup = BeautifulSoup(resp.text, "lxml")
                blocks = soup.select('[data-qaid="product_block"]')
                for block in blocks:
                    comp_el = block.select_one('[data-qaid="company_name"]')
                    if not comp_el:
                        continue
                    cname = comp_el.get_text(" ", strip=True)
                    if not cname:
                        continue
                    ckey = normalize_company_key(cname)
                    if not ckey or ckey in companies:
                        continue

                    # Locate seller profile URL (/c12345-company.html)
                    comp_url = ""
                    parent_a = comp_el if comp_el.name == "a" else comp_el.find_parent("a", href=True)
                    if parent_a and parent_a.get("href"):
                        comp_url = urljoin("https://satu.kz", parent_a["href"])
                    else:
                        for a in block.find_all("a", href=True):
                            if re.search(r"/c\d+\-", a["href"]):
                                comp_url = urljoin("https://satu.kz", a["href"])
                                break

                    prod_el = block.select_one('[data-qaid="product_name"]')
                    city_el = block.select_one('[data-qaid="company_city"], [data-qaid="region_name"]')
                    price_el = block.select_one('[data-qaid="product_price"]')
                    city_txt = city_el.get_text(" ", strip=True) if city_el else ""
                    prod_txt = prod_el.get_text(" ", strip=True) if prod_el else term
                    price_txt = price_el.get_text(" ", strip=True) if price_el else ""

                    companies[ckey] = {
                        "company": cname,
                        "bin": "",
                        "city": city_txt,
                        "address": "",
                        "category": prod_txt[:140],
                        "director": "",
                        "phone_raw": "",
                        "phone_e164": "",
                        "whatsapp_active": False,
                        "email": "",
                        "website": comp_url,
                        "contract_sum_kzt": _parse_kzt_amount(price_txt),
                        "source": self.source_id,
                        "sources": [self.source_id],
                        "source_url": comp_url or search_url,
                    }
                    if len(companies) >= min(limit, 18):
                        break

            # Concurrently fetch seller contact pages (bounded semaphore) to extract direct phones & emails
            sem = asyncio.Semaphore(4)

            async def _enrich_seller_page(rec: dict[str, Any]) -> None:
                url = rec.get("website") or ""
                if not url or "satu.kz/c" not in url:
                    return
                async with sem:
                    try:
                        validate_public_url(url, resolve_dns=self.resolve_dns)
                        r = await client.get(url, headers=headers)
                        if r.status_code != 200:
                            return
                        psoup = BeautifulSoup(r.text, "lxml")
                        # Extract tel: links and visible text (ignoring <script>/<style> noise)
                        tel_nums: list[str] = []
                        has_wa = "wa.me" in r.text.lower() or "whatsapp" in r.text.lower()
                        for a in psoup.find_all("a", href=True):
                            href = a["href"]
                            if href.lower().startswith("tel:"):
                                tel_nums.extend(extract_valid_kz_phones(href))
                            if "wa.me" in href.lower() or "whatsapp.com" in href.lower():
                                has_wa = True
                                tel_nums.extend(extract_valid_kz_phones(href))
                        for noise in psoup(["script", "style", "noscript"]):
                            noise.decompose()
                        vis_text = psoup.get_text(" ", strip=True)
                        phones = tel_nums + [p for p in extract_valid_kz_phones(vis_text) if p not in tel_nums]
                        # Fallback to full raw HTML only if visible text had none (still strictly KZ prefix validated)
                        if not phones:
                            phones = extract_valid_kz_phones(r.text)
                        emails = extract_emails(vis_text) or extract_emails(r.text)
                        if phones:
                            rec["phone_e164"] = phones[0]
                            rec["phone_raw"] = ", ".join(phones[:3])
                            rec["whatsapp_active"] = bool(has_wa or is_kz_mobile(phones[0]))
                        if emails:
                            rec["email"] = emails[0]
                        if not rec["city"]:
                            c_name, _ = city_from_kato_or_address("", vis_text[:1500])
                            if c_name:
                                rec["city"] = c_name
                    except Exception:
                        return

            await asyncio.gather(*[_enrich_seller_page(rec) for rec in companies.values()])

        results = list(companies.values())
        if regions and "all" not in regions:
            filtered = [r for r in results if not r["city"] or matches_requested_regions(r["city"], r["address"], regions)]
            results = filtered
        return results[:limit]


# =====================================================================
# 2. PK.Uchet.kz Legal Registry Adapter (Zero API Key)
# =====================================================================
class UchetPkAdapter:
    """Queries pk.uchet.kz public company search API for BIN, Director FIO, KATO, and active status."""

    source_id = "uchet_pk"
    endpoint = "https://pk.uchet.kz/api/web/company/search/"

    def __init__(self, transport: Any = None, timeout: float = 10.0, resolve_dns: bool = False):
        self.transport = transport
        self.timeout = timeout
        self.resolve_dns = resolve_dns

    async def query_raw(self, value: str, client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
        q = (value or "").strip()
        if len(q) < 2:
            return []
        validate_public_url(self.endpoint, resolve_dns=self.resolve_dns)
        headers = {
            "User-Agent": "LeadHunter/2.9 (Kazakhstan B2B Platform)",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient(transport=self.transport, timeout=self.timeout)
        try:
            resp = await client.post(self.endpoint, json={"value": q}, headers=headers)
            if resp.status_code != 200:
                raise SourceAdapterError(f"http_{resp.status_code}", f"pk.uchet.kz HTTP {resp.status_code}")
            payload = resp.json()
            return payload.get("results") or []
        finally:
            if own_client:
                await client.aclose()

    def _to_lead_record(self, item: dict[str, Any], keyword: str = "") -> dict[str, Any]:
        bin_val = _sanitize_bin(item.get("bin") or item.get("iin"))
        address = (item.get("address") or "").strip()
        city_name, _ = city_from_kato_or_address(item.get("kato_code"), address)
        status = (item.get("status") or "active").strip().lower()
        return {
            "company": (item.get("name") or "").strip() or bin_val,
            "bin": bin_val,
            "city": city_name,
            "address": address,
            "kato_code": str(item.get("kato_code") or ""),
            "category": keyword or "Юридическое лицо РК",
            "director": (item.get("head_fullname") or "").strip(),
            "phone_raw": "",
            "phone_e164": "",
            "whatsapp_active": False,
            "email": "",
            "website": "",
            "legal_status": status,
            "contract_sum_kzt": None,
            "source": self.source_id,
            "sources": [self.source_id],
            "source_url": f"https://pk.uchet.kz/search/bin/{bin_val}" if bin_val else "https://pk.uchet.kz/",
        }

    async def search(self, keywords: list[str], regions: list[str], limit: int = 20) -> list[dict[str, Any]]:
        terms = [k.strip() for k in keywords if k.strip()] or ["медтехника"]
        out: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            for term in terms[:3]:
                # pk.uchet.kz works best with single clean terms or BINs
                rows = await self.query_raw(term, client=client)
                if not rows and " " in term:
                    rows = await self.query_raw(term.split()[0], client=client)
                for row in rows:
                    if (row.get("status") or "").lower() == "liquidated":
                        continue
                    rec = self._to_lead_record(row, keyword=term)
                    if not matches_requested_regions(rec["city"], rec["address"], regions):
                        continue
                    dedupe = rec["bin"] or normalize_company_key(rec["company"])
                    if dedupe in seen_keys:
                        continue
                    seen_keys.add(dedupe)
                    out.append(rec)
                    if len(out) >= limit:
                        return out
        return out

    async def lookup_one(self, query_val: str, client: Optional[httpx.AsyncClient] = None) -> Optional[dict[str, Any]]:
        rows = await self.query_raw(query_val, client=client)
        if not rows:
            return None
        # Prefer active companies with a 12-digit BIN
        ranked = sorted(
            rows,
            key=lambda r: (
                0 if (r.get("status") or "").lower() == "active" else 1,
                0 if _sanitize_bin(r.get("bin")) else 1,
            ),
        )
        return self._to_lead_record(ranked[0])


# =====================================================================
# 3. 2GIS Kazakhstan Places API v3 Adapter
# =====================================================================
class TwoGisAdapter:
    """Queries 2GIS Places / Catalog API v3 for Kazakhstan businesses, phones, and WhatsApp links."""

    source_id = "twogis"
    endpoint = "https://catalog.api.2gis.com/3.0/items"

    def __init__(self, api_key: str = "", transport: Any = None, timeout: float = 10.0, resolve_dns: bool = False):
        self.api_key = (api_key or "").strip()
        self.transport = transport
        self.timeout = timeout
        self.resolve_dns = resolve_dns

    async def search(self, keywords: list[str], regions: list[str], limit: int = 20) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SourceAdapterError("api_key_required", "Configure 2GIS Places API Key in Data Sources settings")

        validate_public_url(self.endpoint, resolve_dns=self.resolve_dns)
        terms = [k.strip() for k in keywords if k.strip()] or ["лаборатория"]
        target_cities = (
            ["Алматы", "Астана", "Шымкент"]
            if (not regions or "all" in regions)
            else [CITY_NAMES_RU.get(r, r) for r in regions]
        )

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            for city in target_cities[:3]:
                for kw in terms[:2]:
                    params = {
                        "q": f"{kw} {city}",
                        "type": "branch",
                        "fields": "items.contact_groups,items.address_name,items.rubrics,items.org",
                        "page_size": min(10, limit),
                        "key": self.api_key,
                    }
                    resp = await client.get(self.endpoint, params=params)
                    if resp.status_code != 200:
                        raise SourceAdapterError(f"http_{resp.status_code}", f"2GIS HTTP {resp.status_code}")
                    data = resp.json()
                    meta = data.get("meta") or {}
                    if meta.get("code") and int(meta["code"]) >= 400:
                        err_msg = (meta.get("error") or {}).get("message") or f"code_{meta['code']}"
                        raise SourceAdapterError(f"twogis_{meta['code']}", err_msg)

                    items = (data.get("result") or {}).get("items") or []
                    for it in items:
                        name = (it.get("name") or (it.get("org") or {}).get("name") or "").strip()
                        if not name:
                            continue
                        phones: list[str] = []
                        emails: list[str] = []
                        website = ""
                        whatsapp_active = False
                        for cg in it.get("contact_groups") or []:
                            for c in cg.get("contacts") or []:
                                ctype = (c.get("type") or "").lower()
                                val = str(c.get("value") or c.get("text") or "").strip()
                                if ctype in ("phone", "whatsapp"):
                                    if ctype == "whatsapp":
                                        whatsapp_active = True
                                    phones.extend(extract_valid_kz_phones(val))
                                elif ctype == "email" and val:
                                    emails.append(val)
                                elif ctype == "website" and val and not website:
                                    website = val
                        # Deduplicate phones keeping mobile first
                        uniq_phones = extract_valid_kz_phones(" ".join(phones))
                        if uniq_phones and is_kz_mobile(uniq_phones[0]):
                            whatsapp_active = True

                        rubrics = [r.get("name", "") for r in (it.get("rubrics") or []) if r.get("name")]
                        dedupe = (uniq_phones[0] if uniq_phones else "") or normalize_company_key(name)
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)

                        out.append({
                            "company": name,
                            "bin": "",
                            "city": city,
                            "address": (it.get("address_name") or "").strip(),
                            "category": ", ".join(rubrics[:3]) or kw,
                            "director": "",
                            "phone_raw": ", ".join(uniq_phones[:3]),
                            "phone_e164": uniq_phones[0] if uniq_phones else "",
                            "whatsapp_active": whatsapp_active,
                            "email": emails[0] if emails else "",
                            "website": website,
                            "contract_sum_kzt": None,
                            "source": self.source_id,
                            "sources": [self.source_id],
                            "source_url": f"https://2gis.kz/search/{quote(name + ' ' + city)}",
                        })
                        if len(out) >= limit:
                            return out
        return out


# =====================================================================
# 4. data.egov.kz Open Registry (ГБД ЮЛ) API v4 Adapter
# =====================================================================
class EgovOpenDataAdapter:
    """Queries data.egov.kz API v4 (gbd_ul) for registered Kazakhstan legal entities."""

    source_id = "egov_open_data"
    endpoint = "https://data.egov.kz/api/v4/gbd_ul/v1"

    def __init__(self, api_key: str = "", transport: Any = None, timeout: float = 10.0, resolve_dns: bool = False):
        self.api_key = (api_key or "").strip()
        self.transport = transport
        self.timeout = timeout
        self.resolve_dns = resolve_dns

    async def search(self, keywords: list[str], regions: list[str], limit: int = 20) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SourceAdapterError("api_key_required", "Configure free data.egov.kz apiKey in Data Sources settings")
        validate_public_url(self.endpoint, resolve_dns=self.resolve_dns)
        terms = [k.strip() for k in keywords if k.strip()] or ["ТОО"]
        query_str = " ".join(terms[:3])
        es_query = {
            "size": min(limit, 50),
            "query": {
                "multi_match": {
                    "query": query_str,
                    "fields": ["nameRu", "nameKz", "okedNameRu", "bin", "address"],
                }
            },
        }
        params = {"apiKey": self.api_key, "source": json.dumps(es_query, ensure_ascii=False)}
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            resp = await client.get(self.endpoint, params=params)
            if resp.status_code == 403:
                raise SourceAdapterError("invalid_api_key", "data.egov.kz rejected apiKey (HTTP 403)")
            if resp.status_code != 200:
                raise SourceAdapterError(f"http_{resp.status_code}", f"data.egov.kz HTTP {resp.status_code}")
            rows = resp.json()
            if isinstance(rows, dict):
                rows = rows.get("data") or rows.get("hits") or []

        out: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = (item.get("nameRu") or item.get("nameKz") or item.get("name") or "").strip()
            bin_val = _sanitize_bin(item.get("bin"))
            address = (item.get("address") or item.get("legalAddress") or "").strip()
            city_name, _ = city_from_kato_or_address(item.get("katoCode") or item.get("kato"), address)
            if not name and not bin_val:
                continue
            if not matches_requested_regions(city_name, address, regions):
                continue
            oked = " · ".join(x for x in [str(item.get("okedCode") or "").strip(), str(item.get("okedNameRu") or "").strip()] if x)
            out.append({
                "company": name or bin_val,
                "bin": bin_val,
                "city": city_name,
                "address": address,
                "oked": oked,
                "category": oked or "ГБД ЮЛ (data.egov.kz)",
                "director": (item.get("directorFio") or item.get("headFullname") or item.get("director") or "").strip(),
                "phone_raw": "",
                "phone_e164": "",
                "whatsapp_active": False,
                "email": "",
                "website": "",
                "legal_status": (item.get("status") or "active").lower(),
                "contract_sum_kzt": None,
                "source": self.source_id,
                "sources": [self.source_id],
                "source_url": "https://data.egov.kz/datasets/view?index=gbd_ul",
            })
        return out[:limit]

    async def lookup_bin(self, bin_code: str) -> Optional[dict[str, Any]]:
        if not self.api_key:
            return None
        clean_bin = _sanitize_bin(bin_code)
        if not clean_bin:
            return None
        rows = await self.search([clean_bin], ["all"], limit=5)
        for r in rows:
            if r.get("bin") == clean_bin:
                return r
        return rows[0] if rows else None


# =====================================================================
# 5. Statsnet.co Kazakhstan REST API v1 Adapter
# =====================================================================
class StatsnetAdapter:
    """Queries Statsnet API v1 (jurisdiction: kz) for corporate search, taxes, risks, and AI dossiers."""

    source_id = "statsnet"
    base_url = "https://statsnet.co/api/v1"

    def __init__(self, api_key: str = "", transport: Any = None, timeout: float = 10.0, resolve_dns: bool = False):
        self.api_key = (api_key or "").strip()
        self.transport = transport
        self.timeout = timeout
        self.resolve_dns = resolve_dns

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "LeadHunter/2.9",
        }

    async def search(self, keywords: list[str], regions: list[str], limit: int = 20) -> list[dict[str, Any]]:
        if not self.api_key:
            raise SourceAdapterError("api_key_required", "Configure Statsnet API Key (sk_live_...) in Data Sources settings")
        url = f"{self.base_url}/companies/search"
        validate_public_url(url, resolve_dns=self.resolve_dns)
        query_str = " ".join(k.strip() for k in keywords if k.strip())[:120] or "медицина"

        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                json={"query": query_str, "filters": {"jurisdiction": ["kz"]}, "limit": min(limit, 30)},
            )
            if resp.status_code in (401, 403):
                raise SourceAdapterError("invalid_api_key", f"Statsnet rejected API key (HTTP {resp.status_code})")
            if resp.status_code == 429:
                raise SourceAdapterError("rate_limited", "Statsnet API quota exceeded (HTTP 429)")
            if resp.status_code != 200:
                raise SourceAdapterError(f"http_{resp.status_code}", f"Statsnet HTTP {resp.status_code}")
            payload = resp.json()

        items = payload.get("items") or payload.get("results") or payload.get("data") or []
        out: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = (it.get("name") or it.get("title") or "").strip()
            bin_val = _sanitize_bin(it.get("bin") or it.get("identifier") or it.get("registration_number"))
            address = (it.get("address") or "").strip()
            city_name, _ = city_from_kato_or_address(it.get("kato"), address or str(it.get("city") or ""))
            if not name and not bin_val:
                continue
            if not matches_requested_regions(city_name, address, regions):
                continue
            tax_kzt = _parse_kzt_amount(it.get("taxes_kzt") or it.get("estimated_revenue") or it.get("revenue"))
            phones = extract_valid_kz_phones(str(it.get("phone") or it.get("phones") or ""))
            out.append({
                "company": name or bin_val,
                "bin": bin_val,
                "city": city_name or (it.get("city") or ""),
                "address": address,
                "oked": (it.get("oked") or it.get("industry") or "").strip(),
                "category": (it.get("industry") or it.get("oked") or "Statsnet KZ").strip(),
                "director": (it.get("director") or it.get("ceo") or "").strip(),
                "phone_raw": ", ".join(phones[:3]),
                "phone_e164": phones[0] if phones else "",
                "whatsapp_active": bool(phones and is_kz_mobile(phones[0])),
                "email": (it.get("email") or "").strip(),
                "website": (it.get("website") or "").strip(),
                "tax_paid_kzt": tax_kzt,
                "contract_sum_kzt": tax_kzt,
                "risk_level": (it.get("risk_level") or "low").lower(),
                "source": self.source_id,
                "sources": [self.source_id],
                "source_url": f"https://statsnet.co/companies/kz/{bin_val}" if bin_val else "https://statsnet.co/",
            })
        return out[:limit]

    async def enrich_bin(self, bin_code: str) -> Optional[dict[str, Any]]:
        if not self.api_key:
            return None
        clean_bin = _sanitize_bin(bin_code)
        if not clean_bin:
            return None
        md_url = f"{self.base_url}/companies/kz/{clean_bin}/md"
        validate_public_url(md_url, resolve_dns=self.resolve_dns)
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            resp = await client.get(md_url, headers=self._headers())
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("content-type", "").lower()
            if "json" in ctype:
                data = resp.json()
                return {
                    "bin": clean_bin,
                    "company": data.get("name") or "",
                    "director": data.get("director") or data.get("ceo") or "",
                    "tax_paid_kzt": _parse_kzt_amount(data.get("taxes_kzt") or data.get("revenue")),
                    "risk_level": data.get("risk_level") or "unknown",
                    "markdown_dossier": data.get("markdown") or json.dumps(data, ensure_ascii=False)[:4000],
                    "source": self.source_id,
                }
            return {
                "bin": clean_bin,
                "markdown_dossier": resp.text[:4000],
                "source": self.source_id,
            }


# =====================================================================
# 6. e-Ondiris / Samruk-Kazyna Domestic Producers Adapter (КТП)
# =====================================================================
class EOndirisSamrukAdapter:
    """Queries e-ondiris.gov.kz domestic manufacturers registry + local FTS5 KTP/Samruk records."""

    source_id = "e_ondiris"
    endpoint = "https://e-ondiris.gov.kz/api/public/producers"

    def __init__(
        self,
        market_engine: Optional[MarketSearchEngine] = None,
        transport: Any = None,
        timeout: float = 6.0,
        resolve_dns: bool = False,
    ):
        self.market_engine = market_engine
        self.transport = transport
        self.timeout = timeout
        self.resolve_dns = resolve_dns

    async def search(self, keywords: list[str], regions: list[str], limit: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        query_str = " ".join(k.strip() for k in keywords if k.strip())[:100]

        # 1. Try live e-ondiris.gov.kz public endpoint (fast timeout, graceful fallback if government SSL/network times out)
        live_error = ""
        try:
            validate_public_url(self.endpoint, resolve_dns=self.resolve_dns)
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                resp = await client.get(self.endpoint, params={"search": query_str, "size": min(limit, 30)})
                if resp.status_code == 200:
                    payload = resp.json()
                    items = payload if isinstance(payload, list) else (payload.get("content") or payload.get("items") or [])
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        name = (it.get("companyName") or it.get("name") or "").strip()
                        bin_val = _sanitize_bin(it.get("bin"))
                        city_name, _ = city_from_kato_or_address(it.get("kato"), str(it.get("region") or it.get("address") or ""))
                        if not name and not bin_val:
                            continue
                        if not matches_requested_regions(city_name, str(it.get("address") or ""), regions):
                            continue
                        phones = extract_valid_kz_phones(str(it.get("phone") or it.get("contacts") or ""))
                        dedupe = bin_val or normalize_company_key(name)
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)
                        out.append({
                            "company": name or bin_val,
                            "bin": bin_val,
                            "city": city_name,
                            "address": str(it.get("address") or ""),
                            "category": f"КТП · {it.get('productName') or it.get('ensTru') or 'Отечественный производитель'}",
                            "director": str(it.get("director") or ""),
                            "phone_raw": ", ".join(phones[:3]),
                            "phone_e164": phones[0] if phones else "",
                            "whatsapp_active": bool(phones and is_kz_mobile(phones[0])),
                            "email": str(it.get("email") or ""),
                            "is_domestic_producer": True,
                            "contract_sum_kzt": None,
                            "source": self.source_id,
                            "sources": [self.source_id],
                            "source_url": "https://e-ondiris.gov.kz/",
                        })
        except Exception as exc:
            live_error = exc.__class__.__name__

        # 2. Complement with any ingested Samruk/KTP/e-Ondiris records in SQLite FTS5
        if self.market_engine is not None:
            reg_arg = regions[0] if (regions and len(regions) == 1 and regions[0] != "all") else ""
            res = self.market_engine.search(query=query_str, region=reg_arg, limit=limit * 2)
            for row in res.get("items") or []:
                src = (row.get("source") or "").lower()
                if not any(tag in src for tag in ("e_ondiris", "ondiris", "samruk", "ktp", "sk")):
                    continue
                dedupe = _sanitize_bin(row.get("bin")) or normalize_company_key(row.get("company") or "")
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                phones = extract_valid_kz_phones(row.get("phone") or "")
                out.append({
                    "company": row.get("company") or "",
                    "bin": _sanitize_bin(row.get("bin")),
                    "city": row.get("city") or "",
                    "address": "",
                    "category": row.get("category") or "КТП / Самрук-Қазына",
                    "director": "",
                    "phone_raw": row.get("phone") or "",
                    "phone_e164": phones[0] if phones else "",
                    "whatsapp_active": bool(phones and is_kz_mobile(phones[0])),
                    "email": row.get("email") or "",
                    "is_domestic_producer": True,
                    "contract_sum_kzt": row.get("contract_sum_kzt") or None,
                    "source": self.source_id,
                    "sources": [self.source_id],
                    "source_url": "https://e-ondiris.gov.kz/",
                })

        if not out and live_error:
            raise SourceAdapterError(f"portal_unreachable:{live_error}", f"e-ondiris.gov.kz timed out ({live_error}); upload KTP export or use local seeds")
        return out[:limit]


def _parse_kzt_amount(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    cleaned = str(raw).replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        val = float(m.group(0))
        return val if val > 0 else None
    except ValueError:
        return None


# =====================================================================
# Simultaneous Multi-Source Harvester & Waterfall Enricher
# =====================================================================
class KazakhstanMultiSourceHarvester:
    """Runs all selected Kazakhstan sources concurrently, merges & deduplicates, and enriches BIN/Director/Taxes."""

    def __init__(
        self,
        market_engine: Optional[MarketSearchEngine] = None,
        api_keys: Optional[dict[str, str]] = None,
        transport: Any = None,
        resolve_dns: bool = False,
    ):
        self.market_engine = market_engine
        self.api_keys = api_keys or {}
        self.transport = transport
        self.resolve_dns = resolve_dns

        self.satu = SatuKzAdapter(transport=transport, resolve_dns=resolve_dns)
        self.uchet = UchetPkAdapter(transport=transport, resolve_dns=resolve_dns)
        self.twogis = TwoGisAdapter(
            api_key=self.api_keys.get("twogis_api_key", ""),
            transport=transport,
            resolve_dns=resolve_dns,
        )
        self.egov = EgovOpenDataAdapter(
            api_key=self.api_keys.get("egov_api_key", ""),
            transport=transport,
            resolve_dns=resolve_dns,
        )
        self.statsnet = StatsnetAdapter(
            api_key=self.api_keys.get("statsnet_api_key", ""),
            transport=transport,
            resolve_dns=resolve_dns,
        )
        self.e_ondiris = EOndirisSamrukAdapter(
            market_engine=market_engine,
            transport=transport,
            resolve_dns=resolve_dns,
        )

    def _merge_records(self, existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """Merge two records representing the same Kazakhstan company without losing richer attributes."""
        merged = dict(existing)
        # Prefer longer/fuller legal name if incoming has a BIN and existing didn't
        if incoming.get("bin") and not merged.get("bin"):
            merged["bin"] = incoming["bin"]
            if len(incoming.get("company") or "") > len(merged.get("company") or ""):
                merged["company"] = incoming["company"]

        for field in ("city", "address", "director", "email", "website", "oked", "kato_code", "legal_status"):
            if not merged.get(field) and incoming.get(field):
                merged[field] = incoming[field]

        # Combine phone numbers and re-rank mobile first
        combined_raw = " ".join(x for x in [merged.get("phone_raw", ""), incoming.get("phone_raw", ""), merged.get("phone_e164", ""), incoming.get("phone_e164", "")] if x)
        phones = extract_valid_kz_phones(combined_raw)
        if phones:
            merged["phone_e164"] = phones[0]
            merged["phone_raw"] = ", ".join(phones[:3])
            merged["whatsapp_active"] = bool(merged.get("whatsapp_active") or incoming.get("whatsapp_active") or is_kz_mobile(phones[0]))

        if incoming.get("category") and incoming["category"] not in (merged.get("category") or ""):
            if not merged.get("category") or merged["category"] in ("Юридическое лицо РК", "B2B/B2G"):
                merged["category"] = incoming["category"]

        if incoming.get("contract_sum_kzt") and (not merged.get("contract_sum_kzt") or incoming["contract_sum_kzt"] > merged["contract_sum_kzt"]):
            merged["contract_sum_kzt"] = incoming["contract_sum_kzt"]

        if incoming.get("tax_paid_kzt"):
            merged["tax_paid_kzt"] = incoming["tax_paid_kzt"]
        if incoming.get("risk_level"):
            merged["risk_level"] = incoming["risk_level"]
        if incoming.get("is_domestic_producer"):
            merged["is_domestic_producer"] = True
        if not merged.get("source_url") and incoming.get("source_url"):
            merged["source_url"] = incoming["source_url"]

        srcs = list(merged.get("sources") or ([merged["source"]] if merged.get("source") else []))
        for s in incoming.get("sources") or ([incoming["source"]] if incoming.get("source") else []):
            if s and s not in srcs:
                srcs.append(s)
        merged["sources"] = srcs
        merged["source"] = "+".join(srcs)
        return merged

    def deduplicate_and_merge(self, raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate cross-source company records by 12-digit BIN, E.164 phone, or normalized legal name."""
        merged_list: list[dict[str, Any]] = []
        by_bin: dict[str, int] = {}
        by_phone: dict[str, int] = {}
        by_name: dict[str, int] = {}

        for rec in raw_records:
            bin_val = _sanitize_bin(rec.get("bin"))
            phones = extract_valid_kz_phones(rec.get("phone_raw") or rec.get("phone_e164") or "")
            phone_val = phones[0] if phones else ""
            name_key = normalize_company_key(rec.get("company") or "")

            rec = dict(rec)
            rec["bin"] = bin_val
            if phone_val:
                rec["phone_e164"] = phone_val
                if not rec.get("phone_raw"):
                    rec["phone_raw"] = phone_val
                rec["whatsapp_active"] = bool(rec.get("whatsapp_active") or is_kz_mobile(phone_val))
            if "sources" not in rec:
                rec["sources"] = [rec.get("source") or "kz_multi"]

            idx: Optional[int] = None
            if bin_val and bin_val in by_bin:
                idx = by_bin[bin_val]
            elif phone_val and phone_val in by_phone:
                idx = by_phone[phone_val]
            elif name_key and name_key in by_name:
                idx = by_name[name_key]

            if idx is None:
                idx = len(merged_list)
                merged_list.append(rec)
            else:
                merged_list[idx] = self._merge_records(merged_list[idx], rec)

            cur = merged_list[idx]
            if cur.get("bin"):
                by_bin[cur["bin"]] = idx
            if cur.get("phone_e164"):
                by_phone[cur["phone_e164"]] = idx
            nk = normalize_company_key(cur.get("company") or "")
            if nk:
                by_name[nk] = idx

        return merged_list

    async def _run_single_source(
        self,
        source_id: str,
        keywords: list[str],
        regions: list[str],
        limit: int,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        t0 = time.monotonic()
        try:
            if source_id == "satu_kz":
                rows = await self.satu.search(keywords, regions, limit=limit)
            elif source_id == "uchet_pk":
                rows = await self.uchet.search(keywords, regions, limit=limit)
            elif source_id == "twogis":
                rows = await self.twogis.search(keywords, regions, limit=limit)
            elif source_id == "egov_open_data":
                rows = await self.egov.search(keywords, regions, limit=limit)
            elif source_id == "statsnet":
                rows = await self.statsnet.search(keywords, regions, limit=limit)
            elif source_id == "e_ondiris":
                rows = await self.e_ondiris.search(keywords, regions, limit=limit)
            elif source_id == "csv_seed" and self.market_engine is not None:
                q_str = " ".join(keywords)
                reg_arg = regions[0] if (regions and len(regions) == 1 and regions[0] != "all") else ""
                res = self.market_engine.search(query=q_str, region=reg_arg, limit=limit)
                rows = []
                for r in res.get("items") or []:
                    phones = extract_valid_kz_phones(r.get("phone") or "")
                    rows.append({
                        "company": r.get("company") or "",
                        "bin": _sanitize_bin(r.get("bin")),
                        "city": r.get("city") or "",
                        "address": "",
                        "category": r.get("category") or r.get("activity") or "Market Seed",
                        "director": "",
                        "phone_raw": r.get("phone") or "",
                        "phone_e164": phones[0] if phones else "",
                        "whatsapp_active": bool(phones and is_kz_mobile(phones[0])),
                        "email": r.get("email") or "",
                        "contract_sum_kzt": r.get("contract_sum_kzt") or None,
                        "source": "csv_seed",
                        "sources": ["csv_seed"],
                        "source_url": "",
                    })
            else:
                return source_id, [], {"status": "unsupported", "found": 0, "ms": 0}

            ms = int((time.monotonic() - t0) * 1000)
            return source_id, rows, {"status": "ok", "found": len(rows), "ms": ms}
        except SourceAdapterError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            return source_id, [], {"status": exc.code, "detail": exc.detail, "found": 0, "ms": ms}
        except SSRFBlockedError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            return source_id, [], {"status": f"ssrf_blocked:{exc}", "found": 0, "ms": ms}
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            return source_id, [], {"status": f"error:{exc.__class__.__name__}", "detail": str(exc)[:180], "found": 0, "ms": ms}

    async def discover_simultaneous(
        self,
        sources: list[str],
        keywords: list[str],
        regions: Optional[list[str]] = None,
        require_phone: bool = False,
        auto_enrich_bin: bool = True,
        index_to_fts: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Run all chosen Kazakhstan sources in parallel, merge results, waterfall-enrich BIN/Director, and index into FTS5."""
        t0 = time.monotonic()
        clean_regions = [r.lower() for r in (regions or ["all"]) if r]
        clean_keywords = [k.strip() for k in (keywords or []) if k.strip()]
        clean_sources = list(dict.fromkeys(s.strip().lower() for s in (sources or []) if s.strip()))
        if not clean_sources:
            clean_sources = ["satu_kz", "uchet_pk"]

        # Step 1: Execute all enabled sources CONCURRENTLY via asyncio.gather
        tasks = [
            self._run_single_source(src, clean_keywords, clean_regions, limit=min(limit, 30))
            for src in clean_sources
        ]
        results = await asyncio.gather(*tasks)

        source_report: dict[str, Any] = {}
        raw_all: list[dict[str, Any]] = []
        for src_id, rows, rep in results:
            source_report[src_id] = rep
            raw_all.extend(rows)

        # Step 2: Deduplicate & merge cross-source entities
        merged = self.deduplicate_and_merge(raw_all)

        # Step 3: Automatic Waterfall Enrichment (resolve missing BIN & Director via free PK.Uchet.kz!)
        enriched_count = 0
        if auto_enrich_bin and merged:
            to_enrich = [rec for rec in merged if (not rec.get("bin") or not rec.get("director")) and rec.get("company")][:12]
            if to_enrich:
                sem = asyncio.Semaphore(4)
                async with httpx.AsyncClient(transport=self.transport, timeout=8.0) as client:
                    async def _wf_enrich(rec: dict[str, Any]) -> bool:
                        q_name = clean_company_search_term(rec["company"])
                        if len(q_name) < 3:
                            return False
                        async with sem:
                            try:
                                found = await self.uchet.lookup_one(q_name, client=client)
                                if not found:
                                    return False
                                # Verify strict token/name match before merging to prevent false BIN matches (e.g. "Amanat" != "AmanatServiceStroy")
                                k1 = normalize_company_key(rec["company"])
                                k2 = normalize_company_key(found["company"])
                                t1 = k1.split()
                                t2 = k2.split()
                                strong_match = bool(
                                    k1
                                    and k2
                                    and (
                                        k1 == k2
                                        or (len(t1) >= 2 and set(t1).issubset(set(t2)))
                                        or (len(t2) >= 2 and set(t2).issubset(set(t1)))
                                    )
                                )
                                if strong_match:
                                    updated = self._merge_records(rec, found)
                                    rec.clear()
                                    rec.update(updated)
                                    return True
                            except Exception:
                                return False
                        return False

                    wf_flags = await asyncio.gather(*[_wf_enrich(r) for r in to_enrich])
                    enriched_count = sum(1 for f in wf_flags if f)

                # Re-deduplicate in case two records resolved to the same 12-digit BIN
                merged = self.deduplicate_and_merge(merged)

        # Step 4: Score completeness (ICP readiness) & filter require_phone if requested
        final_items: list[dict[str, Any]] = []
        for rec in merged:
            if require_phone and not rec.get("phone_e164"):
                continue
            score = 0
            if rec.get("phone_e164"):
                score += 35 if rec.get("whatsapp_active") else 15
            if rec.get("bin"):
                score += 20
            if rec.get("director"):
                score += 20
            if rec.get("email") or rec.get("website"):
                score += 10
            if len(rec.get("sources") or []) > 1:
                score += 15  # Verified across multiple independent sources
            elif rec.get("contract_sum_kzt"):
                score += 10
            rec["icp_score"] = min(100, score)
            final_items.append(rec)

        final_items.sort(key=lambda x: (1 if x.get("phone_e164") else 0, x.get("icp_score", 0), 1 if x.get("bin") else 0), reverse=True)
        final_items = final_items[:limit]

        # Step 5: Permanently index newly discovered companies into SQLite FTS5 Market Seeds
        indexed_new = 0
        if index_to_fts and self.market_engine is not None and final_items:
            seed_rows = [
                {
                    "bin": r.get("bin") or "",
                    "company": r.get("company") or "",
                    "city": r.get("city") or "",
                    "category": r.get("category") or "",
                    "activity": " · ".join(x for x in [r.get("oked", ""), r.get("director", ""), r.get("address", "")] if x),
                    "phone": r.get("phone_e164") or r.get("phone_raw") or "",
                    "email": r.get("email") or "",
                    "contract_sum_kzt": r.get("contract_sum_kzt") or 0.0,
                    "source": r.get("source") or "kz_multi",
                }
                for r in final_items
                if r.get("source") != "csv_seed"
            ]
            if seed_rows:
                indexed_new = self.market_engine.upsert_seeds(seed_rows)

        return {
            "items": final_items,
            "total_raw": len(raw_all),
            "total_merged": len(final_items),
            "enriched_count": enriched_count,
            "indexed_to_fts": indexed_new,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "source_report": source_report,
        }

    async def enrich_by_bin(self, bin_code: str = "", company_name: str = "") -> dict[str, Any]:
        """Concurrently enrich a company by 12-digit BIN or company name across Uchet-PK, eGov, Statsnet, and FTS5."""
        t0 = time.monotonic()
        clean_bin = _sanitize_bin(bin_code)
        lookup_val = clean_bin or clean_company_search_term(company_name)
        if not lookup_val or len(lookup_val) < 2:
            raise ValueError("provide_valid_bin_or_company_name")

        async def _safe(coro):
            try:
                return await coro
            except Exception as exc:
                return {"_error": f"{exc.__class__.__name__}: {exc}"}

        uchet_res, egov_res, statsnet_res = await asyncio.gather(
            _safe(self.uchet.lookup_one(lookup_val)),
            _safe(self.egov.lookup_bin(clean_bin) if clean_bin and self.egov.api_key else asyncio.sleep(0, result=None)),
            _safe(self.statsnet.enrich_bin(clean_bin) if clean_bin and self.statsnet.api_key else asyncio.sleep(0, result=None)),
        )

        merged: dict[str, Any] = {
            "bin": clean_bin,
            "company": company_name.strip(),
            "director": "",
            "city": "",
            "address": "",
            "kato_code": "",
            "oked": "",
            "legal_status": "unknown",
            "phone_e164": "",
            "phone_raw": "",
            "email": "",
            "tax_paid_kzt": None,
            "risk_level": "unknown",
            "markdown_dossier": "",
            "sources_consulted": [],
            "errors": {},
        }

        for src_name, res in [("uchet_pk", uchet_res), ("egov_open_data", egov_res), ("statsnet", statsnet_res)]:
            if isinstance(res, dict) and "_error" in res:
                merged["errors"][src_name] = res["_error"]
            elif isinstance(res, dict) and res:
                merged["sources_consulted"].append(src_name)
                if res.get("bin") and not merged["bin"]:
                    merged["bin"] = res["bin"]
                if res.get("company"):
                    merged["company"] = res["company"]
                for k in ("director", "city", "address", "kato_code", "oked", "legal_status", "risk_level", "markdown_dossier"):
                    if res.get(k) and (not merged.get(k) or merged.get(k) == "unknown"):
                        merged[k] = res[k]
                if res.get("tax_paid_kzt"):
                    merged["tax_paid_kzt"] = res["tax_paid_kzt"]

        # Also check local FTS5 index for known phones/contracts by BIN or name
        if self.market_engine is not None:
            q_fts = merged["bin"] or clean_company_search_term(merged["company"])
            if q_fts:
                local = self.market_engine.search(query=q_fts, limit=3)
                for row in local.get("items") or []:
                    if (merged["bin"] and row.get("bin") == merged["bin"]) or (
                        normalize_company_key(row.get("company") or "") == normalize_company_key(merged["company"])
                    ):
                        if "fts5_local" not in merged["sources_consulted"]:
                            merged["sources_consulted"].append("fts5_local")
                        phones = extract_valid_kz_phones(row.get("phone") or "")
                        if phones and not merged["phone_e164"]:
                            merged["phone_e164"] = phones[0]
                            merged["phone_raw"] = row.get("phone") or phones[0]
                        if row.get("email") and not merged["email"]:
                            merged["email"] = row["email"]
                        if row.get("contract_sum_kzt") and not merged.get("tax_paid_kzt"):
                            merged["contract_sum_kzt"] = row["contract_sum_kzt"]

        # If we resolved a BIN via Uchet-PK from company_name, retry Statsnet once if key is configured
        if not clean_bin and merged["bin"] and self.statsnet.api_key and "statsnet" not in merged["sources_consulted"]:
            st2 = await _safe(self.statsnet.enrich_bin(merged["bin"]))
            if isinstance(st2, dict) and "_error" not in st2:
                merged["sources_consulted"].append("statsnet")
                if st2.get("markdown_dossier"):
                    merged["markdown_dossier"] = st2["markdown_dossier"]
                if st2.get("tax_paid_kzt"):
                    merged["tax_paid_kzt"] = st2["tax_paid_kzt"]

        merged["found"] = bool(merged["sources_consulted"])
        merged["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return merged


def parse_bulk_registry_file(raw_bytes: bytes, filename: str, source_tag: str = "egov_bulk") -> list[dict[str, Any]]:
    """Parse bulk CSV or JSON datasets (data.egov.kz, e-Ondiris KTP, custom B2B exports) into MarketSearchEngine rows."""
    lower_name = (filename or "").lower()
    records: list[dict[str, Any]] = []

    if lower_name.endswith(".json"):
        parsed = json.loads(raw_bytes.decode("utf-8-sig", errors="replace"))
        items = parsed if isinstance(parsed, list) else (parsed.get("items") or parsed.get("data") or parsed.get("results") or [])
        for row in items:
            if not isinstance(row, dict):
                continue
            norm = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
            comp = str(
                norm.get("nameru") or norm.get("name_ru") or norm.get("company")
                or norm.get("company_name") or norm.get("name") or norm.get("namekz") or ""
            ).strip()
            if not comp:
                continue
            addr = str(norm.get("address") or norm.get("legaladdress") or "").strip()
            city_val = str(norm.get("city") or norm.get("region") or "").strip()
            if not city_val:
                city_val, _ = city_from_kato_or_address(norm.get("katocode") or norm.get("kato"), addr)
            phones = extract_valid_kz_phones(str(norm.get("phone") or norm.get("phone_raw") or norm.get("mobile") or ""))
            records.append({
                "bin": _sanitize_bin(norm.get("bin") or norm.get("bin_iin") or norm.get("iin_bin")),
                "company": comp,
                "city": city_val,
                "category": str(norm.get("okednameru") or norm.get("category") or norm.get("oked") or "KZ Registry").strip(),
                "activity": " · ".join(x for x in [
                    str(norm.get("okedcode") or "").strip(),
                    str(norm.get("directorfio") or norm.get("director") or norm.get("head_fullname") or "").strip(),
                    str(norm.get("activity") or "").strip(),
                ] if x),
                "phone": phones[0] if phones else str(norm.get("phone") or "").strip(),
                "email": str(norm.get("email") or "").strip(),
                "contract_sum_kzt": _parse_kzt_amount(norm.get("contract_sum_kzt") or norm.get("tax_paid_kzt") or norm.get("sum")) or 0.0,
                "source": source_tag,
            })
        return records

    # CSV parsing (handles UTF-8-SIG, comma or semicolon delimiter)
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:2048]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    for row in reader:
        norm = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        comp = (
            norm.get("company") or norm.get("company_name") or norm.get("name")
            or norm.get("nameru") or norm.get("наименование") or norm.get("supplier_name") or ""
        )
        if not comp:
            continue
        addr = norm.get("address") or norm.get("адрес") or ""
        city_val = norm.get("city") or norm.get("город") or norm.get("region") or ""
        if not city_val:
            city_val, _ = city_from_kato_or_address(norm.get("kato") or norm.get("katocode"), addr)
        phones = extract_valid_kz_phones(
            norm.get("phone") or norm.get("phone_raw") or norm.get("mobile") or norm.get("телефон") or norm.get("best_phone_to_call") or ""
        )
        records.append({
            "bin": _sanitize_bin(norm.get("bin") or norm.get("bin_iin") or norm.get("бии") or norm.get("бин")),
            "company": comp,
            "city": city_val,
            "category": norm.get("category") or norm.get("okednameru") or norm.get("окэд") or norm.get("sample_item") or "KZ Registry",
            "activity": " · ".join(x for x in [
                norm.get("activity") or "",
                norm.get("director") or norm.get("director_name") or norm.get("руководитель") or "",
            ] if x),
            "phone": phones[0] if phones else (norm.get("phone") or norm.get("телефон") or ""),
            "email": norm.get("email") or "",
            "contract_sum_kzt": _parse_kzt_amount(norm.get("contract_sum_kzt") or norm.get("sum") or norm.get("налоги")) or 0.0,
            "source": source_tag,
        })
    return records
