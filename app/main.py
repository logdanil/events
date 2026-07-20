from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dt_time
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app import db, formatters, llm, matcher
from app.bot import (
    start, show_id, list_assets, add_asset, add_source, remove_asset,
    set_interval, pause, resume, digest, status, setup_commands,
    dbstats, test_event, test_report, add_alias_cmd, find_assets, resolve_or_reply,
    events_history, show_event, reply_html, send_html_message, cost,
    match_test, unmatched_cmd, match_cmd, ignore_publication, set_id_cmd, rematch,
    queue_cmd, delete_test, delete_event_cmd, add_user, remove_user, users,
    reports_cmd, scans_history, articles,
)
from app.config import settings
from app.formatters import MSK
from app.monitor import MIN_CONFIDENCE, Monitor, ScanStats, classification_ok, _normalize_event
from app.retry import with_retries

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

monitor = Monitor()
scan_lock = asyncio.Lock()

async def send_events(app: Application, events: list[dict]) -> None:
    # Events saved earlier but never delivered (e.g. Telegram was down) are retried first.
    pending = {e["fingerprint"]: e for e in db.unsent_events()}
    for e in events:
        pending.setdefault(e["fingerprint"], e)
    subscriber_ids = [s["chat_id"] for s in db.list_subscribers()]
    for event in pending.values():
        asset = db.get_asset(event["asset_code"])
        text = formatters.format_event_short(event, asset)
        markup = formatters.event_details_button(event["id"]) if event.get("id") else None
        try:
            await with_retries(
                lambda text=text, markup=markup: send_html_message(
                    app.bot, settings.admin_telegram_id, text, reply_markup=markup),
                what=f"Telegram send [{event['asset_code']}]",
            )
        except Exception:
            log.exception("Failed to deliver event %s, will retry on next scan", event["fingerprint"])
            continue
        # sent_at ставится по доставке администратору; сбой у отдельного подписчика
        # (заблокировал бота и т.п.) не должен вызывать повторную рассылку всем.
        db.mark_sent(event["fingerprint"])
        for chat_id in subscriber_ids:
            try:
                await with_retries(
                    lambda text=text, markup=markup, chat_id=chat_id: send_html_message(
                        app.bot, chat_id, text, reply_markup=markup),
                    what=f"Telegram send subscriber {chat_id}",
                )
            except Exception:
                log.warning("Не удалось доставить событие подписчику %s", chat_id)


