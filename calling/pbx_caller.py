"""
Bitrix PBX Calling Bridge for Lead Hunter
Connects Lead Hunter directly to Bitrix PBX AI Voice Gateway without copying
or duplicating the Asterisk / Gemini Live telephony infrastructure.

Supports:
- Local execution if running directly on telephony VPS (/home/debian/bitrix_pbx)
  or Windows dev mount (/mnt/d/projects/bitrix_pbx).
- Authenticated SSH execution from whatflow (195.49.210.252) to ai_telephony (185.22.64.70).
- Strict Asterisk AMI / SIP CRLF injection sanitization.
"""

import os
import re
import sys
import subprocess
import shlex
from typing import List, Optional, Dict, Any

VERSION = "2.7.0"

DEFAULT_TELEPHONY_HOST = os.getenv("TELEPHONY_HOST", "185.22.64.70")
DEFAULT_SSH_USER = os.getenv("TELEPHONY_SSH_USER", "debian")
DEFAULT_REMOTE_SCRIPT = os.getenv("TELEPHONY_CALLER_SCRIPT", "/home/debian/bitrix_pbx/outbound_caller.py")
LOCAL_DEV_SCRIPT = "/mnt/d/projects/bitrix_pbx/outbound_caller.py"


def sanitize_ami_value(value: str, max_len: int = 1200) -> str:
    """Strip CR, LF, NUL, and control characters to prevent Asterisk AMI protocol injection."""
    if not value:
        return ""
    # Replace CR, LF, NUL, tabs, and other C0 control chars with a single space
    cleaned = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", str(value))
    return re.sub(r"\s{2,}", " ", cleaned).strip()[:max_len]


def validate_sip_phone(phone: str) -> str:
    """Validate phone number contains only optional leading '+' and 7-15 digits (zero CRLF/SIP injection)."""
    raw = (phone or "").strip()
    if any(ch in raw for ch in ("\r", "\n", "\x00", ";", "@", "/", ":")):
        raise ValueError("invalid_sip_phone_characters")
    if not re.fullmatch(r"\+?[0-9]{7,15}", raw):
        raise ValueError("invalid_sip_phone_format")
    return raw


def validate_transfer_extension(ext: str) -> str:
    """Validate Asterisk transfer extension contains only safe alphanumeric/underscore/hyphen characters."""
    raw = (ext or "").strip()
    if not raw:
        return "055"
    if not re.fullmatch(r"[0-9A-Za-z_\-]{1,32}", raw):
        raise ValueError("invalid_transfer_extension")
    return raw


def call_lead(
    phone: str,
    products: List[str],
    details: str = "",
    ceo: str = "",
    biz: str = "",
    lang: str = "kz",
    greeting: str = "",
    seller: str = "",
    voice: str = "",
    custom_prompt: str = "",
    transfer_target: str = "",
    host: Optional[str] = None,
    user: Optional[str] = None,
    remote_script: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Triggers an outbound AI call to a target lead via Bitrix PBX Asterisk gateway.
    All SIP and AMI parameters are strictly sanitized against CRLF injection.
    """
    safe_phone = validate_sip_phone(phone)
    safe_lang = "kz" if (lang or "kz").strip().lower() == "kz" else "ru"
    safe_products = [sanitize_ami_value(p, max_len=200) for p in products if sanitize_ami_value(p)]
    safe_details = sanitize_ami_value(details, max_len=1500)
    safe_ceo = sanitize_ami_value(ceo, max_len=120)
    safe_biz = sanitize_ami_value(biz, max_len=160)
    safe_greeting = sanitize_ami_value(greeting, max_len=1000)
    safe_seller = sanitize_ami_value(seller, max_len=200)
    safe_voice = re.sub(r"[^A-Za-z0-9_\-]", "", (voice or "Kore").strip())[:40] or "Kore"
    safe_prompt = sanitize_ami_value(custom_prompt, max_len=3000)
    safe_transfer = validate_transfer_extension(transfer_target or "055")

    is_local = os.path.exists(DEFAULT_REMOTE_SCRIPT)
    is_dev = os.path.exists(LOCAL_DEV_SCRIPT)

    args = [safe_phone, "--lang", safe_lang]
    for p in safe_products:
        args.extend(["--product", p])
    if safe_details:
        args.extend(["--details", safe_details])
    if safe_ceo:
        args.extend(["--ceo", safe_ceo])
    if safe_biz:
        args.extend(["--biz", safe_biz])
    if safe_greeting:
        args.extend(["--greeting", safe_greeting])
    if safe_seller:
        args.extend(["--seller", safe_seller])
    if safe_voice:
        args.extend(["--voice", safe_voice])
    if safe_prompt:
        args.extend(["--prompt", safe_prompt])
    if safe_transfer:
        args.extend(["--transfer", safe_transfer])

    if is_local:
        cmd = [sys.executable, DEFAULT_REMOTE_SCRIPT] + args
        print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Local AI call to {safe_phone}...")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {
            "success": res.returncode == 0,
            "returncode": res.returncode,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "mode": "local",
        }
    elif is_dev:
        cmd = [sys.executable, LOCAL_DEV_SCRIPT] + args
        print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Dev AI call to {safe_phone}...")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {
            "success": res.returncode == 0,
            "returncode": res.returncode,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "mode": "dev",
        }
    else:
        target_host = host or DEFAULT_TELEPHONY_HOST
        target_user = user or DEFAULT_SSH_USER
        script_path = remote_script or DEFAULT_REMOTE_SCRIPT

        remote_cmd_parts = ["python3", script_path] + [shlex.quote(a) for a in args]
        remote_cmd_str = " ".join(remote_cmd_parts)

        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
        ]
        key_path = "/var/lib/lead-hunter/.ssh/id_ed25519"
        if os.path.exists(key_path):
            ssh_cmd.extend(["-i", key_path])
        ssh_cmd.extend([
            f"{target_user}@{target_host}",
            remote_cmd_str,
        ])

        print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Remote SSH AI call via {target_host} to {safe_phone}...")
        res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=120)
        return {
            "success": res.returncode == 0,
            "returncode": res.returncode,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "mode": "ssh_remote",
            "host": target_host,
        }


def call_group_by_products(
    group_mapping: Dict[str, List[str]],
    product_details_map: Optional[Dict[str, str]] = None,
    lang: str = "kz",
):
    details_map = product_details_map or {}
    results = {}
    for phone, products in group_mapping.items():
        combined_details = " \n".join([details_map.get(p, "") for p in products if p in details_map])
        result = call_lead(
            phone=phone,
            products=products,
            details=combined_details,
            lang=lang,
        )
        results[phone] = result
    return results
