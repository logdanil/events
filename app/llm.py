"""Единый LLM-слой: все обращения к модели идут только через LLMProvider.

Модель выполняет ТОЛЬКО классификацию публикаций (JSON in → JSON out).
Оформление сообщений — исключительно локально, в app/formatters.py.
Провайдер по умолчанию — Google Gemini 2.5 Flash через REST (без тяжёлых SDK).
"""
from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.retry import with_retries

log = logging.getLogger(__name__)

# Канонические типы событий для классификатора.
EVENT_TYPES = [
    "stock_split", "reverse_split", "dividend", "special_dividend", "delisting",
    "trading_suspension", "merger", "acquisition", "spin_off", "adr_ratio_change",
    "symbol_change", "isin_change", "etf_liquidation", "methodology_change",
    "index_rebalance", "contract_specification_change", "bankruptcy",
    "tender_offer", "rights_issue", "share_issuance", "capital_return",
    "listing", "conversion", "name_change", "other",
]

# Цены USD за 1M токенов (input, output). Обновляются здесь при смене тарифов.
# Для gemini-3.5-flash взяты тарифы flash-класса — сверьте с актуальным прайсом Google.
MODEL_PRICES = {
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
}

MAX_TEXT_CHARS = 1200  # полный текст статьи в модель никогда не отправляется

PROMPT_HEADER = """Классифицируй публикации о корпоративных событиях. Только классификация, никакого оформления.
У каждой публикации указано поле "asset" — базовый актив, к которому она предположительно относится.
Верни СТРОГО JSON-массив (без markdown и пояснений) с объектом для КАЖДОЙ входной публикации:
{"index": <номер из входа>, "is_event": true|false, "event_type": "<из списка ниже>",
 "severity": "critical|high|medium|low", "confidence": 0..1,
 "summary": "суть по-русски, 1-3 предложения, только факты",
 "impact": "возможное влияние на цену/количество бумаг/параметры контракта, 1-2 предложения или null",
 "event_date": "YYYY-MM-DD"|null, "publication_date": "YYYY-MM-DD"|null, "effective_date": "YYYY-MM-DD"|null,
 "status": "confirmed|reported|preliminary|rumor", "action_required": true|false,
 "key_parameters": [{"label": "...", "value": "..."}]}

is_event=true ТОЛЬКО для первичного сообщения о самом корпоративном действии
(официальное объявление, уведомление биржи/регулятора, новость о факте).

is_event=false обязательно, если:
- публикация НЕ о самом активе из поля "asset" (не о его эмитенте, фонде или контракте) —
  событие у другой компании не относится к этому активу, даже если оно значимое;
- это пересказ, обсуждение, интервью, мнение, комментарий аналитиков или последствия
  УЖЕ объявленного события («аналитики считают…», «эксперт обсуждает…», «на фоне листинга…»,
  «после дебюта…», реакция рынка на событие) — само событие уже было объявлено ранее;
- изменение долей инвесторов/фондов в бумаге (13F, "stake boosted/increased/trimmed", позиции управляющих);
- движение цены, аналитика, прогнозы, рекомендации, обзор рынка, обычная квартальная отчётность;
- для ETF: новости об отдельных компаниях из состава фонда или об индексе — не события самого ETF.
Допустимые event_type: """ + ", ".join(EVENT_TYPES) + "\nПубликации:\n"


@dataclass
class LLMUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "LLMUsage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning("LLM вернула невалидный JSON: %.500s", text)
        return []
    return [x for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []


class LLMProvider(ABC):
    """Интерфейс LLM: чтобы заменить модель, достаточно нового провайдера."""
    name: str = "abstract"
    model: str = ""

    @abstractmethod
    async def classify_batch(self, items: list[dict]) -> tuple[list[dict], LLMUsage]:
        """Один вызов модели для батча публикаций. Каждый item:
        {"index", "title", "published_at", "source", "url", "text"}.
        Возвращает (список классификаций c полем index, использование)."""


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.google_api_key
        self.model = model or settings.model
        self._client = httpx.AsyncClient(timeout=60)

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = MODEL_PRICES.get(self.model, MODEL_PRICES["gemini-2.5-flash"])
        return input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out

    def _build_prompt(self, items: list[dict]) -> str:
        lines = []
        for it in items:
            lines.append(json.dumps({
                "index": it["index"],
                "asset": it.get("asset"),
                "title": (it.get("title") or "")[:300],
                "date": it.get("published_at"),
                "source": it.get("source"),
                "url": it.get("url"),
                "text": (it.get("text") or "")[:MAX_TEXT_CHARS],
            }, ensure_ascii=False))
        return PROMPT_HEADER + "\n".join(lines)

    async def classify_batch(self, items: list[dict]) -> tuple[list[dict], LLMUsage]:
        if not items:
            return [], LLMUsage()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": self._build_prompt(items)}]}],
            # thinkingBudget=0: для классификации размышления не нужны,
            # а thinking-токены тарифицируются как выходные (сокращает стоимость в разы).
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0,
                                 "thinkingConfig": {"thinkingBudget": 0}},
        }

        async def call():
            response = await self._client.post(url, json=payload,
                                               headers={"x-goog-api-key": self.api_key})
            response.raise_for_status()
            return response.json()

        data = await with_retries(call, what=f"Gemini classify ({len(items)} публикаций)")
        text = "".join(
            part.get("text", "")
            for cand in data.get("candidates", [])[:1]
            for part in cand.get("content", {}).get("parts", []))
        meta = data.get("usageMetadata", {})
        input_tokens = int(meta.get("promptTokenCount", 0))
        # thoughtsTokenCount тарифицируется как выходные токены.
        output_tokens = int(meta.get("candidatesTokenCount", 0)) + int(meta.get("thoughtsTokenCount", 0))
        usage = LLMUsage(calls=1, input_tokens=input_tokens, output_tokens=output_tokens,
                         cost_usd=self._cost(input_tokens, output_tokens))
        results = _parse_json_array(text)
        log.info("Gemini: %d публикаций → %d классификаций, %d+%d токенов, $%.5f",
                 len(items), len(results), input_tokens, output_tokens, usage.cost_usd)
        return results, usage

    async def aclose(self) -> None:
        await self._client.aclose()


def get_provider() -> LLMProvider:
    if settings.llm_provider.lower() == "gemini":
        return GeminiProvider()
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")


async def classify_publications(provider: LLMProvider, items: list[dict],
                                batch_size: int | None = None
                                ) -> tuple[dict[int, dict], LLMUsage, set[int]]:
    """Пакетная классификация: N публикаций → ceil(N/batch_size) вызовов модели.
    Возвращает (index → классификация, использование, индексы сбойных батчей).
    Публикации из сбойных батчей НЕ должны помечаться обработанными —
    они обязаны попасть в следующий скан."""
    batch_size = batch_size or settings.llm_batch_size
    total = LLMUsage()
    by_index: dict[int, dict] = {}
    failed: set[int] = set()
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        try:
            results, usage = await provider.classify_batch(batch)
        except Exception:
            log.exception("Классификация батча (%d публикаций) не удалась", len(batch))
            failed.update(int(it["index"]) for it in batch)
            continue
        total.add(usage)
        for r in results:
            try:
                by_index[int(r.get("index"))] = r
            except (TypeError, ValueError):
                continue
    return by_index, total, failed
