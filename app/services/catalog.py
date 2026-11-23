"""Shared product catalog definitions for Control Center and Ads Manager."""

from typing import Optional

VERSION = "2.9.1"

CATALOG = [
    {
        "id": "centrifuges",
        "name": "Зертханалық центрифугалар",
        "name_ru": "Лабораторные центрифуги",
        "category": "Медициналық және зертханалық жабдықтар",
        "price_range": "185 000 – 1 850 000 ₸",
        "models": ["DLAB DM0412", "Biosan LMC-3000", "Armed 80-2S", "Eppendorf 5424 R"],
        "specs": "15 000 айн/мин дейін, 1.5-50 мл пробиркалар, жасушалық және қан сарысуы талдауы, ресми кепілдік 12-24 ай.",
        "pdf_kz": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
        "pdf_ru": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
    },
    {
        "id": "tires",
        "name": "Автокөлік шиналары (Жеңіл және жүк көліктері)",
        "name_ru": "Автомобильные шины (Легковые и грузовые)",
        "category": "Автокөлік тауарлары және парктік жабдықтау",
        "price_range": "24 500 – 85 000 ₸",
        "models": ["Viatti Strada Asimmetrico", "Cordiant Comfort 2", "Triangle AdvanteX", "Cordiant Gravity"],
        "specs": "R14-R18 жазғы/қысқы шиналар, күшейтілген бүйірлік қабырғалар, дистрибьютордың тікелей қоймасынан көтерме баға.",
        "pdf_kz": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
        "pdf_ru": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
    },
    {
        "id": "scalpels",
        "name": "Хирургиялық скальпельдер мен бір реттік лезвиялар",
        "name_ru": "Хирургические скальпели и одноразовые лезвия",
        "category": "Медициналық шығын материалдары",
        "price_range": "45 – 180 ₸ / дана",
        "models": ["Лезвия №10, №11, №15, №20, №24", "Скальпель брюшистый/остроконечный"],
        "specs": "Көміртекті және тот баспайтын хирургиялық болат, жеке стерильді фольга қаптама, ҚР тіркеу куәлігі бар.",
        "pdf_kz": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
        "pdf_ru": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
    },
]

CATALOG_KEYS = {item["id"] for item in CATALOG}


def resolve_catalog_item(key_or_name: str) -> dict:
    """Resolve a catalog entry by id, Kazakh name, or Russian name, falling back to first entry."""
    needle = (key_or_name or "").strip()
    return next(
        (c for c in CATALOG if c["id"] == needle or c["name"] == needle or c["name_ru"] == needle),
        CATALOG[0],
    )


def catalog_service_label(product_key: str, lang: str) -> str:
    """Return the localized service name sold for a catalog product key."""
    item = resolve_catalog_item(product_key)
    return item["name"] if lang == "kz" else item["name_ru"]
