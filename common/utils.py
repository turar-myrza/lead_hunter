"""
Common Utilities for Lead Hunter
Handles phone normalization, string sanitization, and dual CSV export (, and ;).
"""

import csv
import os
import re

VERSION = "1.2.1"


def clean_cell(val) -> str:
    """Sanitizes text to prevent CSV column shifts and malformed characters."""
    if not val:
        return ""
    v = str(val).replace("\r", " ").replace("\n", " ")
    v = v.replace('"', "").replace("'", "")
    # Convert internal commas to ' / ' to prevent CSV column shifting
    v = re.sub(r"\s*,\s*", " / ", v)
    # Strip semicolons
    v = v.replace(";", " | ")
    v = re.sub(r"\s*/\s*$", "", v)
    v = re.sub(r"^\s*/\s*", "", v)
    return re.sub(r"\s+", " ", v).strip()


def normalize_phone(raw_phone: str) -> str:
    """Standardizes Kazakhstan / international phone numbers."""
    if not raw_phone:
        return ""
    # Remove excessive punctuation
    p = re.sub(r"[^\d+]", "", raw_phone)
    if p.startswith("8") and len(p) == 11:
        p = "+7" + p[1:]
    return p


def save_csv_dual(output_csv_path: str, fieldnames: list, rows: list):
    """
    Saves leads in two formats:
    1. Comma-separated (sanitized, zero internal commas, never shifts)
    2. Semicolon-separated (standard double-click format for Excel in CIS / Windows)
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_csv_path)), exist_ok=True)

    cleaned_rows = []
    for r in rows:
        cleaned_rows.append({k: clean_cell(v) for k, v in r.items()})

    # 1. Comma delimiter
    with open(output_csv_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=",")
        writer.writeheader()
        writer.writerows(cleaned_rows)

    # 2. Semicolon delimiter
    semi_path = output_csv_path.replace(".csv", "_semicolon.csv")
    with open(semi_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(cleaned_rows)

    return output_csv_path, semi_path
