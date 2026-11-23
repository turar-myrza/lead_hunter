"""Multi-provider AI engine supporting Gemini (with Search Grounding), OpenAI, DeepSeek, Anthropic, and local Ollama/vLLM with LiteLLM-compatible tier routing and automatic rate-limit fallbacks."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings
from app.services.platform_store import PlatformStore
from app.services.web_scraper import SSRFBlockedError, fetch_url_markdown

VERSION = "2.9.0"
logger = logging.getLogger("lead_hunter.ai_engine")


class ProductResearchSchema(BaseModel):
    """Strict Pydantic v2 schema for structured product extraction."""

    name_kz: str = Field(min_length=2, max_length=200)
    name_ru: str = Field(min_length=2, max_length=200)
    category: str = Field(default="B2B Equipment", max_length=200)
    price_range_kzt: str = Field(default="", max_length=120)
    models: list[str] = Field(default_factory=list, max_length=15)
    specs: str = Field(default="", max_length=2000)
    certifications: str = Field(default="", max_length=500)
    target_use_case: str = Field(default="", max_length=1000)


class AIEngineError(ValueError):
    """Raised when an AI provider call fails or returns invalid structured JSON."""


TIER_ALIASES = {
    "research_tier": "web_research",
    "web_research": "web_research",
    "reasoning_tier": "audience_generation",
    "audience_generation": "audience_generation",
    "test_tier": "test_tier",
    "testing": "test_tier",
    "voice_scripting": "voice_scripting",
    "all": "all",
}


def calculate_cost_usd(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate estimated API cost in USD based on model pricing per million tokens."""
    m = (model_name or "").lower()
    p = max(0, prompt_tokens) / 1_000_000.0
    c = max(0, completion_tokens) / 1_000_000.0

    if any(k in m for k in ("ollama", "vllm", "local", "qwen2.5", "llama3")):
        return 0.0
    if "flash-lite" in m or "2.5-flash-lite" in m:
        return p * 0.0375 + c * 0.15
    if "pro" in m:
        return p * 1.25 + c * 5.0
    if "flash" in m:
        return p * 0.075 + c * 0.30
    if "deepseek" in m:
        return p * 0.14 + c * 0.28
    if "gpt-4o-mini" in m:
        return p * 0.15 + c * 0.60
    if "gpt-4o" in m:
        return p * 2.50 + c * 10.0
    if "claude-3-haiku" in m:
        return p * 0.25 + c * 1.25
    if "claude" in m:
        return p * 3.00 + c * 15.0

    # Default conservative commercial rate
    return p * 0.10 + c * 0.40