async def event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Подробнее» под кратким уведомлением: присылает полный отчёт.
    Доступна администратору и подписчикам уведомлений."""
    from app.bot import is_viewer
    query = update.callback_query
    if not is_viewer(update):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer()
    event_id = int(query.data.split(":", 1)[1])
    e = db.get_event(event_id)
    if not e:
        await query.message.reply_text(f"Событие #{event_id} не найдено.")
        return
    await reply_html(query.message, formatters.format_event_full(e, db.get_asset(e["asset_code"])))


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.bot import SCAN_REPORTS_KEY
    if scan_lock.locked():
        log.warning("Scan skipped: previous scan is still running")
        return
    reports_on = db.get_setting(SCAN_REPORTS_KEY, "1") == "1"
    async with scan_lock:
        stats = ScanStats()
        started_at = datetime.now(MSK)
        try:
            # По расписанию опрашиваются только источники с истёкшим refresh_interval.
            events = await monitor.scan_all(due_only=True, stats=stats)
        except Exception:
            log.exception("Scheduled scan failed")
            if reports_on and settings.admin_telegram_id:
                try:
                    await send_html_message(context.application.bot, settings.admin_telegram_id,
                                            "❌ Плановая проверка завершилась ошибкой. Подробности в логах.")
                except Exception:
                    log.exception("Не удалось отправить отчёт об ошибке")
            return
        await send_events(context.application, events)
    # Отчёт о проверке (переключается командой /reports): события отправляются всегда,
    # сводка — только если отчёты включены. Получают все: администратор и подписчики.
    if reports_on:
        from app.bot import notification_recipients
        summary = formatters.format_scan_summary(stats, started_at=started_at,
                                                 finished_at=datetime.now(MSK), new_events=events)
        for chat_id in notification_recipients():
            try:
                # Отчёт без новых событий приходит без звука; со звуком — только события.
                await send_html_message(context.application.bot, chat_id, summary,
                                        silent=not events)
            except Exception:
                log.warning("Не удалось отправить отчёт о проверке получателю %s", chat_id)


async def _run_manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           force_full: bool) -> None:
    from app.bot import guard
    if not await guard(update):
        return
    if scan_lock.locked():
        await update.effective_message.reply_text("Проверка уже выполняется.")
        return
    label = "полную проверку (с резервным поиском)" if force_full else "проверку"
    progress_message = await update.effective_message.reply_text(f"Запускаю {label}…")

    async def progress(done: int, total: int, step: str):
        try:
            await progress_message.edit_text(f"Шаг {done}/{total}: {step}")
        except Exception:
            pass

    async with scan_lock:
        stats = ScanStats()
        started_at = datetime.now(MSK)
        try:
            events = await monitor.scan_all(progress=progress, stats=stats, force_full=force_full)
        except Exception:
            log.exception("Manual scan failed")
            await progress_message.edit_text("Проверка завершилась ошибкой. Подробности в логах сервиса.")
            return
        await send_events(context.application, events)
    summary = formatters.format_scan_summary(stats, started_at=started_at,
                                             finished_at=datetime.now(MSK), new_events=events)
    try:
        await progress_message.edit_text(summary, parse_mode="HTML")
    except Exception:
        await progress_message.edit_text(formatters.strip_html(summary))
    # При включённых отчётах (/reports on) сводку ручной проверки получают
    # и остальные: подписчики + админ, кроме чата, где команда была запущена.
    from app.bot import SCAN_REPORTS_KEY, notification_recipients
    if db.get_setting(SCAN_REPORTS_KEY, "1") == "1":
        invoker = update.effective_user.id if update.effective_user else None
        for chat_id in notification_recipients():
            if chat_id == invoker:
                continue
            try:
                await send_html_message(context.application.bot, chat_id, summary,
                                        silent=not events)
            except Exception:
                log.warning("Не удалось отправить сводку проверки получателю %s", chat_id)


async def manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_manual_scan(update, context, force_full=False)


async def full_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_manual_scan(update, context, force_full=True)


async def process_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/processqueue — обработать очередь pending_llm через Gemini: без сетевого
    поиска, только сохранённые публикации → классификация → события."""
    from app.bot import guard
    if not await guard(update):
        return
    rows = db.pending_matched_publications()
    if not rows:
        await update.effective_message.reply_text("Очередь pending_llm пуста.")
        return
    if scan_lock.locked():
        await update.effective_message.reply_text("Проверка уже выполняется, попробуйте позже.")
        return
    progress_message = await update.effective_message.reply_text(
        f"Обрабатываю очередь: {len(rows)} публикаций…")
    async with scan_lock:
        stats = ScanStats()
        filtered = [{
            "url": row["url"], "url_hash": row["url_hash"], "title_hash": row["title_hash"],
            "title": row["title"] or "", "text": row["text"] or "",
            "published_at": row["published_at"], "source": row["source"],
            "asset_codes": [row["best_asset"]] if row["best_asset"] else [],
        } for row in rows if row["best_asset"]]
        stats.after_filter = len(filtered)
        events = await monitor.classify(filtered, stats)
        new_events = monitor.save_events(events, stats)
        await send_events(context.application, new_events)
        if stats.llm_calls:
            db.record_llm_usage(monitor.llm.name, monitor.llm.model, stats.llm_calls,
                                stats.tokens_in, stats.tokens_out, stats.cost_usd, 0)
    text = (f"Очередь обработана: {len(filtered)} публикаций\n"
            f"• вызовов Gemini: {stats.llm_calls}\n"
            f"• событий от модели: {stats.llm_events}\n"
            f"• новых событий: {stats.saved}\n"
            f"• дубликатов: {stats.duplicates}\n"
            f"• отклонено: {stats.rejected}\n"
            + (f"• сбой LLM: {stats.llm_failed} останутся в очереди\n" if stats.llm_failed else "")
            + f"• стоимость: ${stats.cost_usd:.5f}")
    await progress_message.edit_text(text)


