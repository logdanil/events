"""Синхронизация активов и источников из underlyings_sources.md (рабочая методология).

Файл — источник полноты: при каждом старте сервиса недостающие активы, ISIN,
биржи и источники добавляются в SQLite. Существующие данные не перезаписываются
и не удаляются (активы, которых нет в файле, остаются под мониторингом —
они попадают в отчёт синхронизации как «лишние»). Операция идемпотентна.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app import db

log = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^##\s+(?P<code>.+?)\s*$")
_FIELD_RE = re.compile(r"^\-\s+\*\*(?P<key>[^:*]+):\*\*\s*(?P<value>.*)$")
_URL_RE = re.compile(r"^\s*-\s+(?P<url>https?://\S+)\s*$")
_TICKER_FROM_URL_RE = re.compile(r"nasdaq\.com/market-activity/(?:stocks|etf)/([a-z0-9.]+)/", re.I)


def parse_underlyings_md(path: str | Path) -> list[dict]:
    """Разбирает markdown в список активов: code, name, isin, exchange, sources."""
    assets: list[dict] = []
    current: dict | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = {"code": m.group("code").strip(), "name": "", "isin": None,
                       "exchange": None, "sources": []}
            assets.append(current)
            continue
        if current is None:
            continue
        f = _FIELD_RE.match(line.strip())
        if f:
            key, value = f.group("key").strip().lower(), f.group("value").strip()
            if key == "базовый актив":
                current["name"] = value
            elif key == "isin":
                current["isin"] = value.strip("`").strip() or None
            elif key.startswith("биржа"):
                current["exchange"] = value or None
            continue
        u = _URL_RE.match(line)
        if u and current is not None:
            url = u.group("url").strip()
            if url not in current["sources"]:
                current["sources"].append(url)
    return [a for a in assets if a["name"]]


def infer_instrument_type(name: str) -> str:
    lower = name.lower()
    if "депозитарные расписки" in lower:
        return "ADR"
    if lower.startswith("акции"):
        return "SHARE"
    if any(w in lower for w in ("etf", "ucits", "trust", "фонд")):
        return "ETF"
    return "ASSET"


def english_alias(name: str) -> str | None:
    """Из «Американские депозитарные расписки на акции Alibaba Group Holding Limited»
    достаёт английскую часть — она нужна matcher'у для сопоставления с публикациями."""
    m = re.search(r"(?:на акции|^Акции)\s+(.+)$", name, re.I)
    if m:
        candidate = m.group(1).strip()
        return candidate if len(candidate) >= 5 else None
    return None


def sync_from_md(path: str | Path) -> dict:
    """Добавляет недостающее из md в SQLite. Возвращает отчёт синхронизации."""
    parsed = parse_underlyings_md(path)
    existing = {a["code"]: a for a in db.list_assets()}
    report = {"assets_added": [], "isin_filled": [], "sources_added": 0,
              "aliases_added": [], "extra_in_db": []}

    for item in parsed:
        code = item["code"]
        asset = existing.get(code)
        if asset is None:
            db.upsert_asset(code, item["name"], infer_instrument_type(item["name"]))
            report["assets_added"].append(code)
        # Точные идентификаторы: заполняем только пустые, ничего не перезаписываем.
        current_isin = (asset or {}).get("isin")
        if item["isin"] and not current_isin:
            db.set_asset_field(code, "isin", item["isin"])
            report["isin_filled"].append(code)
        if item["exchange"] and not (asset or {}).get("exchange"):
            db.set_asset_field(code, "exchange", item["exchange"])
        # Английское имя эмитента из русского описания — как алиас для matcher.
        alias = english_alias(item["name"])
        if alias and db.add_alias(code, alias):
            report["aliases_added"].append(f"{code}:{alias}")
        # Тикеры, безопасно выводимые из URL источников (nasdaq.com/.../stocks/tsm/):
        # и как алиас, и как underlying_ticker (если он ещё не заполнен) —
        # иначе matcher не увидит тикер, совпадающий с кодом контракта.
        for url in item["sources"]:
            m = _TICKER_FROM_URL_RE.search(url)
            if not m:
                continue
            ticker = m.group(1).upper()
            if db.add_alias(code, ticker):
                report["aliases_added"].append(f"{code}:{ticker}")
            fresh = db.get_asset(code) if asset is None else asset
            if not (fresh or {}).get("underlying_ticker"):
                db.set_asset_field(code, "underlying_ticker", ticker)
        # Источники: реестр + подписка (идемпотентно по URL).
        known_urls = {s["url"] for s in (asset or {}).get("sources", [])}
        for url in item["sources"]:
            if url not in known_urls:
                db.add_source(code, url)
                report["sources_added"] += 1

    md_codes = {a["code"] for a in parsed}
    report["extra_in_db"] = sorted(set(existing) - md_codes)
    if report["assets_added"] or report["sources_added"] or report["isin_filled"]:
        log.info("Синхронизация с underlyings_sources.md: активов добавлено %d, "
                 "ISIN заполнено %d, источников добавлено %d",
                 len(report["assets_added"]), len(report["isin_filled"]), report["sources_added"])
    if report["extra_in_db"]:
        log.info("В базе есть активы, отсутствующие в md (остаются под мониторингом): %s",
                 ", ".join(report["extra_in_db"]))
    return report


