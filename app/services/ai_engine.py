"""Multi-provider AI engine supporting Gemini (with Search Grounding), OpenAI, DeepSeek, Anthropic, and local Ollama/vLLM."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import httpx

from app.core.config import Settings
from app.services.platform_store import PlatformStore

VERSION = "2.2.0"


class AIEngineError(ValueError):
    """Raised when an AI provider call fails or returns invalid structured JSON."""


def _extract_json_block(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
        if m:
            return json.loads(m.group(1))
        raise AIEngineError("ai_invalid_json_output") from None


class AIEngine:
    """Unified multi-provider LLM router for product research, audience analysis, and call script generation."""

    def __init__(self, store: PlatformStore, settings: Settings, transport: Any = None):
        self.store = store
        self.settings = settings
        self.transport = transport

    def resolve_provider(self, purpose: str = "all", provider_id: Optional[int] = None) -> dict:
        if provider_id is not None:
            p = self.store.get_ai_provider(provider_id, include_secrets=True)
            if not p:
                raise AIEngineError("ai_provider_not_found")
            return p

        providers = [p for p in self.store.list_ai_providers(include_secrets=True) if p.get("is_active")]
        for p in providers:
            if p.get("purpose") == purpose:
                return p
        for p in providers:
            if p.get("purpose") == "all":
                return p
        if providers:
            return providers[0]

        # Fallback to environment Gemini key if configured
        if self.settings.control_gemini_key:
            return {
                "id": 0,
                "name": "System Gemini (.env)",
                "provider": "gemini",
                "api_key": self.settings.control_gemini_key.get_secret_value(),
                "base_url": "",
                "model_name": self.settings.control_ai_model or "gemini-flash-latest",
                "purpose": "all",
                "is_active": True,
            }
        raise AIEngineError("no_active_ai_provider_configured")

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        purpose: str = "all",
        provider_id: Optional[int] = None,
        use_web_search: bool = False,
        expect_json: bool = True,
    ) -> dict:
        cfg = self.resolve_provider(purpose=purpose, provider_id=provider_id)
        provider = (cfg.get("provider") or "gemini").lower()
        api_key = (cfg.get("api_key") or "").strip()
        model = (cfg.get("model_name") or "").strip()
        base_url = (cfg.get("base_url") or "").strip().rstrip("/")

        t0 = time.monotonic()
        transport = self.transport or httpx.AsyncHTTPTransport(
            proxy=self.settings.control_ai_proxy or None,
            trust_env=False,
        )

        async with httpx.AsyncClient(transport=transport, timeout=75.0) as client:
            if provider == "gemini":
                url = base_url or f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                payload: dict[str, Any] = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
                }
                if use_web_search:
                    payload["tools"] = [{"google_search": {}}]
                elif expect_json:
                    payload["generationConfig"]["responseMimeType"] = "application/json"

                resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
                if resp.status_code != 200:
                    raise AIEngineError(f"gemini_http_{resp.status_code}: {resp.text[:200]}")
                body = resp.json()
                parts = body["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
                grounding = body["candidates"][0].get("groundingMetadata") or {}
                sources = [
                    {"title": c.get("web", {}).get("title", ""), "uri": c.get("web", {}).get("uri", "")}
                    for c in grounding.get("groundingChunks", [])
                    if c.get("web", {}).get("uri")
                ]

            elif provider in {"openai", "deepseek", "local_openai", "ollama", "vllm"}:
                if not base_url:
                    base_url = "https://api.deepseek.com/v1" if provider == "deepseek" else "https://api.openai.com/v1"
                url = f"{base_url}/chat/completions"
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                }
                if expect_json and provider in {"openai", "deepseek"}:
                    payload["response_format"] = {"type": "json_object"}
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    raise AIEngineError(f"{provider}_http_{resp.status_code}: {resp.text[:200]}")
                body = resp.json()
                text = body["choices"][0]["message"]["content"]
                sources = []

            elif provider == "anthropic":
                url = base_url or "https://api.anthropic.com/v1/messages"
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model,
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                    "temperature": 0.2,
                }
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    raise AIEngineError(f"anthropic_http_{resp.status_code}: {resp.text[:200]}")
                body = resp.json()
                text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
                sources = []
            else:
                raise AIEngineError(f"unsupported_provider:{provider}")

        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "provider_name": cfg.get("name"),
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "text": text,
            "data": _extract_json_block(text) if expect_json else None,
            "sources": sources,
        }

    async def test_provider(self, provider_id: int) -> dict:
        res = await self.complete(
            system_prompt="You are a connectivity probe. Reply with one short word: OK.",
            user_prompt="Reply OK.",
            provider_id=provider_id,
            expect_json=False,
        )
        return {
            "ok": True,
            "provider": res["provider"],
            "model": res["model"],
            "latency_ms": res["latency_ms"],
            "reply": (res["text"] or "").strip()[:80],
        }

    async def research_product_from_prompt(self, prompt: str, lang: str = "ru") -> dict:
        system = (
            "You are a B2B commercial product researcher for Kazakhstan and CIS markets. "
            "Given a user prompt describing a product or service, research real market data, technical specifications, "
            "popular models/variants, and realistic price ranges in KZT (₸). "
            "Return ONLY a valid JSON object with exact keys: "
            '{"name": "Kazakh product title", "name_ru": "Russian product title", "category": "Market category", '
            '"price_range": "Realistic KZT range e.g. 250 000 – 950 000 ₸", '
            '"models": ["Model 1", "Model 2", "Model 3"], '
            '"specs": "Concise real technical specifications, certifications, warranty (2-3 sentences)", '
            '"target_use_case": "Who uses this product and why (1-2 sentences)"}. '
            "Never output markdown fences or commentary outside JSON."
        )
        res = await self.complete(
            system_prompt=system,
            user_prompt=f"Product/Service prompt: {prompt.strip()}\nPreferred detail language: {lang}",
            purpose="web_research",
            use_web_search=True,
            expect_json=True,
        )
        data = res["data"]
        if not isinstance(data, dict) or not data.get("name"):
            raise AIEngineError("invalid_product_research_payload")
        data["sources"] = res.get("sources", [])
        data["model_used"] = f"{res['provider']}:{res['model']}"
        return data

    async def analyze_audiences_from_prompt(self, prompt: str, product: Optional[dict] = None) -> dict:
        product_ctx = ""
        if product:
            product_ctx = (
                f"\nSelected Product:\n"
                f"- Name (KZ): {product.get('name', '')}\n"
                f"- Name (RU): {product.get('name_ru', '')}\n"
                f"- Category: {product.get('category', '')}\n"
                f"- Price: {product.get('price_range', '')}\n"
                f"- Specs: {product.get('specs', '')}\n"
            )
        system = (
            "You are a B2B/B2G/B2C market segmentation strategist for Kazakhstan. "
            "Analyze the product or category prompt and propose 3 to 5 concrete, distinct target customer segments "
            "that actively buy this product or service in Kazakhstan. "
            "Return ONLY a valid JSON object: "
            '{"summary": "Short market overview", "segments": ['
            '{"name": "Segment name", "segment": "b2b|b2g|b2c", '
            '"description": "Why they buy and what pain point it solves", '
            '"decision_makers": ["Title 1", "Title 2"], '
            '"buying_signals": ["Signal 1", "Signal 2"], '
            '"regions": ["almaty", "astana", "shymkent"], '
            '"keywords": ["keyword1", "keyword2", "keyword3"], '
            '"estimated_volume": "e.g. 120-250 companies in KZ"}'
            "]}. "
            "Regions MUST only use values from: all, almaty, astana, shymkent. "
            "Segment MUST be one of: b2b, b2g, b2c."
        )
        res = await self.complete(
            system_prompt=system,
            user_prompt=f"{product_ctx}\nOperator targeting prompt: {prompt.strip() or 'Find the highest-converting B2B and B2G buyer segments in Kazakhstan.'}",
            purpose="audience_generation",
            use_web_search=False,
            expect_json=True,
        )
        data = res["data"]
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            raise AIEngineError("invalid_audience_analysis_payload")
        data["model_used"] = f"{res['provider']}:{res['model']}"
        return data


def build_call_script_preview(
    product: dict,
    company: dict,
    telephony: dict,
    lang: str = "kz",
    custom_greeting: str = "",
    custom_prompt: str = "",
    ceo: str = "",
    biz: str = "",
) -> dict:
    """Build the exact, isolated opening greeting and full AI system instruction preview for an operator."""
    seller = (company.get("company_name") or "CRM Consulting").strip()
    prod_title = product.get("name") if lang == "kz" else (product.get("name_ru") or product.get("name"))
    specs = product.get("specs") or ""
    price = product.get("price_range") or ""
    details_block = f"{prod_title}. Бағасы/Цена: {price}. {specs}".strip()

    if custom_greeting.strip():
        greeting = custom_greeting.strip()
    elif lang == "kz":
        ceo_part = f", {ceo.strip()}" if ceo.strip() else ""
        biz_part = f"{biz.strip()} компаниясынансыз ба? " if biz.strip() else ""
        greeting = (
            f"Қайырлы күн{ceo_part}! {biz_part}Менің есімім Айнұр, «{seller}» компаниясынан хабарласып тұрмын. "
            f"Біз тікелей жеткізу шарттарымен {prod_title} ұсынамыз. Бұл бағыт қазір сіздер үшін өзекті ме?"
        )
    else:
        ceo_part = f", {ceo.strip()}" if ceo.strip() else ""
        biz_part = f"компания {biz.strip()}? " if biz.strip() else ""
        greeting = (
            f"Добрый день{ceo_part}! {biz_part}Меня зовут Айнұр, компания «{seller}». "
            f"Звоню вам по поводу прямых поставок и условий по направлению: {prod_title}. "
            f"Подскажите, пожалуйста, актуальна ли для вас сейчас эта продукция или услуга?"
        )

    extra = (custom_prompt or "").strip()
    if lang == "kz":
        system_prompt = (
            f"РӨЛ: Сіз — «{seller}» компаниясының кәсіби B2B дауыстық менеджері Айнұрсыз.\n"
            f"ӨНІМ / ҚЫЗМЕТ: {prod_title}\n"
            f"МӘЛІМЕТ ЖӘНЕ БАҒА: {details_block}\n"
            f"ТІЛ ЕРЕЖЕСІ: Тек таза қазақ тілінде сөйлеңіз. Бөтен өнімдерді немесе CRM/WhatsApp қоңырауларын араластырмаңыз.\n"
            f"МАҚСАТ: Клиенттің қажеттілігін нақтылап, ресми коммерциялық ұсынысты WhatsApp-қа жіберуге келісім алу "
            f"немесе қызығушылық болса ішкі {telephony.get('transfer_extension', '055')} менеджерге қосу."
        )
    else:
        system_prompt = (
            f"РОЛЬ: Вы — профессиональный B2B голосовой менеджер Айнұр компании «{seller}».\n"
            f"ПРОДУКТ / УСЛУГА: {prod_title}\n"
            f"ДЕТАЛИ И ЦЕНА: {details_block}\n"
            f"ЯЗЫКОВОЕ ПРАВИЛО: Говорите строго на русском языке и ТОЛЬКО об этом продукте/услуге. Не упоминайте посторонние сервисы.\n"
            f"ЦЕЛЬ: Уточнить потребность, согласовать отправку КП на WhatsApp или перевести горячего клиента на менеджера (доб. {telephony.get('transfer_extension', '055')})."
        )
    if extra:
        system_prompt += f"\n\nАРНАЙЫ НҰСҚАУЛЫҚ / CUSTOM INSTRUCTIONS:\n{extra}"

    return {
        "seller_company": seller,
        "product_name": prod_title,
        "lang": lang,
        "voice_name": telephony.get("voice_name", "Kore"),
        "transfer_extension": telephony.get("transfer_extension", "055"),
        "greeting": greeting,
        "system_prompt": system_prompt,
        "details_sent_to_pbx": details_block,
    }
