"""
Bitrix PBX Calling Bridge for Lead Hunter
Connects Lead Hunter directly to Bitrix PBX AI Voice Gateway without copying
or duplicating the Asterisk / Gemini Live telephony infrastructure.

Supports:
- Local execution if running directly on telephony VPS (/home/debian/bitrix_pbx)
  or Windows dev mount (/mnt/d/projects/bitrix_pbx).
- Authenticated SSH execution from whatflow (195.49.210.252) to ai_telephony (185.22.64.70).
"""

import os
import sys
import subprocess
import shlex
from typing import List, Optional, Dict, Any

VERSION = "2.0.0"

DEFAULT_TELEPHONY_HOST = os.getenv("TELEPHONY_HOST", "185.22.64.70")
DEFAULT_SSH_USER = os.getenv("TELEPHONY_SSH_USER", "debian")
DEFAULT_REMOTE_SCRIPT = os.getenv("TELEPHONY_CALLER_SCRIPT", "/home/debian/bitrix_pbx/outbound_caller.py")
LOCAL_DEV_SCRIPT = "/mnt/d/projects/bitrix_pbx/outbound_caller.py"


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
    """
    is_local = os.path.exists(DEFAULT_REMOTE_SCRIPT)
    is_dev = os.path.exists(LOCAL_DEV_SCRIPT)

    args = [phone, "--lang", lang]
    for p in products:
        args.extend(["--product", p])
    if details:
        args.extend(["--details", details])
    if ceo:
        args.extend(["--ceo", ceo])
    if biz:
        args.extend(["--biz", biz])
    if greeting:
        args.extend(["--greeting", greeting])
    if seller:
        args.extend(["--seller", seller])
    if voice:
        args.extend(["--voice", voice])
    if custom_prompt:
        args.extend(["--prompt", custom_prompt])
    if transfer_target:
        args.extend(["--transfer", transfer_target])

    if is_local:
        cmd = [sys.executable, DEFAULT_REMOTE_SCRIPT] + args
        print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Local AI call to {phone}...")
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
        print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Dev AI call to {phone}...")
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

        print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Remote SSH AI call via {target_host} to {phone}...")
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