# --- Резервные источники: каналы, которые по устройству не блокируют ботов -------

_GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:3d&hl=en-US&gl=US&ceid=US:en"
_SEC_BY_CIK = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
               "&type=&dateb=&owner=include&count=40&output=atom")
_CORP_TERMS = "(split OR dividend OR delisting OR merger OR acquisition OR listing OR spin-off OR tender OR ADR)"


_COMMODITY_TERMS = "(contract OR specification OR exchange OR delivery OR settlement OR notice)"


def _news_query(asset: dict) -> str:
    """Запрос Google News: для бумаг — точное имя + тикер + корпоративные термины;
    для сырья (ASSET) — простой запрос по названию контракта + биржевые термины
    (корпоративные термины для LME/CME неуместны и дают пустую выдачу)."""
    from urllib.parse import quote_plus
    name = re.sub(r"\s*\([^)]*\)\s*$", "", asset["name"]).strip()
    name = re.sub(r"^(Американские депозитарные расписки на акции|Акции)\s+", "", name, flags=re.I)
    if asset.get("instrument_type") == "ASSET":
        # «LBMA precious metal prices (…)» слишком длинно — берём до запятой/скобки
        short = re.split(r"[,(]", name)[0].strip()
        short = short.replace(" futures", "").replace(" Futures", "").replace(" Index", "")
        return quote_plus(f"{short} futures {_COMMODITY_TERMS}")
    parts = [f'"{name}"']
    ticker = asset.get("underlying_ticker")
    if ticker and len(ticker) >= 3:
        parts.append(ticker)
    return quote_plus(" OR ".join(parts) + " " + _CORP_TERMS)


def ensure_backup_sources() -> dict:
    """Для каждого актива гарантирует резервные каналы:
    - SEC EDGAR по CIK (официальные филинги эмитента/фонда) — если CIK известен;
    - Google News RSS по имени и тикеру — для всех.
    Идемпотентно: существующие источники не трогаются."""
    report = {"sec_added": 0, "news_added": 0}
    for asset in db.list_assets():
        existing = {s["url"] for s in asset.get("sources", [])}
        if asset.get("cik"):
            url = _SEC_BY_CIK.format(cik=str(asset["cik"]).zfill(10))
            if url not in existing:
                db.add_source(asset["code"], url, kind="official", source_type="SEC",
                              refresh_interval=60)
                report["sec_added"] += 1
        url = _GOOGLE_NEWS.format(q=_news_query(asset))
        if url not in existing:
            db.add_source(asset["code"], url, kind="news", source_type="NEWS",
                          refresh_interval=60)
            report["news_added"] += 1
    if report["sec_added"] or report["news_added"]:
        log.info("Резервные источники: SEC по CIK +%d, Google News +%d",
                 report["sec_added"], report["news_added"])
    return report
