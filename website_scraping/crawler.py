"""
Website Contact Crawler
Extracts direct phone numbers, WhatsApp links, emails, and corporate addresses
from any company website (homepage, /contacts, /about, /kontakty).
"""

import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from common.utils import clean_cell, normalize_phone, save_csv_dual

VERSION = "1.3.0"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class WebsiteContactCrawler:
    """Crawls company websites to extract contact leads."""

    def __init__(self, timeout: int = 10):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout

    def crawl_site(self, base_url: str) -> dict:
        """Crawls root and contact subpages of a domain to find phone numbers and emails."""
        if not base_url.startswith("http://") and not base_url.startswith("https://"):
            base_url = "https://" + base_url

        parsed = urlparse(base_url)
        domain = parsed.netloc

        pages_to_check = [
            base_url,
            urljoin(base_url, "/contacts"),
            urljoin(base_url, "/contact"),
            urljoin(base_url, "/kontakty"),
            urljoin(base_url, "/about"),
            urljoin(base_url, "/o-nas"),
        ]

        phones = set()
        emails = set()
        whatsapp = set()
        addresses = []
        company_title = ""

        visited = set()
        for url in pages_to_check:
            if url in visited:
                continue
            visited.add(url)

            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    continue

                soup = BeautifulSoup(r.text, "html.parser")
                if not company_title and soup.title:
                    company_title = soup.title.string.strip()

                html = r.text

                # 1. Regex phones
                found_phones = re.findall(r"(?:\+7|8)[\s\-\(\)\d]{9,15}\d", html)
                for p in found_phones:
                    clean_p = re.sub(r"[^\d+]", "", p)
                    if len(clean_p) >= 10:
                        phones.add(p.strip())

                # 2. 'tel:' links
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("tel:"):
                        phones.add(href.replace("tel:", "").strip())
                    elif "wa.me/" in href or "api.whatsapp.com" in href:
                        whatsapp.add(href)

                # 3. Emails
                found_emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", html)
                for em in found_emails:
                    if not em.endswith(".png") and not em.endswith(".jpg"):
                        emails.add(em.lower())

                time.sleep(0.2)
            except Exception:
                continue

        return {
            "domain": domain,
            "company_name": company_title or domain,
            "phones": " / ".join(sorted(phones)),
            "emails": " / ".join(sorted(emails)),
            "whatsapp_links": " / ".join(sorted(whatsapp)),
            "url": base_url,
            "source": "website_scraping",
        }

    def crawl_multiple(self, url_list: list, output_csv: str = "data/website_leads.csv"):
        """Crawls a batch of websites and exports leads."""
        results = []
        for u in url_list:
            print(f"[Website Crawler] Scanning {u}...")
            data = self.crawl_site(u)
            results.append(data)

        fieldnames = ["domain", "company_name", "phones", "emails", "whatsapp_links", "url", "source"]
        p1, p2 = save_csv_dual(output_csv, fieldnames, results)
        print(f"[Website Crawler] Saved {len(results)} leads to {p1} & {p2}")
        return results


if __name__ == "__main__":
    test_urls = sys.argv[1:] if len(sys.argv) > 1 else ["https://mp.kz"]
    crawler = WebsiteContactCrawler()
    crawler.crawl_multiple(test_urls)
