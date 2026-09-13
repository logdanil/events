"""Конвейер мониторинга, построенный вокруг ИСТОЧНИКОВ:

источники → публикации → локальная фильтрация → сопоставление активам
→ кэш → LLM (батчами) → SQLite → Telegram.

Каждый источник опрашивается один раз за цикл; публикации сопоставляются
со всеми активами локально. Каскад: RSS (уровень 1) → HTML (уровень 2)
→ Tavily (уровень 3, не чаще раза в сутки либо /fullscan).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app import db, health, html_monitor, matcher, rss_monitor
from app.config import settings
from app.formatters import classify_severity
from app.llm import EVENT_TYPES, LLMUsage, classify_publications, get_provider
from app.search_monitor import SearchMonitor
from app import search_monitor

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.72
# Публикация старше этого срока (по дате публикации от модели) событием не считается:
# агрегаторы переиндексируют старые статьи со свежей датой ленты.
STALE_DAYS = 10
# Окно семантической дедупликации: события одного типа по одному активу
# в пределах окна считаются одним событием (пересказы не рассылаются повторно).
EVENT_DEDUP_DAYS = 14


@dataclass
class ScanStats:
    sources_checked: int = 0
    rss_publications: int = 0
    html_publications: int = 0
    tavily_publications: int = 0
    publications: int = 0          # fetched_total
    cache_hits: int = 0            # отсечено кэшем
    new_after_cache: int = 0       # новых после кэша
    after_keywords: int = 0        # прошло keyword-фильтр
    matched_high: int = 0
    matched_medium: int = 0
    matched_low: int = 0
    unmatched: int = 0
    irrelevant_global: int = 0     # чужие публикации глобальных фидов
    after_filter: int = 0          # публикаций передано в LLM (sent_to_llm)
    llm_failed: int = 0            # публикаций из сбойных батчей (будут повторены)
    llm_calls: int = 0
    llm_events: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    rejected: int = 0
    duplicates: int = 0
    saved: int = 0
    errors: int = 0
    assets_scanned: int = 0

    def summary_ru(self) -> str:
        lines = [
            "Статистика проверки:",
            f"• проверено источников: {self.sources_checked}",
            f"• скачано публикаций: {self.publications}",
            f"  ├ RSS: {self.rss_publications}",
            f"  ├ HTML: {self.html_publications}",
            f"  └ Tavily: {self.tavily_publications}",
            f"• отсечено кэшем: {self.cache_hits}",
            f"• новых после кэша: {self.new_after_cache}",
            f"• прошло keyword-фильтр: {self.after_keywords}",
            "• сопоставлено:",
            f"  ├ high: {self.matched_high}",
            f"  ├ medium: {self.matched_medium}",
            f"  ├ low: {self.matched_low}",
            f"  └ unmatched: {self.unmatched}",
            f"• нерелевантных публикаций глобальных источников: {self.irrelevant_global}",
            f"• передано в LLM: {self.after_filter} (вызовов: {self.llm_calls}, "
            f"токены: {self.tokens_in}+{self.tokens_out})",
        ]
        if self.llm_failed:
            lines.append(f"• сбой LLM: {self.llm_failed} публикаций будут повторены в следующем скане")
        lines += [
            f"• событий от модели: {self.llm_events}",
            f"• отклонено: {self.rejected}",
            f"• дубликатов: {self.duplicates}",
            f"• записано: {self.saved}",
            f"• ошибок: {self.errors}",
            f"• стоимость: ${self.cost_usd:.5f}",
        ]
        return "\n".join(lines)


def _fingerprint(asset_code: str, event: dict) -> str:
    raw = "|".join([
        asset_code.upper(), event.get("event_type", "").strip().lower(),
        str(event.get("event_date") or ""),
        re.sub(r"\W+", " ", event.get("title", "").lower()).strip(),
        urlparse(event.get("source_url", "")).path.rstrip("/")
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_event(asset_code: str, raw: dict) -> dict:
    """Приводит событие (классификация LLM либо тестовые данные) к форме строки events."""
    sources = [s for s in (raw.get("sources") or []) if isinstance(s, dict) and s.get("url")]
    primary = sources[0] if sources else {}
    action_required = raw.get("action_required")
    event = {
        "asset_code": asset_code,
        "event_type": raw.get("event_type", ""),
        "severity": classify_severity(raw.get("event_type"), raw.get("severity")),
        "title": (raw.get("title_ru") or raw.get("title") or raw.get("summary") or "").strip()[:300],
        "summary_ru": (raw.get("summary_ru") or raw.get("summary") or "").strip(),
        "impact_ru": (raw.get("impact_ru") or raw.get("impact") or "").strip() or None,
        "action_required": None if action_required is None else bool(action_required),
        "recommended_action": (raw.get("recommended_action_ru") or raw.get("recommended_action") or "").strip() or None,
        "event_date": raw.get("event_date") or None,
        "publication_date": raw.get("publication_date") or None,
        "effective_date": raw.get("effective_date") or None,
        "status": (raw.get("status") or "").strip().lower() or None,
        "underlying_ticker": (raw.get("underlying_ticker") or "").strip() or None,
        "confidence": float(raw.get("confidence", 0) or 0),
        "source_url": primary.get("url") or raw.get("source_url", ""),
        "source_name": primary.get("name") or raw.get("source_name"),
        "key_parameters_json": json.dumps(raw.get("key_parameters") or [], ensure_ascii=False),
        "additional_sources_json": json.dumps(sources[1:], ensure_ascii=False),
    }
    event["fingerprint"] = _fingerprint(asset_code, event)
    return event


def split_sources(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Каскад: источники с фидами (уровень 1) и HTML-страницы (уровень 2)."""
    rss, html = [], []
    for s in sources:
        parser = (s.get("parser") or "auto").lower()
        looks_rss = parser == "rss" or (parser == "auto" and (
            s["type"] in ("RSS", "SEC")
            or any(x in s["url"].lower() for x in ("rss", "atom", "feed", ".xml", "output=atom"))))
        (rss if looks_rss else html).append(s)
    return rss, html


