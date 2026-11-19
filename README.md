# Lead Hunter Suite

Multi-source B2B & B2G Lead Generation and Contact Scraping Architecture for Kazakhstan.

## Architecture

```
lead_hunter/
├── common/                     # Shared utilities across all scrapers
│   ├── __init__.py
│   └── utils.py                # Text sanitization, phone normalization, dual CSV export (, and ;)
│
├── procurements/               # Tender & Procurement Portals
│   ├── __init__.py
│   ├── goszakup/               # Goszakup (goszakup.gov.kz)
│   │   ├── __init__.py
│   │   └── scraper.py          # Awards & Contract PDF operational phone parser (v1.2.0)
│   └── mp_kz/                  # MP.kz / Mitwork commercial electronic trading platform
│       ├── __init__.py
│       └── scraper.py          # Platform & company contacts extractor (v1.0.0)
│
├── website_scraping/           # Generic Company Website Crawler
│   ├── __init__.py
│   └── crawler.py              # Domain contact extractor (homepage, /contacts, /about) (v1.0.0)
│
├── telegram_scraping/          # Telegram Lead Scraper
│   ├── __init__.py
│   └── reader.py               # Channel & chat web preview parser (t.me/s/{channel}) (v1.0.0)
│
├── data/                       # Unified directory for generated lead datasets
│   ├── medical_suppliers_contract_phones_2026.csv
│   ├── medical_suppliers_contract_phones_2026_semicolon.csv
│   ├── mp_kz_leads.csv
│   └── mp_kz_leads_semicolon.csv
│
├── main.py                     # Unified CLI entrypoint (v1.0.0)
└── README.md
```

## Usage

### 1. MP.kz (Mitwork Procurement)
Scrapes commercial tenders, organizers, platform hotline (`+7 727 330-99-77`), and direct company profiles:
```bash
python3 main.py mp_kz --limit 30 --output data/mp_kz_leads.csv
```

### 2. Goszakup (State Procurements)
Scrapes awarded state tenders and extracts operational mobile numbers from signed contract PDFs:
```bash
python3 main.py goszakup --output data/medical_suppliers_contract_phones_2026.csv
```

### 3. Website Scraping
Scrapes phone numbers, WhatsApp links, and emails from any company website:
```bash
python3 main.py website https://mp.kz https://example.kz --output data/website_leads.csv
```

### 4. Telegram Scraping
Scrapes public Telegram channel previews for phone numbers and usernames (without API keys):
```bash
python3 main.py telegram kztenders tender_kz --output data/telegram_leads.csv
```

## CSV Formatting Guarantee
All scrapers automatically generate **two** sanitized formats to prevent column shifts:
1. `*.csv`: Comma-separated with all internal text/phone commas sanitized to ` / ` (strictly aligned columns).
2. `*_semicolon.csv`: Semicolon-separated for direct opening in Windows / CIS Excel.
