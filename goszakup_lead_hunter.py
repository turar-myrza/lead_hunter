"""
Goszakup Lead Hunter Module
Collects B2G suppliers who won medical disposable contracts in Almaty & Shymkent.
Extracts operational phone numbers directly from inside signed contract PDFs.
Sanitizes cell data to prevent CSV column shifts.
"""

import csv
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from bs4 import BeautifulSoup

VERSION = "1.2.0"

BASE_URL = "https://goszakup.gov.kz"
OLD_BASE_URL = "https://old.goszakup.gov.kz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch_url(url: str, retries: int = 3, delay: float = 0.5) -> str:
    """Fetch URL text with retries and timeout."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as res:
                return res.read().decode("utf-8", errors="ignore")
        except Exception as e:
            if attempt == retries - 1:
                return ""
            time.sleep(delay * (attempt + 1))
    return ""


def download_bytes(url: str, retries: int = 3, delay: float = 0.5) -> bytes:
    """Download binary data (e.g. PDF) with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=20) as res:
                return res.read()
        except Exception as e:
            if attempt == retries - 1:
                return b""
            time.sleep(delay * (attempt + 1))
    return b""


def extract_supplier_from_contract_pdf(pdf_bytes: bytes) -> dict:
    """
    Parses a signed contract PDF using pdftotext.
    Extracts operational phone, director name, bank account (IIK), and address
    from Section 14 (Реквизиты Сторон / Өнім беруші / Поставщик).
    """
    if not pdf_bytes:
        return {}

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        txt = subprocess.check_output(["pdftotext", tmp_path, "-"]).decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[WARN] pdftotext failed: {e}", file=sys.stderr)
        txt = ""
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not txt:
        return {}

    details = {
        "contract_phone": "",
        "contract_director": "",
        "contract_iik": "",
        "contract_address": "",
        "contract_bin": "",
    }

    # Search for the Supplier requisites block (Өнім беруші / Поставщик)
    blocks = re.findall(
        r"(?:Өнім\s*беруші|Поставщик)(?:\s*\([^)]*\))?:?\s*\n(.*?)(?=\n\s*(?:Расшифровка|Осы\s*құжат|Данный\s*документ|Приложение|\Z))",
        txt,
        re.DOTALL | re.IGNORECASE,
    )

    target_block = ""
    if blocks:
        for b in reversed(blocks):
            if any(k in b.lower() for k in ["тел", "иик", "жск", "бин", "иин", "бсн", "жсн"]):
                target_block = b
                break
        if not target_block:
            target_block = blocks[-1]

    if target_block:
        phone_match = re.search(r"(?:Тел\.?|Телефон):?\s*([^\n\r]+)", target_block, re.IGNORECASE)
        if phone_match:
            details["contract_phone"] = phone_match.group(1).strip()

        dir_match = re.search(r"(?:Руководитель|Басшысы|Директор|Председатель|Төраға):?\s*([^\n\r]+)", target_block, re.IGNORECASE)
        if dir_match:
            details["contract_director"] = dir_match.group(1).strip()

        iik_match = re.search(r"(?:ИИК|ЖСК)\s*([A-Z0-9]{20})", target_block, re.IGNORECASE)
        if iik_match:
            details["contract_iik"] = iik_match.group(1).strip()

        bin_match = re.search(r"(?:БИН|ИИН|БСН|ЖСН)[^\d]*(\d{12})", target_block, re.IGNORECASE)
        if bin_match:
            details["contract_bin"] = bin_match.group(1).strip()

    if not details["contract_phone"]:
        all_phones = re.findall(r"(?:Тел\.?|Телефон):?\s*([^\n\r]+)", txt, re.IGNORECASE)
        if len(all_phones) >= 2:
            details["contract_phone"] = all_phones[1].strip()
        elif len(all_phones) == 1:
            details["contract_phone"] = all_phones[0].strip()

    return details


