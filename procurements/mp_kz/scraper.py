"""
MP.kz (Mitwork Procurement) Lead Scraper
Connects to https://mp.kz/ and retrieves organizer/supplier company leads,
platform hotline contacts, BINs, addresses, and published phone numbers.
"""

import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

from common.utils import clean_cell, normalize_phone, save_csv_dual

VERSION = "1.2.0"

BASE_URL = "https://mp.kz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


class MPKzScraper:
    """Scraper engine for MP.kz electronic trading platform."""

    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self.init_session()

    def init_session(self):
        """Initializes session and acquires cookies from MP.kz."""
        try:
            r = self.session.get(BASE_URL, timeout=self.timeout)
            if r.status_code == 200:
                print(f"[MP.kz] Connected successfully. Cookies: {len(self.session.cookies)}")
            else:
                print(f"[MP.kz] Warning: Status {r.status_code}")
        except Exception as e:
            print(f"[MP.kz] Initial connection error: {e}", file=sys.stderr)

    def get_platform_contacts(self) -> dict:
        """Retrieves official platform hotline, support email, and office address."""
        url = f"{BASE_URL}/contacts/"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                return {}
            soup = BeautifulSoup(r.text, "html.parser")
            html = r.text

            phones = set(re.findall(r"(?:\+7|8)[\s\-\(\)\d]{9,15}\d", html))
            emails = set(re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html))

            # Filter valid KZ numbers
            valid_phones = [p for p in phones if len(re.sub(r"\D", "", p)) >= 10]

            return {
                "source": "mp.kz /contacts/",
                "hotline_phones": " / ".join(valid_phones),
                "emails": " / ".join(emails),
                "working_hours": "8:00 - 18:00",
                "operator": "ТОО «Реал тайм инжиниринг»",
            }
        except Exception as e:
            print(f"[MP.kz] Error fetching /contacts/: {e}", file=sys.stderr)
            return {}

    def get_active_tenders(self, max_pages: int = 2) -> list:
        """Retrieves active tenders from /tenders/."""
        tenders = []
        for page in range(1, max_pages + 1):
            url = f"{BASE_URL}/tenders/?page={page}" if page > 1 else f"{BASE_URL}/tenders/"
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    break
                soup = BeautifulSoup(r.text, "html.parser")

                for a in soup.find_all("a"):
                    href = a.get("href", "")
                    if "/tender/" in href:
                        title = a.get_text(strip=True)
                        m = re.search(r"/tender/(\d+)", href)
                        if m:
                            tid = m.group(1)
                            full_url = f"{BASE_URL}{href}" if href.startswith("/") else href
                            tenders.append({
                                "tender_id": tid,
                                "title": title,
                                "url": full_url,
                            })
                time.sleep(0.3)
            except Exception as e:
                print(f"[MP.kz] Error on page {page}: {e}", file=sys.stderr)
                break
        return tenders

    def get_tender_and_company_details(self, tender_url: str) -> dict:
        """Fetches tender page and extracts company / organizer link and phone."""
        try:
            r = self.session.get(tender_url, timeout=self.timeout)
            if r.status_code != 200:
                return {}
            soup = BeautifulSoup(r.text, "html.parser")

            company_id = ""
            company_name = ""
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if "/company/" in href:
                    m = re.search(r"/company/(\d+)", href)
                    if m:
                        company_id = m.group(1)
                        company_name = a.get_text(strip=True)
                        break

            # Check for direct phone written inside tender description
            desc_text = soup.get_text("\n", strip=True)
            phones = re.findall(r"(?:тел|телефон|whatsapp|ватсап|сот)[\s.:]*([+\d\s\(\)\-]{9,20})", desc_text, re.I)
            contact_phone = phones[0].strip() if phones else ""

            return {
                "company_id": company_id,
                "company_name": company_name,
                "tender_contact_phone": contact_phone,
            }
        except Exception:
            return {}

    def get_company_profile(self, company_id: str) -> dict:
        """Fetches company profile /company/{id} to extract BIN, address, and phone."""
        url = f"{BASE_URL}/company/{company_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                return {}
            soup = BeautifulSoup(r.text, "html.parser")

            name = soup.title.string.replace("- ЭТП", "").strip() if soup.title else ""
            text = soup.get_text("\n", strip=True)

            bin_m = re.search(r"БИН:?\s*(\d{12})", text)
            bin_iin = bin_m.group(1) if bin_m else ""

            # Extract Phone
            phone = ""
            for h3 in soup.find_all(["h3", "strong", "span"]):
                if "телефоны" in h3.get_text(strip=True).lower():
                    sibling = h3.find_next_sibling()
                    if sibling:
                        t = sibling.get_text(" ", strip=True)
                        if "- не указаны -" not in t.lower():
                            phone = t
                    break

            # Extract Address
            address = ""
            for h3 in soup.find_all(["h3", "strong", "span"]):
                if "адрес" in h3.get_text(strip=True).lower():
                    sibling = h3.find_next_sibling()
                    if sibling:
                        address = sibling.get_text(" ", strip=True)
                    break

            # Extract Legal Form (ТОО / АО / ИП)
            legal_form = ""
            form_m = re.search(r"Организационно-правовая форма:\s*([A-Za-zА-Яа-я]+)", text)
            if form_m:
                legal_form = form_m.group(1)

            return {
                "company_id": company_id,
                "company_name": name,
                "legal_form": legal_form,
                "bin_iin": bin_iin,
                "phone": phone,
                "address": address,
                "company_url": url,
            }
        except Exception as e:
            print(f"[MP.kz] Error fetching company {company_id}: {e}", file=sys.stderr)
            return {}

    def run(self, output_csv: str = "data/mp_kz_leads.csv", max_tenders: int = 25):
        """Executes full scraping cycle and saves leads to CSV."""
        print(f"=== Starting MP.kz Lead Scraper v{VERSION} ===")

        # 1. Platform Support & Hotline
        platform = self.get_platform_contacts()
        print(f"[MP.kz] Platform Hotline: {platform.get('hotline_phones')}")
        print(f"[MP.kz] Platform Email:   {platform.get('emails')}")

        # 2. Scrape Tenders and Organizers
        print("\n[MP.kz] Scanning active commercial tenders...")
        tenders = self.get_active_tenders(max_pages=2)
        print(f"[MP.kz] Discovered {len(tenders)} active tenders.")

        leads = []
        seen_companies = set()

        for t in tenders[:max_tenders]:
            t_details = self.get_tender_and_company_details(t["url"])
            time.sleep(0.2)
            cid = t_details.get("company_id")

            c_profile = {}
            if cid and cid not in seen_companies:
                seen_companies.add(cid)
                print(f"  -> Extracting company profile: ID {cid}...")
                c_profile = self.get_company_profile(cid)
                time.sleep(0.2)

            comp_name = c_profile.get("company_name") or t_details.get("company_name") or "Unknown"
            bin_iin = c_profile.get("bin_iin", "")
            comp_phone = c_profile.get("phone", "")
            tender_phone = t_details.get("tender_contact_phone", "")
            active_phone = comp_phone if comp_phone else tender_phone

            leads.append({
                "company_name": comp_name,
                "bin_iin": bin_iin,
                "legal_form": c_profile.get("legal_form", ""),
                "phone": active_phone,
                "address": c_profile.get("address", ""),
                "tender_title": t["title"],
                "tender_url": t["url"],
                "company_url": c_profile.get("company_url", ""),
                "source": "mp.kz",
            })

        fieldnames = [
            "company_name",
            "bin_iin",
            "legal_form",
            "phone",
            "address",
            "tender_title",
            "tender_url",
            "company_url",
            "source",
        ]

        p1, p2 = save_csv_dual(output_csv, fieldnames, leads)
        print(f"\n[DONE] Saved {len(leads)} MP.kz leads to:\n  - {p1}\n  - {p2}")
        return leads


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/mp_kz_leads.csv"
    scraper = MPKzScraper()
    scraper.run(out)