async def pipe_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pipetest АКТИВ [mock] — сквозной локальный тест конвейера на демо-публикации.
    Создаёт тестовое событие (is_test=1), не попадающее в рабочие списки."""
    from app.bot import guard
    from app.monitor import classification_ok, _normalize_event
    if not await guard(update):
        return
    msg = update.effective_message
    args = list(context.args)
    mock = bool(args) and args[-1].lower() == "mock"
    if mock:
        args.pop()
    if not args:
        await msg.reply_text("Формат: /pipetest АКТИВ [mock]")
        return
    asset = await resolve_or_reply(update, " ".join(args))
    if not asset:
        return
    stages: list[str] = [f"🧪 Сквозной тест конвейера для {asset['code']}"
                         + (" (mock, без Gemini)" if mock else "")]
    title = f"{asset['name'].split(' (')[0]} declares quarterly distribution"
    pub = {"url": f"https://pipetest.local/{asset['code']}/{int(datetime.now().timestamp())}",
           "title": title, "text": "", "published_at": str(datetime.now(MSK).date()),
           "source": "pipetest", "subscribers": [asset["code"]], "source_id": -1}
    stages.append(f"1. Публикация: «{title}»")

    keywords_ok = matcher.has_corporate_keywords(pub)
    stages.append(f"2. Keyword-фильтр: {'пройден' if keywords_ok else 'НЕ пройден'}")
    if not keywords_ok:
        await _say(msg, "\n".join(stages))
        return

    profiles = matcher.build_profiles(db.list_assets(enabled_only=True), db.list_matcher_rules())
    profile = next(p for p in profiles if p.code == asset["code"])
    result = matcher.match_one(pub, profile)
    stages.append(f"3. Matcher: score {result.score:.2f} [{result.confidence}] — "
                  f"{', '.join(result.matched_by) or 'нет признаков'}")
    if result.confidence not in ("high", "medium"):
        stages.append("Публикация не достигла high/medium — в Gemini не передаётся.")
        await _say(msg, "\n".join(stages))
        return
    stages.append("4. Очередь: публикация готова к классификации")

    if mock:
        classification = {"index": 0, "is_event": True, "event_type": "special_dividend",
                          "severity": "medium", "confidence": 0.95,
                          "summary": "Фонд объявил квартальное распределение дохода (mock-тест конвейера).",
                          "impact": "Диагностическое событие, влияния нет.",
                          "event_date": str(datetime.now(MSK).date()), "status": "confirmed",
                          "action_required": False, "key_parameters": []}
        stages.append("5. Gemini: пропущен (mock), использован предсказуемый JSON · стоимость $0.00000")
    else:
        classifications, usage, failed = await llm.classify_publications(
            monitor.llm, [{"index": 0, **pub}])
        if failed or 0 not in classifications:
            stages.append("5. Gemini: сбой вызова — проверьте ключ и модель (/cost, логи)")
            await _say(msg, "\n".join(stages))
            return
        classification = classifications[0]
        stages.append(f"5. Gemini: {usage.calls} вызов, токены {usage.input_tokens}+{usage.output_tokens}, "
                      f"${usage.cost_usd:.5f}")
        db.record_llm_usage(monitor.llm.name, monitor.llm.model, usage.calls,
                            usage.input_tokens, usage.output_tokens, usage.cost_usd, 0)
    accepted, reason = classification_ok(classification)
    stages.append(f"6. Классификация: {json.dumps(classification, ensure_ascii=False)[:200]}")
    if not accepted:
        stages.append(f"   Отклонено: {reason}")
        await _say(msg, "\n".join(stages))
        return
    event = _normalize_event(asset["code"], {
        **classification, "title": pub["title"],
        "sources": [{"name": "pipetest", "url": pub["url"]}]})
    event["is_test"] = 1
    row_id = db.save_event(event)
    if not row_id:
        stages.append("7. Событие: дубликат, не записано")
        await _say(msg, "\n".join(stages))
        return
    event["id"] = row_id
    db.mark_sent(event["fingerprint"])
    stages.append(f"7. Событие записано: id={row_id}, is_test=1 (в рабочий /events не попадает)")
    stages.append("8. Форматирование и отправка ↓")
    await _say(msg, "\n".join(stages))
    await reply_html(msg, formatters.format_event_short(event, asset),
                     reply_markup=formatters.event_details_button(row_id))


DAILY_SUMMARY_KEY = "daily_summary"

_SUMMARY_PROMPT = """Составь краткую сводку дня для команды мониторинга корпоративных событий.
Пиши по-русски, деловым языком, 5–10 коротких строк обычного текста (без markdown и HTML).
Структура: 1) общая картина дня одной фразой; 2) главные темы публикаций, которые анализировались
(сгруппируй похожие); 3) события, если были; 4) что было отклонено и почему (одной-двумя фразами);
5) на что обратить внимание завтра, если есть кандидаты на грани порога.
Только факты из данных, ничего не выдумывай. Данные дня (JSON):
"""


async def build_daily_summary() -> str:
    """Собирает саммари дня: цифры локально, связный текст — Gemini (с фолбэком без него)."""
    data = db.daily_summary_data(hours=24)
    narrative = None
    try:
        prompt_payload = {
            "проверок": data["scans"],
            "статусы_публикаций": data["status_counts"],
            "анализировала_модель": [
                {"заголовок": (a.get("title") or "")[:120], "источник": a.get("source"),
                 "актив": a.get("best_asset"), "итог": a.get("llm_result")}
                for a in data["llm_seen"]],
            "события": [
                {"актив": e.get("asset_code"), "тип": e.get("event_type"),
                 "заголовок": (e.get("title") or "")[:120]} for e in data["events"]],
            "кандидаты_на_грани": [
                {"заголовок": (a.get("title") or "")[:100], "актив": a.get("best_asset"),
                 "score": a.get("best_score")} for a in data["near_miss"]],
        }
        narrative, usage = await monitor.llm.generate(
            _SUMMARY_PROMPT + json.dumps(prompt_payload, ensure_ascii=False))
        db.record_llm_usage(monitor.llm.name, monitor.llm.model, usage.calls,
                            usage.input_tokens, usage.output_tokens, usage.cost_usd, 0)
    except Exception:
        log.exception("Gemini-саммари не удалось, отправляю только цифры")
    return formatters.format_daily_summary(data, narrative)


async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная сводка в 19:00 МСК всем получателям."""
    from app.bot import notification_recipients
    if db.get_setting(DAILY_SUMMARY_KEY, "1") != "1":
        return
    summary = await build_daily_summary()
    for chat_id in notification_recipients():
        try:
            await send_html_message(context.application.bot, chat_id, summary)
        except Exception:
            log.warning("Не удалось отправить дневное саммари получателю %s", chat_id)


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/summary — сводка дня сейчас; /summary on|off — ежедневная рассылка в 19:00 МСК."""
    from app.bot import guard
    if not await guard(update):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "вкл"):
        db.set_setting(DAILY_SUMMARY_KEY, "1")
        await update.effective_message.reply_text("✅ Ежедневное саммари в 19:00 МСК включено (для всех получателей).")
        return
    if arg in ("off", "выкл"):
        db.set_setting(DAILY_SUMMARY_KEY, "0")
        await update.effective_message.reply_text("🔕 Ежедневное саммари выключено. Включить: /summary on")
        return
    progress = await update.effective_message.reply_text("Готовлю сводку дня…")
    text = await build_daily_summary()
    try:
        await progress.delete()
    except Exception:
        pass
    await reply_html(update.effective_message, text)


async def test_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testbroadcast — тестовое сообщение всем получателям с отчётом о доставке."""
    from app.bot import guard, notification_recipients
    if not await guard(update):
        return
    recipients = notification_recipients()
    results = []
    for chat_id in recipients:
        try:
            await context.application.bot.send_message(
                chat_id=chat_id,
                text="🔧 Тестовая рассылка Corporate Events Monitor: если вы видите это "
                     "сообщение, уведомления о событиях и отчёты до вас доходят.")
            results.append(f"✅ {chat_id} — доставлено")
        except Exception as exc:
            results.append(f"❌ {chat_id} — {type(exc).__name__}: {str(exc)[:80]}")
    await update.effective_message.reply_text(
        f"Тестовая рассылка ({len(recipients)} получателей: админ + подписчики):\n"
        + "\n".join(results)
        + "\n\nЕсли получателей меньше, чем ожидали — проверьте /users: подписчики "
          "хранятся в базе того сервера, где работает бот.")


