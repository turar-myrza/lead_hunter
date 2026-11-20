"""
Lead Hunter - WhatsApp Cloud API Integration
Provides autonomous document, media, and text dispatch via Meta WhatsApp Business Cloud API.
"""

import os
import requests
from typing import Dict, Any, Optional

VERSION = "1.5.0"

DEFAULT_ACCESS_TOKEN = os.getenv(
    "META_ACCESS_TOKEN",
    "EAAVsyfgZCWoEBSfFqHgE172ym5HjlRpbpnZBrackxpgHpY1Cu1jpplxmOTx8CZCCn9gsnowQX6OBaNp9kAucaXT8zNsaygiRv8xzSamaIcAtF0REHDi83EhdzLFphZCtxfNvfATthSWpOMjQ8fqMXTsSX6CZBeeErwezQ9azKsO51s3A0bpyWnz5F6OXjXqSATQZDZD"
)

DEFAULT_PHONE_NUMBER_ID = os.getenv("META_DEFAULT_PHONE_ID", "1344487605411939")
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

def clean_phone_number(phone: str) -> str:
    """Normalize phone to E.164 digits without leading '+'."""
    return "".join(filter(str.isdigit, phone))

def upload_whatsapp_media(
    file_path: str,
    mime_type: str = "application/pdf",
    phone_number_id: Optional[str] = None,
    access_token: Optional[str] = None
) -> str:
    """
    Upload local media file to Meta WhatsApp Business Cloud API.
    Returns media_id.
    """
    pid = phone_number_id or DEFAULT_PHONE_NUMBER_ID
    token = access_token or DEFAULT_ACCESS_TOKEN
    url = f"{GRAPH_API_BASE}/{pid}/media"

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Media file not found: {file_path}")

    filename = os.path.basename(file_path)
    headers = {"Authorization": f"Bearer {token}"}
    data = {
        "messaging_product": "whatsapp",
        "type": mime_type
    }

    with open(file_path, "rb") as f:
        files = {"file": (filename, f, mime_type)}
        response = requests.post(url, headers=headers, files=files, data=data, timeout=40)

    res_json = response.json()
    if response.status_code != 200 or "id" not in res_json:
        raise RuntimeError(f"WhatsApp media upload failed ({response.status_code}): {res_json}")

    return res_json["id"]

def send_whatsapp_document(
    to_phone: str,
    pdf_path: str,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    access_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Upload and send a PDF document to recipient via WhatsApp Business Cloud API.
    """
    pid = phone_number_id or DEFAULT_PHONE_NUMBER_ID
    token = access_token or DEFAULT_ACCESS_TOKEN
    norm_phone = clean_phone_number(to_phone)
    display_filename = filename or os.path.basename(pdf_path)

    # 1. Upload file
    media_id = upload_whatsapp_media(
        file_path=pdf_path,
        mime_type="application/pdf",
        phone_number_id=pid,
        access_token=token
    )

    # 2. Dispatch message
    url = f"{GRAPH_API_BASE}/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    doc_payload: Dict[str, Any] = {
        "id": media_id,
        "filename": display_filename
    }
    if caption:
        doc_payload["caption"] = caption

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": norm_phone,
        "type": "document",
        "document": doc_payload
    }

    response = requests.post(url, headers=headers, json=payload, timeout=25)
    res_json = response.json()
    if response.status_code != 200 or "messages" not in res_json:
        raise RuntimeError(f"WhatsApp message dispatch failed ({response.status_code}): {res_json}")

    return res_json
