"""
Lead Hunter Main CLI Controller
Unified entrypoint for multiple data sources:
- Procurements: Goszakup (goszakup.gov.kz) & MP.kz (mp.kz)
- Website Scraping: Custom corporate domains
- Telegram Scraping: Public business/tender channels
"""

import argparse
import sys

from procurements.goszakup.scraper import run_contract_lead_hunter
from procurements.mp_kz.scraper import MPKzScraper
from website_scraping.crawler import WebsiteContactCrawler
from telegram_scraping.reader import TelegramLeadReader

VERSION = "1.2.0"


def main():
    parser = argparse.ArgumentParser(
        description=f"Lead Hunter Suite v{VERSION} - Multi-source B2B/B2G Lead Extraction Engine"
    )
    subparsers = parser.add_subparsers(dest="command", help="Data source engine to run")

    # 1. MP.kz
    mp_parser = subparsers.add_parser("mp_kz", help="Scrape MP.kz electronic trading platform")
    mp_parser.add_argument("--output", default="data/mp_kz_leads.csv", help="Output CSV path")
    mp_parser.add_argument("--limit", type=int, default=25, help="Number of tenders to scan")

    # 2. Goszakup
    gz_parser = subparsers.add_parser("goszakup", help="Scrape Goszakup procurement portal & contract PDFs")
    gz_parser.add_argument("--output", default="data/medical_suppliers_contract_phones_2026.csv", help="Output CSV path")

    # 3. Website Scraping
    web_parser = subparsers.add_parser("website", help="Scrape contacts from custom company websites")
    web_parser.add_argument("urls", nargs="+", help="Website URLs to crawl")
    web_parser.add_argument("--output", default="data/website_leads.csv", help="Output CSV path")

    # 4. Telegram Scraping
    tg_parser = subparsers.add_parser("telegram", help="Scrape public Telegram channels for leads")
    tg_parser.add_argument("channels", nargs="+", help="Telegram channel usernames without @")
    tg_parser.add_argument("--output", default="data/telegram_leads.csv", help="Output CSV path")

    args = parser.parse_args()

    if not args.command or args.command == "mp_kz":
        print(f"=== Running MP.kz Scraper (Lead Hunter v{VERSION}) ===")
        out = getattr(args, "output", "data/mp_kz_leads.csv")
        lim = getattr(args, "limit", 25)
        scraper = MPKzScraper()
        scraper.run(output_csv=out, max_tenders=lim)

    elif args.command == "goszakup":
        print(f"=== Running Goszakup Scraper (Lead Hunter v{VERSION}) ===")
        run_contract_lead_hunter(args.output)

    elif args.command == "website":
        print(f"=== Running Website Crawler (Lead Hunter v{VERSION}) ===")
        crawler = WebsiteContactCrawler()
        crawler.crawl_multiple(args.urls, output_csv=args.output)

    elif args.command == "telegram":
        print(f"=== Running Telegram Reader (Lead Hunter v{VERSION}) ===")
        reader = TelegramLeadReader()
        reader.scrape_channels(args.channels, output_csv=args.output)


if __name__ == "__main__":
    main()