def _target_asset(c: dict, candidates: list[str]) -> str | None:
    """Актив, о котором публикация: код от модели, если он среди кандидатов;
    единственный кандидат — если модель код не вернула."""
    code = str(c.get("asset_code") or "").strip().upper()
    if code and code in candidates:
        return code
    if len(candidates) == 1 and not code:
        return candidates[0]
    return None


def _stale_reason(c: dict) -> str:
    """Устаревшая публикация: дата публикации (или дата события в прошлом) старше STALE_DAYS."""
    from datetime import date
    for key in ("publication_date", "event_date"):
        raw = c.get(key)
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        age = (date.today() - d).days
        if key == "event_date" and age < 0:
            return ""  # событие в будущем — актуально
        if age > STALE_DAYS:
            return f"устаревшая публикация: {key} {d.isoformat()} ({age} дн. назад)"
        return ""  # дата свежая — дальше не проверяем
    return ""


def classification_ok(c: dict, min_confidence: float = MIN_CONFIDENCE,
                      allow_rumors: bool = False) -> tuple[bool, str]:
    """Проверка классификации LLM. Возвращает (принято, причина отклонения)."""
    if c.get("about_asset") is False:
        return False, "публикация не об этом активе"
    if not c.get("is_event"):
        return False, "не событие"
    stale = _stale_reason(c)
    if stale:
        return False, stale
    if c.get("event_type") not in EVENT_TYPES or c.get("event_type") == "other":
        return False, f"event_type «{c.get('event_type')}» вне перечня"
    try:
        conf = float(c.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < min_confidence:
        return False, f"confidence {conf:.2f} < {min_confidence}"
    if (c.get("status") or "").lower() == "rumor" and not allow_rumors:
        return False, "статус rumor: не рассылается"
    return True, ""


class Monitor:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(25, pool=60),
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=20))
        self.llm = get_provider()
        self.search = SearchMonitor()

    async def collect_publications(self, sources: list[dict],
                                   stats: ScanStats | None = None,
                                   with_search: bool = False,
                                   assets: list[dict] | None = None) -> list[dict]:
        """Опрашивает источники (RSS и HTML параллельно) и при необходимости Tavily."""
        rss_sources, html_sources = split_sources(sources)
        tasks = [rss_monitor.fetch_all(self.http, rss_sources),
                 html_monitor.fetch_all(self.http, html_sources)]
        rss_pubs, html_pubs = await asyncio.gather(*tasks)
        tavily_pubs: list[dict] = []
        if with_search and assets:
            tavily_pubs = await self.search.fetch_all(assets)
            search_monitor.mark_run()
        publications = rss_pubs + html_pubs + tavily_pubs
        if stats:
            stats.sources_checked += len(sources)
            stats.rss_publications += len(rss_pubs)
            stats.html_publications += len(html_pubs)
            stats.tavily_publications += len(tavily_pubs)
            stats.publications += len(publications)  # fetched_total считается здесь же
        return publications

    async def classify(self, filtered: list[dict], stats: ScanStats | None = None,
                       min_confidence: float = MIN_CONFIDENCE,
                       allow_rumors: bool = False) -> list[dict]:
        """Батч-классификация публикаций и превращение в нормализованные события."""
        if not filtered:
            return []
        # Имя актива-кандидата передаётся модели: публикация не о нём — не событие.
        asset_names = {a["code"]: a["name"] for a in db.list_assets()}
        items = [{"index": i, "title": p["title"], "published_at": p.get("published_at"),
                  "source": p.get("source"), "url": p["url"], "text": p.get("text", ""),
                  "asset": "; ".join(f"{c} — {asset_names.get(c, c)}" for c in p.get("asset_codes", []))}
                 for i, p in enumerate(filtered)]
        classifications, usage, failed = await classify_publications(self.llm, items)
        if usage.calls:
            if len(failed) < len(items):
                info = health.check_llm_health(True)
                if info:
                    health.pending_resolved.append(info)
            else:
                a = health.check_llm_health(False, "все батчи завершились ошибкой")
                if a:
                    health.pending_alerts.append(a)
        if stats:
            stats.llm_calls += usage.calls
            stats.tokens_in += usage.input_tokens
            stats.tokens_out += usage.output_tokens
            stats.cost_usd += usage.cost_usd
        if stats:
            stats.llm_failed += len(failed)
        events: list[dict] = []
        for i, pub in enumerate(filtered):
            if i in failed:
                # Сбой LLM не финализирует публикацию: она вернётся в следующем скане
                # (pending_llm при этом сохраняется — не трогаем запись).
                continue
            c = classifications.get(i)
            llm_result, status = "no_response", "fetched"
            if c:
                status = "llm_analyzed"
                if c.get("is_event") and stats:
                    stats.llm_events += 1
                accepted, reason = classification_ok(c, min_confidence, allow_rumors)
                target = _target_asset(c, pub["asset_codes"])
                if accepted and not target:
                    accepted, reason = False, "модель не отнесла публикацию ни к одному кандидату"
                if accepted:
                    llm_result, status = c.get("event_type", "event"), "event_created"
                    # Одна публикация — одно событие, только для актива, о котором она.
                    # Копирование на всех кандидатов давало «присосавшиеся» события.
                    events.append(_normalize_event(target, {
                        **c,
                        "title": pub["title"],
                        "sources": [{"name": pub.get("source"), "url": pub["url"]}],
                    }))
                else:
                    llm_result, status = f"rejected:{reason[:80]}", "rejected"
                    if c.get("is_event") and stats:
                        stats.rejected += 1
            db.remember_article(pub["url"], pub["url_hash"], pub.get("title_hash"),
                                pub.get("published_at"), pub.get("source"), llm_result,
                                status=status, title=pub.get("title"), text=pub.get("text"))
        return events

    def save_events(self, events: list[dict], stats: ScanStats | None = None) -> list[dict]:
        saved = []
        for e in events:
            if db.event_exists(e["fingerprint"]):
                if stats:
                    stats.duplicates += 1
                continue
            # Другая статья о том же событии (тип+актив в окне) — дубликат, не уведомляем.
            if db.similar_event_exists(e["asset_code"], e["event_type"], days=EVENT_DEDUP_DAYS):
                if stats:
                    stats.duplicates += 1
                log.info("Семантический дубликат: %s/%s уже есть за последние %d дн. — «%.60s»",
                         e["asset_code"], e["event_type"], EVENT_DEDUP_DAYS, e.get("title", ""))
                continue
            row_id = db.save_event(e)
            if row_id:
                e["id"] = row_id
                if stats:
                    stats.saved += 1
                saved.append(e)
        return saved

    async def scan_all(self, progress=None, stats: ScanStats | None = None,
                       force_full: bool = False, due_only: bool = False) -> list[dict]:
        """Полный цикл: источники → публикации → фильтры → LLM → база.
        force_full — принудительно включить Tavily (команда /fullscan)."""
        if db.get_setting("paused", "0") == "1":
            log.info("Сканирование пропущено: включена пауза")
            return []
        stats = stats if stats is not None else ScanStats()
        run_id = db.start_scan_run()
        assets = db.list_assets(enabled_only=True)
        stats.assets_scanned = len(assets)
        try:
            sources = db.list_source_defs(enabled_only=True, due_only=due_only)
            with_search = force_full or (search_monitor.is_due() and bool(settings.tavily_api_key))
            if progress:
                await progress(1, 4, f"источники ({len(sources)})")
            publications = await self.collect_publications(
                sources, stats, with_search=with_search, assets=assets)

            if progress:
                await progress(2, 4, "фильтрация")
            profiles = matcher.build_profiles(assets, db.list_matcher_rules())
            filtered = matcher.filter_publications(publications, profiles,
                                                   settings.lookback_days, stats)
            # Публикации, сопоставленные вручную (/match) или через /rematch,
            # ждут LLM-анализа: восстанавливаем их из кэша без повторного скачивания.
            for row in db.pending_matched_publications():
                filtered.append({
                    "url": row["url"], "url_hash": row["url_hash"], "title_hash": row["title_hash"],
                    "title": row["title"] or "", "text": row["text"] or "",
                    "published_at": row["published_at"], "source": row["source"],
                    "asset_codes": [row["best_asset"]] if row["best_asset"] else [],
                })
            filtered = [p for p in filtered if p.get("asset_codes")]
            stats.after_filter = len(filtered)

            if progress:
                await progress(3, 4, f"LLM ({len(filtered)} публикаций)")
            events = await self.classify(filtered, stats)
            new_events = self.save_events(events, stats)
            if progress:
                await progress(4, 4, "готово")
        except Exception as exc:
            stats.errors += 1
            db.finish_scan_run(run_id, "error", stats.assets_scanned, stats.saved,
                               error=str(exc)[:500], errors=stats.errors)
            raise
        if stats.llm_calls or stats.cache_hits:
            db.record_llm_usage(self.llm.name, self.llm.model, stats.llm_calls,
                                stats.tokens_in, stats.tokens_out, stats.cost_usd, stats.cache_hits)
        with db.connect() as conn:
            conn.execute("UPDATE scan_runs SET stats_json=? WHERE id=?",
                         (json.dumps(stats.__dict__, ensure_ascii=False), run_id))
        db.finish_scan_run(run_id, "ok", stats.assets_scanned, stats.saved, errors=stats.errors)
        db.prune_processed_articles(days=max(30, settings.lookback_days * 10))
        log.info("%s", stats.summary_ru().replace("\n", " | "))
        return new_events
