"""
Bitrix PBX Calling Bridge for Lead Hunter
Connects Lead Hunter directly to Bitrix PBX AI Voice Gateway without copying
or duplicating the Asterisk / Gemini Live telephony infrastructure.

Allows mapping:
- Group of numbers A -> Product A (e.g. Disposable surgery scalpel blades)
- Group of numbers B -> Products A & B
- Directly dials target leads via Asterisk PBX with tailored AI prompts.
"""

import os
import sys
import subprocess
from typing import List, Optional, Dict, Any

VERSION = "1.5.0"

PBX_DIR = "/mnt/d/projects/bitrix_pbx"
OUTBOUND_CALLER_SCRIPT = os.path.join(PBX_DIR, "outbound_caller.py")


def call_lead(
    phone: str,
    products: List[str],
    details: str = "",
    ceo: str = "",
    biz: str = "",
    lang: str = "ru",
    greeting: str = "",
) -> bool:
    """
    Triggers an outbound AI call to a target lead via Bitrix PBX.
    Uses the tailored product pitch and custom opening greeting.
    """
    if not os.path.exists(OUTBOUND_CALLER_SCRIPT):
        raise FileNotFoundError(f"Bitrix PBX caller not found at {OUTBOUND_CALLER_SCRIPT}")

    cmd = [
        sys.executable,
        OUTBOUND_CALLER_SCRIPT,
        phone,
        "--lang",
        lang,
    ]
    for p in products:
        cmd.extend(["--product", p])
    if details:
        cmd.extend(["--details", details])
    if ceo:
        cmd.extend(["--ceo", ceo])
    if biz:
        cmd.extend(["--biz", biz])
    if greeting:
        cmd.extend(["--greeting", greeting])

    print(f"\n[Lead Hunter PBX Bridge v{VERSION}] Initiating AI call to {phone}...")
    print(f"Products: {', '.join(products)}")
    res = subprocess.run(cmd)
    return res.returncode == 0


def call_group_by_products(
    group_mapping: Dict[str, List[str]],
    product_details_map: Optional[Dict[str, str]] = None,
    lang: str = "ru",
):
    """
    Dials a group of numbers, where each number is mapped to its target products.
    Example:
        group_mapping = {
            "+77017211156": ["Одноразовые лезвия для хирургического скальпеля"],
            "+77071010122": ["Одноразовые лезвия для хирургического скальпеля", "Стерильные перчатки"],
        }
    """
    details_map = product_details_map or {}
    results = {}
    for phone, products in group_mapping.items():
        combined_details = " \n".join([details_map.get(p, "") for p in products if p in details_map])
        success = call_lead(
            phone=phone,
            products=products,
            details=combined_details,
            lang=lang,
        )
        results[phone] = success
    return results