def sanitize_secrets(text: str, secrets: list[str]) -> str:
    """Mask any plain-text API keys or tokens in strings/errors."""
    cleaned = text or ""
    for s in secrets:
        if s and len(s) > 4:
            cleaned = cleaned.replace(s, s[:2] + "••••" + s[-2:])
    # Also mask standard bearer or key patterns
    cleaned = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", r"\1••••", cleaned)
    cleaned = re.sub(r"(x-goog-api-key['\":\s=]+)[A-Za-z0-9_\-]{8,}", r"\1••••", cleaned)
    return cleaned


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
    """Unified multi-provider LLM router supporting model tiers, token spend tracking, and automatic rate-limit fallbacks."""

    def __init__(self, store: PlatformStore, settings: Settings, transport: Any = None):
        self.store = store
        self.settings = settings
        self.transport = transport

    def resolve_candidate_providers(self, purpose: str = "all", provider_id: Optional[int] = None) -> list[dict]:
        """Resolve ordered list of active providers with tier matching and fallback routing."""
        canonical_purpose = TIER_ALIASES.get(purpose, purpose)

        if provider_id is not None:
            p = self.store.get_ai_provider(provider_id, include_secrets=True)
            if not p:
                raise AIEngineError("ai_provider_not_found")
            # If explicit ID requested, put it first and remaining active providers as fallbacks
            others = [
                o for o in self.store.list_ai_providers(include_secrets=True)
                if o.get("is_active") and o.get("id") != provider_id
            ]
            return [p] + others

        active = [p for p in self.store.list_ai_providers(include_secrets=True) if p.get("is_active")]
        tier_matched = []
        general = []
        remaining = []

        for p in active:
            p_purp = TIER_ALIASES.get(p.get("purpose", "all"), p.get("purpose", "all"))
            if p_purp == canonical_purpose:
                tier_matched.append(p)
            elif p_purp == "all":
                general.append(p)
            else:
                remaining.append(p)

        candidates = tier_matched + general + remaining

        # Fallback to system Gemini from .env if configured
        if self.settings.control_gemini_key:
            candidates.append({
                "id": 0,
                "name": "System Gemini (.env)",
                "provider": "gemini",
                "api_key": self.settings.control_gemini_key.get_secret_value(),
                "base_url": "",
                "model_name": self.settings.control_ai_model or "gemini-flash-latest",
                "purpose": "all",
                "is_active": True,
            })

        return candidates

    def resolve_provider(self, purpose: str = "all", provider_id: Optional[int] = None) -> dict:
        candidates = self.resolve_candidate_providers(purpose=purpose, provider_id=provider_id)
        if not candidates:
            raise AIEngineError("no_active_ai_provider_configured")
        return candidates[0]

    async def _execute_single_provider(
        self,
        client: httpx.AsyncClient,
        cfg: dict,
        system_prompt: str,
        user_prompt: str,
        use_web_search: bool,
        expect_json: bool,
    ) -> dict:
        provider = (cfg.get("provider") or "gemini").lower()
        api_key = (cfg.get("api_key") or "").strip()
        model = (cfg.get("model_name") or "").strip()
        base_url = (cfg.get("base_url") or "").strip().rstrip("/")

        prompt_tokens = 0
        completion_tokens = 0
        sources = []

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
                raise AIEngineError(f"gemini_http_{resp.status_code}: {resp.text[:180]}")

            body = resp.json()
            candidates = body.get("candidates") or []
            if not candidates or not candidates[0].get("content"):
                raise AIEngineError("gemini_empty_response")

            parts = candidates[0]["content"].get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            grounding = candidates[0].get("groundingMetadata") or {}
            sources = [
                {"title": c.get("web", {}).get("title", ""), "uri": c.get("web", {}).get("uri", "")}
                for c in grounding.get("groundingChunks", [])
                if c.get("web", {}).get("uri")
            ]
            usage = body.get("usageMetadata") or {}
            prompt_tokens = usage.get("promptTokenCount", 0)
            completion_tokens = usage.get("candidatesTokenCount", 0)

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
                raise AIEngineError(f"{provider}_http_{resp.status_code}: {resp.text[:180]}")

            body = resp.json()
            choices = body.get("choices") or []
            if not choices or not choices[0].get("message"):
                raise AIEngineError(f"{provider}_empty_response")

            text = choices[0]["message"].get("content", "")
            usage = body.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

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
                raise AIEngineError(f"anthropic_http_{resp.status_code}: {resp.text[:180]}")

            body = resp.json()
            text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
            usage = body.get("usage") or {}
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)

        else:
            raise AIEngineError(f"unsupported_provider:{provider}")

        # Fallback token estimation if provider did not return usageMetadata
        if not prompt_tokens:
            prompt_tokens = max(1, (len(system_prompt) + len(user_prompt)) // 4)
        if not completion_tokens:
            completion_tokens = max(1, len(text) // 4)

        return {
            "text": text,
            "sources": sources,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        purpose: str = "all",
        provider_id: Optional[int] = None,
        use_web_search: bool = False,
        expect_json: bool = True,
    ) -> dict:
        candidates = self.resolve_candidate_providers(purpose=purpose, provider_id=provider_id)
        if not candidates:
            raise AIEngineError("no_active_ai_provider_configured")

        transport = self.transport or httpx.AsyncHTTPTransport(
            proxy=self.settings.control_ai_proxy or None,
            trust_env=False,
        )

        known_secrets = [c.get("api_key", "") for c in candidates if c.get("api_key")]
        attempt_errors = []

        async with httpx.AsyncClient(transport=transport, timeout=75.0) as client:
            for cfg in candidates:
                t0 = time.monotonic()
                provider = (cfg.get("provider") or "gemini").lower()
                model = (cfg.get("model_name") or "").strip()
                p_name = cfg.get("name") or provider

                try:
                    res = await self._execute_single_provider(
                        client=client,
                        cfg=cfg,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        use_web_search=use_web_search,
                        expect_json=expect_json,
                    )
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    cost_usd = calculate_cost_usd(model, res["prompt_tokens"], res["completion_tokens"])

                    # Durable usage recording
                    try:
                        self.store.record_ai_usage(
                            provider_id=cfg.get("id", 0),
                            provider_name=p_name,
                            model_name=model,
                            tier=purpose,
                            purpose=purpose,
                            prompt_tokens=res["prompt_tokens"],
                            completion_tokens=res["completion_tokens"],
                            estimated_cost_usd=cost_usd,
                            latency_ms=latency_ms,
                            status="success",
                        )
                    except Exception as rec_err:
                        logger.warning("Failed to record AI usage: %s", rec_err)

                    return {
                        "provider_name": p_name,
                        "provider": provider,
                        "model": model,
                        "latency_ms": latency_ms,
                        "prompt_tokens": res["prompt_tokens"],
                        "completion_tokens": res["completion_tokens"],
                        "estimated_cost_usd": cost_usd,
                        "text": res["text"],
                        "data": _extract_json_block(res["text"]) if expect_json else None,
                        "sources": res["sources"],
                    }

                except Exception as err:
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    err_msg = sanitize_secrets(str(err), known_secrets)
                    attempt_errors.append(f"{p_name}({model}): {err_msg}")
                    logger.warning("AI provider failed, attempting fallback: %s", err_msg)
                    try:
                        self.store.record_ai_usage(
                            provider_id=cfg.get("id", 0),
                            provider_name=p_name,
                            model_name=model,
                            tier=purpose,
                            purpose=purpose,
                            prompt_tokens=0,
                            completion_tokens=0,
                            estimated_cost_usd=0.0,
                            latency_ms=latency_ms,
                            status=f"error: {err_msg[:60]}",
                        )
                    except Exception:
                        pass
                    # Continue loop to next fallback provider
                    continue

        joined = " | ".join(attempt_errors)
        raise AIEngineError(f"all_ai_providers_exhausted: {joined}")

    async def test_provider(self, provider_id: int) -> dict:
        """Health-check ping testing latency, token counts, and cost."""
        res = await self.complete(
            system_prompt="You are a lightweight connectivity probe. Reply with one short word: OK.",
            user_prompt="Reply OK.",
            provider_id=provider_id,
            purpose="test_tier",
            expect_json=False,
        )
        return {
            "ok": True,
            "provider_name": res["provider_name"],
            "provider": res["provider"],
            "model": res["model"],
            "latency_ms": res["latency_ms"],
            "prompt_tokens": res["prompt_tokens"],
            "completion_tokens": res["completion_tokens"],
            "cost_usd": res["estimated_cost_usd"],
            "reply": (res["text"] or "").strip()[:80],
        }

    async def research_product_from_prompt(self, prompt: str, lang: str = "ru", url: str = "") -> dict:
        scraped_context = ""
        scraped_url = ""
        target_url = (url or "").strip()
        if not target_url:
            m = re.search(r"https?://[^\s<>\"']+", prompt or "")
            if m:
                target_url = m.group(0)

        if target_url:
            try:
                scraped = await fetch_url_markdown(
                    target_url,
                    transport=self.transport,
                    resolve_dns=self.transport is None,
                )
                scraped_url = scraped.get("url", target_url)
                scraped_context = (
                    f"\n\n--- SCRAPED PRODUCT PAGE MARKDOWN ({scraped_url}) ---\n"
                    f"{scraped.get('markdown', '')[:8000]}\n--- END SCRAPED PAGE ---"
                )
            except SSRFBlockedError:
                raise
            except Exception as scrape_err:
                logger.warning("Optional URL scrape skipped (%s): %s", target_url, scrape_err)

        system = (
            "You are a B2B commercial product researcher for Kazakhstan and CIS markets. "
            "Given a user prompt (and optional scraped page markdown), extract real market data, technical specifications, "
            "certifications (CE, ISO, GOST, EAEU), popular models/variants, and realistic price ranges in KZT (₸). "
            "Return ONLY a valid JSON object matching this exact Pydantic schema: "
            '{"name_kz": "Kazakh product title", "name_ru": "Russian product title", "category": "Market category", '
            '"price_range_kzt": "Realistic KZT range e.g. 250 000 – 950 000 ₸", '
            '"models": ["Model 1", "Model 2", "Model 3"], '
            '"specs": "Concise real technical specifications and warranty (2-3 sentences)", '
            '"certifications": "CE, ISO 13485, GOST, or warranty/compliance standards", '
            '"target_use_case": "Who uses this product and why (1-2 sentences)"}. '
            "Never output markdown fences or commentary outside JSON."
        )
        res = await self.complete(
            system_prompt=system,
            user_prompt=f"Product/Service prompt: {prompt.strip()}\nPreferred detail language: {lang}{scraped_context}",
            purpose="web_research",
            use_web_search=not bool(scraped_context),
            expect_json=True,
        )
        raw = res["data"]
        if not isinstance(raw, dict):
            raise AIEngineError("invalid_product_research_payload")

        # Normalize aliases so both legacy and strict Pydantic schema keys validate cleanly
        normalized = {
            "name_kz": raw.get("name_kz") or raw.get("name") or "",
            "name_ru": raw.get("name_ru") or raw.get("name_kz") or raw.get("name") or "",
            "category": raw.get("category") or "B2B Equipment",
            "price_range_kzt": raw.get("price_range_kzt") or raw.get("price_range") or "",
            "models": raw.get("models") if isinstance(raw.get("models"), list) else [],
            "specs": raw.get("specs") or "",
            "certifications": raw.get("certifications") or "",
            "target_use_case": raw.get("target_use_case") or "",
        }
        try:
            validated = ProductResearchSchema.model_validate(normalized)
        except ValidationError as exc:
            raise AIEngineError(f"product_schema_validation_error: {exc}") from None

        out = validated.model_dump()
        # Keep backward-compatible aliases alongside strict fields
        out["name"] = out["name_kz"]
        out["price_range"] = out["price_range_kzt"]
        sources = res.get("sources", [])
        if scraped_url:
            sources = [{"title": "Scraped Product Page", "uri": scraped_url}] + list(sources)
        out["sources"] = sources
        out["scraped_url"] = scraped_url
        out["model_used"] = f"{res['provider']}:{res['model']}"
        out["latency_ms"] = res.get("latency_ms", 0)
        out["cost_usd"] = res.get("estimated_cost_usd", 0.0)
        return out

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
            '"regions": ["all", "almaty", "astana", "shymkent"], '
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
        data["latency_ms"] = res.get("latency_ms", 0)
        data["cost_usd"] = res.get("estimated_cost_usd", 0.0)
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