def get_contracts_for_announcement(anno_id: str):
    """Finds signed contract IDs and direct PDF download URLs for an announcement."""
    url = f"{BASE_URL}/ru/announce/index/{anno_id}?tab=contracts"
    html = fetch_url(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    contract_ids = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if "cpublic/show/" in href:
            m = re.search(r"cpublic/show/(\d+)", href)
            if m:
                cid = m.group(1)
                if cid not in contract_ids:
                    contract_ids.append(cid)

    contracts_info = []
    for cid in contract_ids:
        c_url = f"{OLD_BASE_URL}/ru/egzcontract/cpublic/contract/{cid}"
        c_html = fetch_url(c_url)
        if not c_html:
            continue

        c_soup = BeautifulSoup(c_html, "html.parser")
        pdf_url = None
        for a in c_soup.find_all("a"):
            href = a.get("href", "")
            txt = a.get_text(strip=True)
            if "download_file" in href:
                pdf_url = href
                if "contract_orig" in txt:
                    break

        if pdf_url:
            contracts_info.append({
                "contract_id": cid,
                "contract_url": c_url,
                "pdf_url": pdf_url,
            })
    return contracts_info


def get_completed_lots(keyword: str, kato: str, year: str = "2026", count: int = 50):
    """
    Query Goszakup lot search for completed procurements.
    Status 360 = Закупка состоялась (Procurement awarded)
    """
    params = [
        ("count_record", str(count)),
        ("filter[status][]", "360"),
        ("filter[year]", year),
        ("filter[name]", keyword),
        ("filter[kato]", kato),
    ]
    url = f"{BASE_URL}/ru/search/lots?{urllib.parse.urlencode(params)}"
    html = fetch_url(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 2:
        return []

    results = []
    rows = tables[1].find_all("tr")[1:]
    for r in rows:
        cells = [td.get_text(" ", strip=True) for td in r.find_all(["td", "th"])]
        if len(cells) < 7:
            continue

        lot_code = cells[0].split()[0] if cells[0] else ""
        lot_name = cells[2]
        lot_sum = cells[4]

        anno_link = None
        for a in r.find_all("a"):
            href = a.get("href", "")
            if "/announce/index/" in href:
                anno_link = href
                break

        if anno_link:
            anno_id_match = re.search(r"/announce/index/(\d+)", anno_link)
            if anno_id_match:
                anno_id = anno_id_match.group(1)
                results.append({
                    "lot_code": lot_code,
                    "lot_name": lot_name,
                    "lot_sum": lot_sum,
                    "anno_id": anno_id,
                    "anno_url": f"{BASE_URL}{anno_link}" if anno_link.startswith("/") else anno_link,
                })
    return results


def get_winner_from_announcement(anno_id: str):
    """Fetch winners tab of announcement to extract supplier ID and BIN/IIN."""
    url = f"{BASE_URL}/ru/announce/index/{anno_id}?tab=winners"
    html = fetch_url(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    winners = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 5 and "Закупка состоялась" in cells[3]:
            winner_text = cells[4]
            supp_id = None
            for a in tr.find_all("a"):
                href = a.get("href", "")
                if "supplierreg/show/" in href:
                    m = re.search(r"supplierreg/show/(\d+)", href)
                    if m:
                        supp_id = m.group(1)
                        break

            bin_match = re.match(r"^(\d{12})\b", winner_text)
            bin_iin = bin_match.group(1) if bin_match else ""
            raw_name = winner_text[13:].strip() if bin_iin else winner_text.strip()

            winners.append({
                "supplier_id": supp_id,
                "bin_iin": bin_iin,
                "company_name": raw_name,
            })
    return winners


def get_supplier_profile_details(supp_id: str):
    """Extract default contact information from supplier registry profile."""
    url = f"{OLD_BASE_URL}/ru/registry/show_supplier/{supp_id}"
    html = fetch_url(url)
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    details = {
        "bin": "",
        "name_ru": "",
        "profile_phone": "",
        "profile_email": "",
        "profile_fio": "",
        "address": "",
    }

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 2:
            label = cells[0].lower()
            val = cells[1].strip()
            if "бин участника" in label:
                details["bin"] = val
            elif "наименование на рус" in label:
                details["name_ru"] = val
            elif "контактный телефон" in label:
                details["profile_phone"] = val
            elif "e-mail" in label:
                details["profile_email"] = val
            elif "фио" in label:
                details["profile_fio"] = val
            elif "полный адрес(рус)" in label or "адрес местонахождения" in label:
                details["address"] = val

    return details


def clean_cell(val) -> str:
    """Sanitizes cell content to ensure zero comma shifts and clean formatting."""
    if not val:
        return ""
    v = str(val).replace("\r", " ").replace("\n", " ")
    v = v.replace('"', '').replace("'", "")
    # Convert internal commas to ' / ' to prevent CSV column splitting
    v = re.sub(r"\s*,\s*", " / ", v)
    # Strip semicolons
    v = v.replace(";", " | ")
    # Clean leading / trailing slashes
    v = re.sub(r"\s*/\s*$", "", v)
    v = re.sub(r"^\s*/\s*", "", v)
    return re.sub(r"\s+", " ", v).strip()


def run_contract_lead_hunter(output_csv: str = "medical_suppliers_contract_phones_2026.csv"):
    """
    Main pipeline execution:
    Scans awarded medical lots -> extracts contract PDFs -> parses real operational phones from PDF.
    """
    print(f"=== Starting Goszakup Contract Lead Hunter v{VERSION} ===")
    cities = [
        ("Almaty", "750000000"),
        ("Shymkent", "790000000"),
    ]
    keywords = [
        "шприц",
        "перчатки",
        "катетер",
        "медицинских изделий",
        "бинт",
    ]

    leads = {}
    processed_annos = set()

    for city_name, kato_code in cities:
        print(f"\n[CITY] Scanning {city_name} (KATO: {kato_code})...")
        for kw in keywords:
            print(f"  [KEYWORD] Searching: '{kw}'...")
            lots = get_completed_lots(keyword=kw, kato=kato_code, year="2026", count=30)
            print(f"    Found {len(lots)} awarded lots.")

            for lot in lots:
                anno_id = lot["anno_id"]
                if anno_id in processed_annos:
                    continue
                processed_annos.add(anno_id)

                contracts = get_contracts_for_announcement(anno_id)
                winners = get_winner_from_announcement(anno_id)
                time.sleep(0.2)

                contract_details = {}
                contract_pdf_url = ""
                if contracts:
                    first_c = contracts[0]
                    contract_pdf_url = first_c["pdf_url"]
                    print(f"    [CONTRACT PDF] Downloading PDF for contract {first_c['contract_id']}...")
                    pdf_bytes = download_bytes(first_c["pdf_url"])
                    if pdf_bytes:
                        contract_details = extract_supplier_from_contract_pdf(pdf_bytes)
                        if contract_details.get("contract_phone"):
                            print(f"      >>> REAL CONTRACT PHONE FOUND: {contract_details['contract_phone']}")

                for w in winners:
                    supp_id = w["supplier_id"]
                    bin_iin = w["bin_iin"]
                    key = bin_iin or supp_id
                    if not key or key in leads:
                        continue

                    profile = {}
                    if supp_id:
                        profile = get_supplier_profile_details(supp_id)
                        time.sleep(0.15)

                    c_phone = contract_details.get("contract_phone", "")
                    p_phone = profile.get("profile_phone", "")
                    active_phone = c_phone if c_phone else p_phone

                    c_director = contract_details.get("contract_director", "")
                    p_director = profile.get("profile_fio", "")
                    active_director = c_director if c_director else p_director

                    leads[key] = {
                        "company_name": profile.get("name_ru") or w["company_name"],
                        "bin_iin": bin_iin or profile.get("bin", ""),
                        "city": city_name,
                        "real_contract_phone": c_phone,
                        "default_profile_phone": p_phone,
                        "best_phone_to_call": active_phone,
                        "email": profile.get("profile_email", ""),
                        "director_name": active_director,
                        "bank_account_iik": contract_details.get("contract_iik", ""),
                        "sample_item": lot["lot_name"],
                        "contract_sum_kzt": lot["lot_sum"],
                        "contract_pdf_url": contract_pdf_url,
                        "anno_url": lot["anno_url"],
                    }

    print(f"\n[DONE] Gathered {len(leads)} leads with contract analysis.")

    fieldnames = [
        "company_name",
        "bin_iin",
        "city",
        "real_contract_phone",
        "default_profile_phone",
        "best_phone_to_call",
        "email",
        "director_name",
        "bank_account_iik",
        "sample_item",
        "contract_sum_kzt",
        "contract_pdf_url",
        "anno_url",
    ]

    cleaned_leads = []
    for row in leads.values():
        c_row = {k: clean_cell(v) for k, v in row.items()}
        if c_row.get("email") and "@" not in c_row["email"]:
            c_row["email"] = ""
        cleaned_leads.append(c_row)

    # 1. Comma-separated (sanitized: zero internal commas, columns will never shift)
    with open(output_csv, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=",")
        writer.writeheader()
        writer.writerows(cleaned_leads)

    # 2. Semicolon-separated (standard format for Windows / Excel CIS)
    semi_path = output_csv.replace(".csv", "_semicolon.csv")
    with open(semi_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(cleaned_leads)

    print(f"[EXPORT] Saved {len(cleaned_leads)} leads to:")
    print(f"  - Comma separated:     '{output_csv}'")
    print(f"  - Semicolon separated: '{semi_path}'")
    return cleaned_leads


if __name__ == "__main__":
    out_file = sys.argv[1] if len(sys.argv) > 1 else "medical_suppliers_contract_phones_2026.csv"
    run_contract_lead_hunter(out_file)
