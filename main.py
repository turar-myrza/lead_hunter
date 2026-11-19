"""
Lead Hunter Main CLI Controller
Unified entrypoint for multiple data sources:
- Procurements: Goszakup (goszakup.gov.kz) & MP.kz (mp.kz)
- Website Scraping: Custom corporate domains
- Telegram Scraping: Public business channels & Group Member Outreach
"""

import argparse
import sys

from procurements.goszakup.scraper import run_contract_lead_hunter
from procurements.mp_kz.scraper import MPKzScraper
from website_scraping.crawler import WebsiteContactCrawler
from telegram_scraping.reader import TelegramLeadReader
from telegram_scraping.outreach_engine import run_telegram_outreach
from calling.pbx_caller import call_lead

VERSION = "1.4.0"


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

    # 4. Telegram Scraping (Public Channel Previews)
    tg_parser = subparsers.add_parser("telegram", help="Scrape public Telegram channels for leads")
    tg_parser.add_argument("channels", nargs="+", help="Telegram channel usernames without @")
    tg_parser.add_argument("--output", default="data/telegram_leads.csv", help="Output CSV path")

    # 5. Telegram Group Outreach & Member Texting
    outreach_parser = subparsers.add_parser("outreach", help="Extract Telegram group members and text personalized inquiries")
    outreach_parser.add_argument("group", help="Group ID (e.g. -961056384) or @username")
    outreach_parser.add_argument("--campaign", default="bitrix_crm", choices=["bitrix_crm", "accounting", "china_logistics"], help="Campaign inquiry type")
    outreach_parser.add_argument("--limit", type=int, default=50, help="Max members to extract")
    outreach_parser.add_argument("--prefix", default=None, help="Optional text prefix for message (e.g. '[Тест бота]')")
    outreach_parser.add_argument("--live", action="store_true", help="Execute real sending (default is safe DRY-RUN preview)")
    outreach_parser.add_argument("--output", default="data/telegram_outreach_queue.csv", help="Output CSV path")

    # 6. AI PBX Product Caller Bridge
    call_parser = subparsers.add_parser("call", help="Originate an outbound AI voice call for product sales via Bitrix PBX")
    call_parser.add_argument("phone", help="Target phone number (must be in whitelist)")
    call_parser.add_argument("--product", action="append", dest="products", required=True, help="Product name to pitch (can specify multiple)")
    call_parser.add_argument("--details", default="", help="Detailed product specs, sizes, certifications")
    call_parser.add_argument("--ceo", default="", help="CEO / Contact name")
    call_parser.add_argument("--biz", default="", help="Company name")
    call_parser.add_argument("--lang", default="ru", choices=["ru", "kz"], help="Conversation language")

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

    elif args.command == "outreach":
        print(f"=== Running Telegram Group Member Outreach (Lead Hunter v{VERSION}) ===")
        run_telegram_outreach(
            group_id_or_username=args.group,
            campaign_type=args.campaign,
            limit=args.limit,
            live=args.live,
            prefix=args.prefix,
            output_csv=args.output,
        )

    elif args.command == "call":
        print(f"=== Running AI PBX Product Caller Bridge (Lead Hunter v{VERSION}) ===")
        call_lead(
            phone=args.phone,
            products=args.products,
            details=args.details,
            ceo=args.ceo,
            biz=args.biz,
            lang=args.lang,
        )


if __name__ == "__main__":
    main()