def _redact(text: str) -> str:
    for secret in (settings.tavily_api_key, settings.google_api_key, settings.telegram_bot_token):
        if secret:
            text = text.replace(secret, "***")
    return text


async def _say(message, text: str) -> None:
    for i in range(0, len(text), 3500):
        await message.reply_text(text[i:i + 3500], disable_web_page_preview=True)


async def debug_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Поэтапная диагностика для одного актива: идентификаторы → источники →
    воронка публикаций → лучшие совпадения со скорингом → LLM."""
    from app.bot import guard
    if not await guard(update):
        return
    msg = update.effective_message
    query_text = " ".join(context.args).strip()
    if not query_text:
        await msg.reply_text("Формат: /debugscan АКТИВ — код, тикер, алиас или имя")
        return
    asset = await resolve_or_reply(update, query_text)
    if not asset:
        return

    profiles = matcher.build_profiles(db.list_assets(enabled_only=True), db.list_matcher_rules())
    profile = next(p for p in profiles if p.code == asset["code"])

    # Этап 1. Идентификаторы актива.
    ambiguous = sorted(set(profile.tickers) & matcher.AMBIGUOUS_TICKERS)
    lines = [
        "Этап 1. Идентификаторы актива",
        f"contract_code: {asset['code']}",
        f"underlying_ticker: {asset.get('underlying_ticker') or 'NULL'}",
        f"full_name: {asset['name']}",
        f"issuer: {asset.get('issuer_name') or 'NULL'} · provider: {asset.get('fund_provider') or 'NULL'}",
        f"ISIN: {asset.get('isin') or 'NULL'} · CUSIP: {asset.get('cusip') or 'NULL'} · "
        f"CIK: {asset.get('cik') or 'NULL'} · LEI: {asset.get('lei') or 'NULL'}",
        f"Имена для сопоставления: {'; '.join(profile.names)}",
        f"Алиасы: {', '.join(profile.long_aliases + profile.mid_aliases) or 'нет'}",
        f"Тикеры: {', '.join(profile.tickers) or 'нет'}"
        + (f" (неоднозначные, требуют контекста: {', '.join(ambiguous)})" if ambiguous else ""),
        "Заполнить недостающее: /setid " + asset["code"] + " | cik | 0000884394",
    ]
    await _say(msg, "\n".join(lines))

    # Этап 2. Источники: персональные (owned) и глобальные.
    all_sources = db.list_source_defs(enabled_only=True)
    own = [s for s in all_sources if asset["code"] in s["subscribers"]]
    global_sources = [s for s in all_sources if not s["subscribers"]]
    s_lines = ["Этап 2. Источники",
               f"Персональные: {len(own)} · глобальные: {len(global_sources)}"]
    for s in own:
        owned = len(s["subscribers"]) <= 2
        s_lines.append(f"• [{s['type']}] {s['name']} — "
                       f"{'owned (+0.45 к скору)' if owned else 'общий, нужно текстовое совпадение'}")
    s_lines.append("Глобальные SEC/Nasdaq-фиды сопоставляются только по filer/имени, "
                   "подписка на них не даёт принадлежности.")
    await _say(msg, "\n".join(s_lines))

    if scan_lock.locked():
        await msg.reply_text("Обычная проверка уже выполняется, попробуйте позже.")
        return
    async with scan_lock:
        stats = ScanStats()
        # Этап 3. Публикации: воронка без записи в кэш (диагностика ничего не финализирует).
        try:
            publications = await monitor.collect_publications(own + global_sources, stats)
        except Exception as exc:
            await _say(msg, f"Этап 3. Ошибка получения публикаций: "
                            f"{type(exc).__name__}: {_redact(str(exc))[:400]}")
            return
        fetched_total = len(publications)
        fresh, cached = [], 0
        seen_urls: set[str] = set()
        for p in publications:
            uh, th = matcher.url_hash(p.get("url", "")), matcher.title_hash(p.get("title", ""))
            if uh in seen_urls:
                continue
            seen_urls.add(uh)
            if db.article_seen(uh, th):
                cached += 1
                continue
            fresh.append({**p, "url_hash": uh, "title_hash": th})
        keyword_pass = [p for p in fresh if matcher.has_corporate_keywords(p)]
        scored = []
        for p in keyword_pass:
            results = matcher.match_publication(p, profiles)
            for_asset = next((r for r in results if r.asset_code == asset["code"]), None) \
                or matcher.match_one(p, profile)
            scored.append((p, for_asset, results))
        by_conf = {"high": [], "medium": [], "low": [], "none": []}
        for p, r, _ in scored:
            by_conf[r.confidence].append((p, r))
        await _say(msg, "\n".join([
            "Этап 3. Публикации (воронка)",
            f"fetched_total: {fetched_total} (RSS: {stats.rss_publications}, "
            f"HTML: {stats.html_publications})",
            f"after_cache (новых): {len(fresh)} · в кэше: {cached}",
            f"after_keywords: {len(keyword_pass)}",
            f"matched_to_{asset['code']}: high {len(by_conf['high'])} · "
            f"medium {len(by_conf['medium'])} · low {len(by_conf['low'])} · "
            f"unmatched {len(by_conf['none'])}",
        ]))

        # Этап 4. Топ-10 ближайших совпадений с объяснением.
        top = sorted(scored, key=lambda x: x[1].score, reverse=True)[:10]
        t_lines = ["Этап 4. Лучшие совпадения (топ-10)"]
        for p, r, results in top:
            verdict = ("прошло" if r.confidence in ("high", "medium")
                       else f"не прошло порог {matcher.MEDIUM} (score {r.score:.2f})")
            t_lines.append(
                f"— {p['title'][:90]}\n"
                f"  score {r.score:.2f} [{r.confidence}] · {verdict}\n"
                f"  matched_by: {', '.join(r.matched_by) or '—'} · "
                f"terms: {', '.join(r.matched_terms) or '—'}"
                + (f" · отклонено: {', '.join(r.rejected_terms)}" if r.rejected_terms else ""))
        if not scored:
            t_lines.append("Нет новых публикаций после кэша и keyword-фильтра.")
        await _say(msg, "\n".join(t_lines))

        # Этап 5. LLM только для high/medium (как в обычном скане).
        mine = [{**p, "asset_codes": [asset["code"]]}
                for p, r in by_conf["high"] + by_conf["medium"]]
        if not mine:
            await _say(msg, "Этап 5. LLM: передавать нечего (нет high/medium совпадений).\n\n"
                            + stats.summary_ru())
            return
        items = [{"index": i, "title": p["title"], "published_at": p.get("published_at"),
                  "source": p.get("source"), "url": p["url"], "text": p.get("text", "")}
                 for i, p in enumerate(mine)]
        classifications, usage, failed = await llm.classify_publications(monitor.llm, items)
        stats.llm_calls += usage.calls
        stats.tokens_in += usage.input_tokens
        stats.tokens_out += usage.output_tokens
        stats.cost_usd += usage.cost_usd
        c_lines = [f"Этап 5. LLM: передано {len(items)}, батчей {usage.calls}, "
                   f"токены {usage.input_tokens}+{usage.output_tokens}, ${usage.cost_usd:.5f}"]
        if failed:
            c_lines.append(f"Сбой батчей: {len(failed)} публикаций останутся на следующий скан.")
        for i, p in enumerate(mine):
            if i in failed:
                continue
            c = classifications.get(i)
            c_lines.append(f"{i}) {p['title'][:70]}")
            c_lines.append("   " + (_redact(json.dumps(c, ensure_ascii=False)[:350]) if c else "нет ответа"))
            db.remember_article(p["url"], p["url_hash"], p.get("title_hash"),
                                p.get("published_at"), p.get("source"),
                                (c or {}).get("event_type", "no_response"),
                                status="llm_analyzed" if c else "fetched",
                                title=p.get("title"), text=p.get("text"))
            if not c:
                continue
            accepted, reason = classification_ok(c)
            if not accepted:
                c_lines.append(f"   ✗ {reason}")
                stats.rejected += 1
                continue
            event = _normalize_event(asset["code"], {
                **c, "title": p["title"],
                "sources": [{"name": p.get("source"), "url": p["url"]}]})
            if db.event_exists(event["fingerprint"]):
                c_lines.append("   ≈ дубликат")
                stats.duplicates += 1
                continue
            from app.monitor import EVENT_DEDUP_DAYS
            if db.similar_event_exists(event["asset_code"], event["event_type"], days=EVENT_DEDUP_DAYS):
                c_lines.append(f"   ≈ семантический дубликат ({event['event_type']} по активу за {EVENT_DEDUP_DAYS} дн. уже есть)")
                stats.duplicates += 1
                continue
            row_id = db.save_event(event)
            if row_id:
                event["id"] = row_id
                stats.saved += 1
                db.set_article_status(p["url_hash"], "event_created")
                c_lines.append(f"   ✓ записано событие id={row_id}")
                await reply_html(msg, formatters.format_event_short(event, asset),
                                 reply_markup=formatters.event_details_button(row_id))
                db.mark_sent(event["fingerprint"])
        if stats.llm_calls:
            db.record_llm_usage(monitor.llm.name, monitor.llm.model, stats.llm_calls,
                                stats.tokens_in, stats.tokens_out, stats.cost_usd, stats.cache_hits)
        await _say(msg, "\n".join(c_lines) + f"\n\nДиагностика завершена.\n\n{stats.summary_ru()}")


async def post_init(app: Application) -> None:
    from app import assets_sync
    db.init_db()
    root = Path(__file__).resolve().parent.parent
    # JSON-файлы — только начальная загрузка: единственный источник истины — SQLite.
    db.seed_from_json(str(root / "data" / "assets.json"), str(root / "data" / "event_prompts.json"))
    # Рабочая методология: недостающие активы/ISIN/источники добавляются идемпотентно.
    underlyings = root / "underlyings_sources.md"
    if underlyings.exists():
        assets_sync.sync_from_md(underlyings)
    await setup_commands(app)
    app.bot_data["scheduled_scan"] = scheduled_scan
    if not settings.admin_telegram_id:
        log.warning("ADMIN_TELEGRAM_ID не задан — автоматические проверки не запланированы")
        return
    interval = int(db.get_setting("interval_minutes", str(settings.scan_interval_minutes)))
    app.job_queue.run_repeating(scheduled_scan, interval=interval * 60, first=interval * 60, name="scheduled_scan")
    app.job_queue.run_daily(send_daily_summary, time=dt_time(19, 0, tzinfo=MSK), name="daily_summary")
    log.info("Bot initialized with %s minute interval; daily summary at 19:00 MSK", interval)


def validate_settings() -> None:
    missing = []
    if not settings.telegram_bot_token: missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.google_api_key: missing.append("GOOGLE_API_KEY")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))
    if not settings.tavily_api_key:
        log.warning("TAVILY_API_KEY не задан: резервный поиск (уровень 3) отключён")
    if not settings.admin_telegram_id:
        log.warning("ADMIN_TELEGRAM_ID не задан: доступна только команда /id. "
                    "Отправьте боту /id, запишите номер в .env и перезапустите.")


def main() -> None:
    validate_settings()
    app = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("scan", manual_scan))
    app.add_handler(CommandHandler("fullscan", full_scan))
    app.add_handler(CommandHandler("cost", cost))
    app.add_handler(CommandHandler("list", list_assets))
    app.add_handler(CommandHandler("find", find_assets))
    app.add_handler(CommandHandler("add", add_asset))
    app.add_handler(CommandHandler("alias", add_alias_cmd))
    app.add_handler(CommandHandler("addsource", add_source))
    app.add_handler(CommandHandler("remove", remove_asset))
    app.add_handler(CommandHandler("interval", set_interval))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("digest", digest))
    app.add_handler(CommandHandler("events", events_history))
    app.add_handler(CommandHandler("event", show_event))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("debugscan", debug_scan))
    app.add_handler(CommandHandler("dbstats", dbstats))
    app.add_handler(CommandHandler("testevent", test_event))
    app.add_handler(CommandHandler("testreport", test_report))
    app.add_handler(CommandHandler("matchtest", match_test))
    app.add_handler(CommandHandler("unmatched", unmatched_cmd))
    app.add_handler(CommandHandler("match", match_cmd))
    app.add_handler(CommandHandler("ignorepublication", ignore_publication))
    app.add_handler(CommandHandler("setid", set_id_cmd))
    app.add_handler(CommandHandler("rematch", rematch))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("processqueue", process_queue))
    app.add_handler(CommandHandler("pipetest", pipe_test))
    app.add_handler(CommandHandler("deletetest", delete_test))
    app.add_handler(CommandHandler("deleteevent", delete_event_cmd))
    app.add_handler(CommandHandler("adduser", add_user))
    app.add_handler(CommandHandler("removeuser", remove_user))
    app.add_handler(CommandHandler("users", users))
    app.add_handler(CommandHandler("reports", reports_cmd))
    app.add_handler(CommandHandler("testbroadcast", test_broadcast))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("scans", scans_history))
    app.add_handler(CommandHandler("articles", articles))
    app.add_handler(CallbackQueryHandler(event_callback, pattern=r"^event:\d+$"))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
