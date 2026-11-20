# Lead Hunter & Universal Sales Intelligence Platform

**Lead Hunter** is an autonomous B2B/B2G sales intelligence, outbound calling, and document generation platform for Kazakhstan and international markets.

* **Production Control Center:** [https://leadhunter.crmconsulting.kz/](https://leadhunter.crmconsulting.kz/)
* **Version:** `1.6.0`

---

## Architecture Overview

```
lead_hunter/
├── app/                        # FastAPI Web Platform & Universal Control Center
│   ├── api/                    # REST APIs (Control Center, Outreach, Deals, Analytics)
│   ├── core/                   # Application settings, secrets, and environment config
│   ├── models/                 # Pydantic schemas (Products, ICP Segments, Candidates)
│   ├── services/               # Workflow orchestration, AI intelligence, and discovery
│   └── web/                    # Modern Control Center (HTML, CSS, JavaScript)
│
├── calling/                    # Bitrix PBX & Asterisk AI Voice Calling Bridge
│   └── pbx_caller.py           # Outbound caller for Gemini Live 2.0 Voice SDR
│
├── documents/                  # Automated Document & Commercial Offer Engine
│   └── offer_generator.py      # Dynamic DOCX/PDF generator with authentic product photos
│
├── integrations/               # External Channels & Messengers
│   ├── whatsapp_sender.py      # Meta WhatsApp Cloud API with DOCUMENT header templates
│   └── bitrix24.py             # Bitrix24 REST API client
│
├── telegram_scraping/          # Telegram Community Outreach & Scraping
│   ├── outreach_engine.py      # Personalized inquiry messenger with anti-spam jitter
│   └── reader.py               # Channel & chat web preview parser
│
├── procurements/               # Tender & Procurement Portals
│   ├── goszakup/               # Goszakup (goszakup.gov.kz) awarded contract scraper
│   └── mp_kz/                  # MP.kz / Mitwork commercial electronic trading platform
│
├── templates/                  # Official Word (.docx) letterhead templates
│   ├── commercial_offer_template.docx      # Russian Commercial Offer template
│   └── commercial_offer_template_kz.docx   # Kazakh Commercial Offer template
│
└── output/                     # Generated PDFs & outgoing dispatch packages
```

---

## Core Capabilities

1. **Executive Control Center (`/platform`)**:
   - Universal product brief and value proposition manager.
   - Structured ICP audience generation via Gemini Flash.
   - Live outbound AI voice calling launcher (Asterisk PBX).
   - WhatsApp 2-page PDF commercial offer dispatch via approved Meta templates.
2. **AI Voice Calling (Asterisk + Gemini Live 2.0)**:
   - Real B2B sales manager techniques in **Kazakh** and **Russian**.
   - Contextual openings and objection handlers (*«Жеткізушіміз бар»*, *«Поштаға жіберіңіз»*, *«Қанша тұрады?»*).
   - Strict 2-way phone selection (current number vs. dictated number) and automatic hangup upon agreement.
3. **Automated Commercial Offer Generation**:
   - High-fidelity 2-page PDF generation from `.docx` templates using `docxtpl` and headless LibreOffice.
   - Authentic scraped manufacturer photos, exact 180mm table borders, and official director signature.
4. **Meta WhatsApp Cloud API Delivery**:
   - Approved template `commercial_offer_pdf_doc` (WABA `891036053865144`) with native `DOCUMENT` header.
   - Bypasses the 24-hour delivery barrier to deliver official PDF proposals directly to client chats.
