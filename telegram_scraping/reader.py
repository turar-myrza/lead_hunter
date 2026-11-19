"""
Telegram Lead Reader
Extracts leads (phone numbers, usernames, emails, WhatsApp links)
from public Telegram channels and chat web previews (t.me/s/{channel})
without requiring Telegram API keys or phone logins.
"""

import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

from common.utils import clean_cell, normalize_phone, save_csv_dual

VERSION = "1.3.0"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class TelegramLeadReader:
    """Scrapes public Telegram channel previews for business leads."""

    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout

    def scrape_channel(self, channel_name: str, max_posts: int = 50) -> list:
        """
        Scrapes public channel web preview at https://t.me/s/{channel_name}.
        Extracts phone numbers, @mentions, and emails.
        """
        channel_name = channel_name.strip("@").replace("https://t.me/s/", "").replace("https://t.me/", "")
        url = f"https://t.me/s/{channel_name}"

        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                print(f"[Telegram] Failed to load channel {channel_name}: HTTP {r.status_code}")
                return []

            soup = BeautifulSoup(r.text, "html.parser")
            channel_title = soup.find("div", class_="tgme_channel_info_title")
            title_text = channel_title.get_text(strip=True) if channel_title else channel_name

            messages = soup.find_all("div", class_="tgme_widget_message_wrap")
            leads = []

            for msg in messages[-max_posts:]:
                text_elem = msg.find("div", class_="tgme_widget_message_text")
                if not text_elem:
                    continue
                text = text_elem.get_text("\n", strip=True)

                phones = set(re.findall(r"(?:\+7|8)[\s\-\(\)\d]{9,15}\d", text))
                emails = set(re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text))
                usernames = set(re.findall(r"@[a-zA-Z0-9_]{5,32}", text))

                # Post date / link
                time_elem = msg.find("time")
                post_date = time_elem.get("datetime", "") if time_elem else ""

                if phones or emails or usernames:
                    leads.append({
                        "channel": channel_name,
                        "channel_title": title_text,
                        "phones": " / ".join(phones),
                        "emails": " / ".join(emails),
                        "usernames": " / ".join(usernames),
                        "snippet": text[:150],
                        "post_date": post_date,
                        "source": "telegram_scraping",
                    })

            return leads
        except Exception as e:
            print(f"[Telegram] Error scraping {channel_name}: {e}", file=sys.stderr)
            return []

    def scrape_channels(self, channels: list, output_csv: str = "data/telegram_leads.csv"):
        """Scrapes multiple channels and saves leads to CSV."""
        all_leads = []
        for ch in channels:
            print(f"[Telegram] Scanning @{ch}...")
            l = self.scrape_channel(ch)
            print(f"  -> Found {len(l)} leads with contacts.")
            all_leads.extend(l)
            time.sleep(0.3)

        fieldnames = ["channel", "channel_title", "phones", "emails", "usernames", "snippet", "post_date", "source"]
        p1, p2 = save_csv_dual(output_csv, fieldnames, all_leads)
        print(f"[Telegram] Exported {len(all_leads)} leads to:\n  - {p1}\n  - {p2}")
        return all_leads


if __name__ == "__main__":
    chans = sys.argv[1:] if len(sys.argv) > 1 else ["kztenders"]
    reader = TelegramLeadReader()
    reader.scrape_channels(chans)
