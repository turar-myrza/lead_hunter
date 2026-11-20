"""
Lead Hunter Main CLI Controller
Unified entrypoint for multiple data sources and customer conversion pipelines:
- Procurements: Goszakup (goszakup.gov.kz) & MP.kz (mp.kz)
- Website Scraping: Custom corporate domains
- Telegram Scraping: Public business channels & Group Member Outreach
- AI PBX Telephony: Dynamic product voice pitching via Bitrix PBX / Asterisk
- Document Engine: Dynamic Commercial Offer (КП) generator (.docx -> .pdf) & WhatsApp dispatch
"""

import argparse
import sys
import os

from procurements.goszakup.scraper import run_contract_lead_hunter
from procurements.mp_kz.scraper import MPKzScraper
from website_scraping.crawler import WebsiteContactCrawler
from telegram_scraping.reader import TelegramLeadReader
from telegram_scraping.outreach_engine import run_telegram_outreach
from calling.pbx_caller import call_lead
from documents.offer_generator import generate_commercial_offer
from integrations.whatsapp_sender import (
    send_whatsapp_document,
    send_whatsapp_template,
    send_whatsapp_template_with_document
)

VERSION = "1.5.4"


def run_demo_tires_offer(client_company: str = "ТОО «ТАЙЫРОВ»", client_name: str = "Даурен", send_wa_phone: str = None):
    """Generate sample commercial offer with real vehicle tires in Kazakhstan."""
    tires_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tires")

    items = [
        {
            "name": "Viatti Strada Asimmetrico (V-130) 205/55 R16 91V",
            "description": "Легковая летняя шина с асимметричным рисунком протектора. Центральные кольцевые канавки обеспечивают быстрый отвод воды из пятна контакта. Усиленная внешняя плечевая зона гарантирует курсовую устойчивость при маневрировании.",
            "unit": "шт",
            "price": "26 800",
            "image_path": os.path.join(tires_dir, "viatti_v130.jpg"),
        },
        {
            "name": "Cordiant Comfort 2 205/55 R16 94V XL",
            "description": "Комфортная летняя шина повышенной прочности (XL). Технологии DRY-COR и WET-COR для максимального сцепления на сухом и мокром асфальте. Оптимизированный шаг рисунка протектора снижает акустический шум.",
            "unit": "шт",
            "price": "28 400",
            "image_path": os.path.join(tires_dir, "cordiant_comfort2.jpg"),
        },
        {
            "name": "Triangle AdvanteX TC101 215/60 R16 99V XL",
            "description": "Премиальная шоссейная шина для седанов и кроссоверов. Четыре глубоких продольных дренажных канала. Закрытый дизайн плечевых блоков снижает сопротивление качению и расход топлива.",
            "unit": "шт",
            "price": "27 900",
            "image_path": os.path.join(tires_dir, "triangle_tc101.jpg"),
        },
        {
            "name": "Viatti Bosco A/T (V-237) 215/65 R16 98H",
            "description": "Вседорожная шина (A/T) для кроссоверов и внедорожников. Усиленная конструкция каркаса с технологией переменной жесткости боковины (VSS). Грунтозацепы в плечевой зоне обеспечивают тягу на грунтовых дорогах.",
            "unit": "шт",
            "price": "32 500",
            "image_path": os.path.join(tires_dir, "viatti_v237.jpg"),
        },
        {
            "name": "Triangle Trin PL01 205/55 R16 94R XL",
            "description": "Зимняя фрикционная шина (липучка). Плотная ламелизация протектора обеспечивает надежный зацеп на укатанном снегу и обледенелых участках. Морозостойкая резиновая смесь сохраняет эластичность до -40°C.",
            "unit": "шт",
            "price": "25 500",
            "image_path": os.path.join(tires_dir, "triangle_pl01.jpg"),
        },
    ]

    print(f"Generating Commercial Offer PDF for {client_company} ({client_name})...")
    res = generate_commercial_offer(
        client_company=client_company,
        client_name=client_name,
        items=items,
    )
    print(f"Generated DOCX: {res['docx']}")
    print(f"Generated PDF:  {res['pdf']}")

    if send_wa_phone:
        print(f"Sending WhatsApp Inquiry Template to {send_wa_phone}...")
        try:
            # 1. Send approved WhatsApp Template to initiate dialogue outside 24h window
            tpl_res = send_whatsapp_template(
                to_phone=send_wa_phone,
                template_name="send_catalog_via_url_qaz",
                language_code="kk",
                components=[
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [
                            {"type": "text", "text": "https://crmconsulting.bitrix24.kz/"}
                        ]
                    }
                ]
            )
            print(f"✅ WhatsApp Marketing Template Accepted: {tpl_res}")
        except Exception as e:
            print(f"⚠️ Template dispatch warning: {e}")

        print(f"Sending Commercial Offer PDF to {send_wa_phone}...")
        caption = f"Здравствуйте, {client_name}! Направляем официальное коммерческое предложение по автомобильным шинам (Cordiant, Viatti, Triangle) от ТОО «АВРОРА-2014» с фотографиями всех моделей и ценами."
        try:
            wa_res = send_whatsapp_document(
                to_phone=send_wa_phone,
                pdf_path=res["pdf"],
                caption=caption,
                filename="КП_Шины_АВРОРА_2014.pdf",
            )
            print(f"✅ WhatsApp Document Dispatch Result: {wa_res}")
        except Exception as e:
            print(f"⚠️ Document dispatch notice: {e}")

    return res


def main():
    parser = argparse.ArgumentParser(
        description=f"Lead Hunter Suite v{VERSION} - Multi-source B2B/B2G Lead Extraction Engine"
    )
    subparsers = parser.add_subparsers(dest="command", help="Engine subsystem to run")

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

    # 7. Document Generator (КП) & WhatsApp Dispatch
    offer_parser = subparsers.add_parser("offer", help="Generate Commercial Offer (.docx -> .pdf) and send via WhatsApp")
    offer_parser.add_argument("--company", default="ТОО «ТАЙЫРОВ»", help="Customer company name")
    offer_parser.add_argument("--ceo", default="Даурен", help="Customer CEO / contact person")
    offer_parser.add_argument("--demo-tires", action="store_true", default=True, help="Use vehicle tires demonstration dataset")
    offer_parser.add_argument("--send-wa", default=None, help="Phone number to send generated PDF via WhatsApp Cloud API")

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

    elif args.command == "offer":
        print(f"=== Running Commercial Offer Generator (Lead Hunter v{VERSION}) ===")
        if args.demo_tires:
            run_demo_tires_offer(
                client_company=args.company,
                client_name=args.ceo,
                send_wa_phone=args.send_wa,
            )


if __name__ == "__main__":
    main()
