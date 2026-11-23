"""Kazakhstan multi-source discovery & enrichment adapters."""

from app.services.discovery.kz_multi_source import (
    VERSION,
    KZ_SOURCES_META,
    KazakhstanMultiSourceHarvester,
    normalize_company_key,
    extract_valid_kz_phones,
)

__all__ = [
    "VERSION",
    "KZ_SOURCES_META",
    "KazakhstanMultiSourceHarvester",
    "normalize_company_key",
    "extract_valid_kz_phones",
]
