"""
Lead Hunter - Commercial Offer (КП) Document Generator
Generates high-fidelity .docx and .pdf Commercial Offers from dynamic templates.
Uses python-docx-template (docxtpl) and headless LibreOffice.

Features:
- Dynamic table rows (1 to 1000+ items) with automatic downward expansion
- High-resolution inline product images (InlineImage)
- Full preservation of Word styles, company header, address, stamps and signatures
- 100% self-hosted, open-source, free with zero external API fees
"""

import os
import subprocess
from datetime import datetime
from typing import List, Dict, Any, Optional
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

VERSION = "1.5.4"

DEFAULT_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates",
    "commercial_offer_template.docx"
)

DEFAULT_TEMPLATE_PATH_KZ = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates",
    "commercial_offer_template_kz.docx"
)

DEFAULT_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output"
)

def format_price(amount: Any) -> str:
    """Format numeric price with thousands separator, e.g. 28400 -> '28 400'."""
    try:
        val = int(amount)
        return f"{val:,}".replace(",", " ")
    except (ValueError, TypeError):
        return str(amount)

def generate_commercial_offer(
    client_company: str,
    items: List[Dict[str, Any]],
    client_name: Optional[str] = None,
    doc_number: Optional[str] = None,
    doc_date: Optional[str] = None,
    intro_text: Optional[str] = None,
    template_path: Optional[str] = None,
    output_docx_path: Optional[str] = None,
    output_pdf_path: Optional[str] = None,
    convert_to_pdf: bool = True,
    lang: str = "ru"
) -> Dict[str, str]:
    """
    Generate Commercial Offer DOCX and PDF from template.

    :param client_company: Company name or recipient representation (e.g. 'ТОО «ТАЙЫРОВ»')
    :param items: List of dicts with keys:
                  'name' (str),
                  'description' (str),
                  'unit' (str, default 'шт' or 'дана'),
                  'price' (numeric or str),
                  'image_path' (str, optional)
    :param client_name: Optional contact person name
    :param doc_number: Outgoing document number (defaults to daily sequence)
    :param doc_date: Document date (defaults to current date DD.MM.YYYY)
    :param intro_text: Optional custom intro paragraph
    :param template_path: Path to .docx template (defaults to templates/commercial_offer_template.docx or _kz.docx)
    :param output_docx_path: Target path for docx output
    :param output_pdf_path: Target path for pdf output
    :param convert_to_pdf: Whether to convert generated docx to pdf using LibreOffice
    :param lang: Language code ('ru' or 'kz'/'kk')
    :return: Dict containing 'docx' and 'pdf' file paths
    """
    is_kz = lang in ["kz", "kk"]
    default_tpl = DEFAULT_TEMPLATE_PATH_KZ if is_kz else DEFAULT_TEMPLATE_PATH
    tpl_file = template_path or default_tpl
    if not os.path.exists(tpl_file):
        raise FileNotFoundError(f"Commercial offer template not found at: {tpl_file}")

    # Explicit application paths keep generated files in the existing private store.
    if not output_docx_path or not output_pdf_path:
        os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    today = datetime.now()
    doc_date = doc_date or today.strftime("%d.%m.%Y")
    doc_number = doc_number or f"{today.strftime('%d%m')}/{today.strftime('%y')}"

    full_company = client_company
    if client_name and client_name not in client_company:
        full_company = f"{client_company} ({client_name})"

    tpl = DocxTemplate(tpl_file)

    default_unit = "дана" if is_kz else "шт"

    # Process items and images
    processed_items = []
    for item in items:
        img_obj = None
        img_path = item.get("image_path")
        if img_path and os.path.exists(img_path):
            img_obj = InlineImage(tpl, img_path, width=Mm(30))

        processed_items.append({
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "unit": item.get("unit", default_unit),
            "price": format_price(item.get("price", "")),
            "image": img_obj if img_obj is not None else ""
        })

    context = {
        "client_company": full_company,
        "doc_number": doc_number,
        "doc_date": doc_date,
        "intro_text": intro_text,
        "items": processed_items
    }

    tpl.render(context)

    # Resolve output paths
    base_name = f"KP_{client_company.replace(' ', '_').replace('«', '').replace('»', '').replace('\"', '')}_{today.strftime('%Y%m%d_%H%M%S')}"
    target_docx = output_docx_path or os.path.join(DEFAULT_OUTPUT_DIR, f"{base_name}.docx")
    target_pdf = output_pdf_path or os.path.join(DEFAULT_OUTPUT_DIR, f"{base_name}.pdf")

    tpl.save(target_docx)

    result = {"docx": target_docx, "pdf": None}

    if convert_to_pdf:
        out_dir = os.path.dirname(target_pdf)
        cmd = [
            "libreoffice",
            "--headless",
            "--convert-to", "pdf",
            target_docx,
            "--outdir", out_dir
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)
        if proc.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {proc.stderr}")

        # LibreOffice names output file by base docx name + .pdf
        expected_lo_pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(target_docx))[0] + ".pdf")
        if os.path.exists(expected_lo_pdf) and expected_lo_pdf != target_pdf:
            os.replace(expected_lo_pdf, target_pdf)

        result["pdf"] = target_pdf

    return result
