from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, timedelta

import telegram.error
from telegram import Update, BotCommand
from telegram.ext import ContextTypes

from app import db, formatters, matcher
from app.config import settings

log=logging.getLogger(__name__)

HELP = """<b>Мониторинг корпоративных событий</b>

/scan — проверить источники сейчас
/fullscan — полная проверка, включая резервный поиск Tavily
/reports on|off — лично вам: сводка после каждой проверки или только при новых событиях
/reports all on|off — то же по умолчанию для всех, кто не выбрал сам
/summary — сводка дня + Excel-детализация файлом; /summary on|off — ежедневно в 19:00 МСК
/scans [N] — история проверок
/articles [N] — какие публикации бот скачал и как обработал
/cost — стоимость LLM: сегодня, за месяц, экономия кэша
/list — список активов
/find ТЕКСТ — найти актив по имени, тикеру или алиасу
/add CODE | TYPE | NAME | АЛИАСЫ — добавить актив (алиасы через запятую, необязательны)
/alias АКТИВ | АЛИАС — добавить идентификатор к активу
/addsource АКТИВ | URL — добавить источник
/sources [АКТИВ] — состояние источников, активы без покрытия
/disablesource ID — отключить мёртвый источник
/remove АКТИВ — удалить актив
/interval MINUTES — изменить период
/pause — остановить автоматические проверки
/resume — возобновить
/digest — сводка событий за 24 часа
/events [АКТИВ] [N] — история событий (например /events SPYF 50)
/events test — тестовые события
/event ID или /event АКТИВ — полный отчёт о событии
/status — состояние сервиса
/id — показать ваш Telegram ID

Доступ сотрудников:
/adduser ID [имя] — подписать на уведомления (только чтение)
/removeuser ID — отписать
/users — список подписчиков
/testbroadcast — тестовая рассылка всем с отчётом о доставке
/exportusers — резервная копия всех получателей (TG + email) одной командой
/importusers … — восстановить TG-подписчиков из резервной копии
/health — здоровье сервиса: источники, LLM, планировщик, доставка
/rehab — вернуть отключённые источники в мониторинг, починить сломанные URL

Email-дублирование (события + дневное саммари):
/addemail адрес [имя] — добавить почту в рассылку
/removeemail адрес — убрать
/emails — список адресов и статус SMTP
/testemail — тестовое письмо

Диагностика и сопоставление:
/debugscan АКТИВ — поэтапная проверка одного актива
/matchtest АКТИВ | ТЕКСТ — проверить сопоставление текста
/unmatched [АКТИВ] — публикации, не прошедшие порог сопоставления
/match ID АКТИВ — вручную привязать публикацию
/ignorepublication ID — пометить нерелевантной
/rematch [АКТИВ|all] [дней] — пересопоставить кэш локально, без LLM
/queue — очередь публикаций, ждущих Gemini
/processqueue — обработать очередь через Gemini сейчас
/setid АКТИВ | ПОЛЕ | ЗНАЧЕНИЕ — ISIN/CUSIP/CIK/LEI/тикеры/эмитент
/dbstats — состояние базы данных
/pipetest АКТИВ [mock] — сквозной тест конвейера на демо-публикации
/testevent АКТИВ — тестовое событие (проверка записи и дедупликации)
/testreport АКТИВ — демонстрационный полный отчёт (без записи в базу)
/deletetest — удалить все тестовые события
/deleteevent ID — удалить событие

Везде, где написано АКТИВ, подойдёт любой известный идентификатор:
канонический код, тикер, алиас или имя (можно часть имени).
Типы: ETF, SHARE, ADR, ASSET.
Пример: <code>/add TSM | ADR | Taiwan Semiconductor ADR | TSMC, 2330.TW</code>

Уведомление о событии выглядит так:

🚨 <b>Дробление акций</b>
<code>SPYF</code> · SPDR S&amp;P 500 ETF Trust
🔴 Критическая

Эмитент объявил дробление паёв 1:5, дата вступления в силу — 21 июля 2026.

⚠️ Проверить необходимость корректировки параметров контракта.

Полный отчёт с датами, параметрами, влиянием и источниками — по кнопке «Подробнее» или команде /event ID."""


VIEWER_HELP = """<b>Мониторинг корпоративных событий</b>

Вы подписаны на уведомления о корпоративных событиях.

Доступные команды:
/digest — сводка событий за 24 часа
/events [АКТИВ] [N] — история событий
/event ID или /event АКТИВ — полный отчёт о событии
/reports on|off — сводка после каждой проверки или только при новых событиях
/id — показать ваш Telegram ID

Управление активами и настройками — у администратора."""


def authorized(update: Update) -> bool:
    # ADMIN_TELEGRAM_ID=0 — режим первоначальной настройки: доступна только команда /id.
    return bool(settings.admin_telegram_id
                and update.effective_user
                and update.effective_user.id == settings.admin_telegram_id)


def is_viewer(update: Update) -> bool:
    """Наблюдатель: администратор либо подписчик из таблицы subscribers."""
    if authorized(update):
        return True
    return bool(update.effective_user and db.is_subscriber(update.effective_user.id))


async def guard(update: Update) -> bool:
    if not authorized(update):
        if settings.admin_telegram_id == 0:
            await update.effective_message.reply_text(
                "Бот в режиме настройки: отправьте /id, запишите номер в ADMIN_TELEGRAM_ID в .env и перезапустите.")
        else:
            await update.effective_message.reply_text("Нет доступа.")
        return False
    return True


async def viewer_guard(update: Update) -> bool:
    """Доступ для чтения: администратор и подписчики уведомлений."""
    if not is_viewer(update):
        await update.effective_message.reply_text(
            "Нет доступа. Отправьте /id и попросите администратора добавить вас командой /adduser.")
        return False
    return True

async def reply_html(message, text: str, reply_markup=None) -> None:
    """Отправляет HTML-сообщение: длинный текст режется на части (теги и ссылки
    не разрываются), при ошибке HTML-разметки — fallback на обычный текст."""
    chunks = formatters.split_html(text)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
        except telegram.error.BadRequest:
            log.warning("HTML-отправка не удалась, отправляю обычным текстом: %.120s", chunk)
            await message.reply_text(formatters.strip_html(chunk), disable_web_page_preview=True, reply_markup=markup)


async def send_html_message(bot, chat_id: int, text: str, reply_markup=None,
                            silent: bool = False):
    """То же, что reply_html, но через bot.send_message (для плановых уведомлений).
    silent=True — сообщение приходит без звука (для рутинных отчётов без событий).
    Возвращает первое отправленное сообщение (для закрепления)."""
    chunks = formatters.split_html(text)
    first_message = None
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            message = await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML",
                                             disable_web_page_preview=True, reply_markup=markup,
                                             disable_notification=silent)
        except telegram.error.BadRequest:
            log.warning("HTML-отправка не удалась, отправляю обычным текстом: %.120s", chunk)
            message = await bot.send_message(chat_id=chat_id, text=formatters.strip_html(chunk),
                                             disable_web_page_preview=True, reply_markup=markup,
                                             disable_notification=silent)
        if first_message is None:
            first_message = message
    return first_message


async def resolve_or_reply(update: Update, text: str) -> dict | None:
    """Находит актив по любому идентификатору; при неоднозначности или отсутствии отвечает пользователю."""
    asset, candidates = db.resolve_asset(text)
    if asset:
        return asset
    if candidates:
        lines = [f"{html.escape(a['code'])} — {html.escape(a['name'])}" for a in candidates]
        await update.effective_message.reply_text(
            "Найдено несколько активов, уточните запрос или укажите код:\n" + "\n".join(lines))
    else:
        await update.effective_message.reply_text(
            f"Актив «{text}» не найден. /list — все активы, /find ТЕКСТ — поиск.")
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        await update.effective_message.reply_text(HELP, parse_mode="HTML")
    elif is_viewer(update):
        await update.effective_message.reply_text(VIEWER_HELP, parse_mode="HTML")
    else:
        await guard(update)  # выдаст корректное сообщение об отказе/режиме настройки

async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Ваш Telegram ID: <code>{update.effective_user.id}</code>", parse_mode="HTML")

async def list_assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    assets=db.list_assets()
    lines=[f"<b>{html.escape(a['code'])}</b> — {html.escape(a['name'])} [{a['instrument_type']}] · источников: {len(a['sources'])}" for a in assets]
    for i in range(0, len(lines), 30):
        await update.effective_message.reply_text("\n".join(lines[i:i+30]), parse_mode="HTML", disable_web_page_preview=True)

async def add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    raw=" ".join(context.args)
    parts=[p.strip() for p in raw.split("|")]
    if len(parts) not in (3, 4) or parts[1].upper() not in {"ETF","SHARE","ADR","ASSET"}:
        await update.effective_message.reply_text("Формат: /add CODE | TYPE | NAME | АЛИАСЫ (последнее необязательно)")
        return
    aliases = [a.strip() for a in parts[3].split(",") if a.strip()] if len(parts) == 4 else []
    db.upsert_asset(parts[0], parts[2], parts[1], aliases)
    alias_note = f" Алиасов добавлено: {len(aliases)}." if aliases else ""
    await update.effective_message.reply_text(
        f"Добавлен актив {parts[0].upper()}.{alias_note} Теперь добавьте источник: /addsource {parts[0]} | URL")

async def add_alias_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    raw=" ".join(context.args)
    parts=[p.strip() for p in raw.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await update.effective_message.reply_text("Формат: /alias АКТИВ | НОВЫЙ_АЛИАС")
        return
    asset = await resolve_or_reply(update, parts[0])
    if not asset: return
    if db.add_alias(asset["code"], parts[1]):
        await update.effective_message.reply_text(
            f"Алиас «{parts[1]}» добавлен к активу {asset['code']} — {asset['name']}.")
    else:
        await update.effective_message.reply_text(f"Такой идентификатор у {asset['code']} уже есть.")

async def find_assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    text=" ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Формат: /find ТЕКСТ (имя, тикер или алиас)")
        return
    asset, candidates = db.resolve_asset(text)
    matches = [asset] if asset else candidates
    if not matches:
        await update.effective_message.reply_text(f"По запросу «{text}» ничего не найдено.")
        return
    lines=[]
    for a in matches:
        aliases = ", ".join(a["aliases"]) if a["aliases"] else "нет"
        lines.append(f"<b>{html.escape(a['code'])}</b> — {html.escape(a['name'])} [{a['instrument_type']}]\n"
                     f"  алиасы: {html.escape(aliases)} · источников: {len(a['sources'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    raw=" ".join(context.args)
    parts=[p.strip() for p in raw.split("|", 1)]
    if len(parts)!=2 or not parts[1].startswith("http"):
        await update.effective_message.reply_text("Формат: /addsource АКТИВ | https://...")
        return
    asset = await resolve_or_reply(update, parts[0])
    if not asset: return
    db.add_source(asset["code"], parts[1])
    await update.effective_message.reply_text(f"Источник добавлен к {asset['code']} — {asset['name']}.")

async def remove_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    text=" ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Формат: /remove АКТИВ")
        return
    asset = await resolve_or_reply(update, text)
    if not asset: return
    db.remove_asset(asset["code"])
    await update.effective_message.reply_text(f"Актив {asset['code']} — {asset['name']} удалён.")

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        minutes=int(context.args[0])
        if minutes < 15: raise ValueError
    except (ValueError, IndexError):
        await update.effective_message.reply_text("Укажите число минут не меньше 15: /interval 60")
        return
    db.set_setting("interval_minutes", str(minutes))
    job=context.application.job_queue.get_jobs_by_name("scheduled_scan")
    for j in job: j.schedule_removal()
    context.application.job_queue.run_repeating(context.application.bot_data["scheduled_scan"], interval=minutes*60, first=minutes*60, name="scheduled_scan")
    await update.effective_message.reply_text(f"Период изменён: {minutes} мин.")

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    db.set_setting("paused", "1")
    await update.effective_message.reply_text("Автоматический мониторинг приостановлен.")

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    db.set_setting("paused", "0")
    await update.effective_message.reply_text("Автоматический мониторинг возобновлён.")

async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await viewer_guard(update): return
    events = db.events_since_hours(24)
    await reply_html(update.effective_message, formatters.format_digest(events, last_run=db.last_ok_run()))


async def events_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/events · /events SPYF · /events SPYF 50 · /events 50 · /events test.
    Фильтр по активу выполняется в SQL; тестовые события скрыты из рабочего списка."""
    if not await viewer_guard(update): return
    args = list(context.args)
    limit = 20
    if args and args[-1].isdigit():
        limit = min(int(args.pop()), 100)
    asset_code, test_only = None, False
    if args:
        if args[0].lower() == "test":
            test_only = True
        else:
            asset = await resolve_or_reply(update, " ".join(args))
            if not asset: return
            asset_code = asset["code"]
    events = db.recent_events(limit=limit, asset_code=asset_code, test_only=test_only)
    if not events:
        if asset_code:
            await update.effective_message.reply_text(
                f"По активу {asset_code} сохранённых событий пока нет.")
        elif test_only:
            await update.effective_message.reply_text("Тестовых событий нет.")
        else:
            await update.effective_message.reply_text("Сохранённых событий пока нет.")
        return
    await reply_html(update.effective_message, formatters.format_events_list(events))


async def show_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/event 17 — по ID; /event SPYF (или SPY, алиас) — последнее рабочее событие актива."""
    if not await viewer_guard(update): return
    arg = " ".join(context.args).strip()
    if not arg:
        await update.effective_message.reply_text("Использование: /event ID или /event TICKER")
        return
    if arg.isdigit():
        e = db.get_event(int(arg))
        if not e:
            await update.effective_message.reply_text(f"Событие #{arg} не найдено. История: /events")
            return
    else:
        found, candidates = db.resolve_asset(arg)
        if not found:
            if candidates:
                lines = [f"{html.escape(a['code'])} — {html.escape(a['name'])}" for a in candidates]
                await update.effective_message.reply_text(
                    "Найдено несколько активов, уточните:\n" + "\n".join(lines))
            else:
                await update.effective_message.reply_text("Использование: /event ID или /event TICKER")
            return
        e = db.last_event_for_asset(found["code"])
        if not e:
            await update.effective_message.reply_text(
                f"По активу {found['code']} сохранённых событий пока нет.")
            return
    await reply_html(update.effective_message, formatters.format_event_full(e, db.get_asset(e["asset_code"])))


async def delete_event_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        event_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Формат: /deleteevent ID")
        return
    e = db.get_event(event_id)
    if not e:
        await update.effective_message.reply_text(f"Событие #{event_id} не найдено.")
        return
    db.delete_event(event_id)
    await update.effective_message.reply_text(
        f"Событие #{event_id} ({e['asset_code']} — {(e['title'] or '')[:60]}) удалено.")


async def delete_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    n = db.delete_test_events()
    await update.effective_message.reply_text(
        f"Удалено тестовых событий: {n}." if n else "Тестовых событий нет.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    assets=db.list_assets(enabled_only=True)
    sources=db.list_source_defs(enabled_only=True)
    paused=db.get_setting("paused","0") == "1"
    interval=db.get_setting("interval_minutes", str(settings.scan_interval_minutes))
    await update.effective_message.reply_text(
        f"Статус: {'пауза' if paused else 'работает'}\nАктивов: {len(assets)}\nИсточников: {len(sources)}\n"
        f"Период: {interval} мин.\nМодель: {settings.llm_provider}/{settings.model}")


async def cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await reply_html(update.effective_message,
                     formatters.format_cost_report(db.cost_stats(), settings.model))


SCAN_REPORTS_KEY = "scan_reports"

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]{2,}$")


async def add_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addemail адрес [имя] — один получатель с именем;
    /addemail a@x.ru b@y.ru, c@z.ru — сразу несколько (через пробел или запятую)."""
    if not await guard(update): return
    from app import emailer
    tokens = [t for raw in context.args for t in raw.replace(",", " ").split() if t]
    emails = [t.lower() for t in tokens if _EMAIL_RE.match(t)]
    non_emails = [t for t in tokens if not _EMAIL_RE.match(t)]
    if not emails:
        await update.effective_message.reply_text(
            "Формат: /addemail user@example.com [имя]\n"
            "или пачкой: /addemail a@x.ru b@y.ru c@z.ru")
        return
    note = "" if emailer.is_configured() else (
        "\n⚠️ Почта не настроена: задайте GMAIL_* переменные (см. README) — "
        "иначе письма не отправятся.")
    if len(emails) == 1:
        name = " ".join(non_emails) or None
        added = db.add_email_recipient(emails[0], name)
        await update.effective_message.reply_text(
            (f"Адрес {emails[0]} добавлен: события и дневное саммари будут дублироваться на почту."
             if added else f"Адрес {emails[0]} уже в списке.") + note)
        return
    added, skipped = [], []
    for email_addr in emails:
        (added if db.add_email_recipient(email_addr) else skipped).append(email_addr)
    lines = [f"Добавлено адресов: {len(added)}"] + [f"• {e}" for e in added]
    if skipped:
        lines.append(f"Уже были в списке: {', '.join(skipped)}")
    if non_emails:
        lines.append(f"Не похоже на адреса (пропущено): {', '.join(non_emails[:5])}")
    lines.append("Проверить доставку всем: /testemail")
    await update.effective_message.reply_text("\n".join(lines) + note)


async def remove_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Формат: /removeemail user@example.com")
        return
    if db.remove_email_recipient(context.args[0]):
        await update.effective_message.reply_text(f"Адрес {context.args[0].lower()} удалён из рассылки.")
    else:
        await update.effective_message.reply_text("Такого адреса нет. Список: /emails")


async def emails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    from app import emailer
    recipients = db.list_email_recipients()
    if not recipients:
        await update.effective_message.reply_text(
            "Email-рассылка пуста. Добавить: /addemail user@example.com")
        return
    lines = [f"Email-рассылка ({len(recipients)}) — события и дневное саммари:"]
    for r in recipients:
        lines.append(f"• {r['email']}" + (f" — {r['name']}" if r["name"] else ""))
    lines.append("SMTP: " + ("настроен ✅" if emailer.is_configured() else "НЕ настроен ⚠️ (см. .env)"))
    await update.effective_message.reply_text("\n".join(lines))


async def export_recipients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/exportusers — все получатели одной строкой-командой для восстановления."""
    if not await guard(update): return
    subs = db.list_subscribers()
    mails = db.list_email_recipients()
    lines = ["Резервная копия получателей. Сохраните это сообщение: если база когда-нибудь "
             "окажется пустой, отправьте команды ниже боту.", ""]
    if subs:
        lines.append("/importusers " + " ".join(f"{s['chat_id']}:{(s['name'] or '').replace(' ', '_')}"
                                                if s['name'] else str(s['chat_id']) for s in subs))
    if mails:
        lines.append("/addemail " + " ".join(m["email"] for m in mails))
    if not subs and not mails:
        lines.append("Получателей нет.")
    await update.effective_message.reply_text("\n".join(lines))


async def import_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/importusers 123:Имя 456 789:Иван_Петров — восстановить TG-подписчиков пачкой."""
    if not await guard(update): return
    added, skipped, bad = [], [], []
    for token in context.args:
        chat_id, _, name = token.partition(":")
        if not chat_id.lstrip("-").isdigit():
            bad.append(token)
            continue
        ok = db.add_subscriber(int(chat_id), name.replace("_", " ") or None)
        (added if ok else skipped).append(chat_id)
    lines = [f"Подписчиков добавлено: {len(added)}"]
    if skipped: lines.append(f"Уже были: {', '.join(skipped)}")
    if bad: lines.append(f"Пропущено (не ID): {', '.join(bad[:5])}")
    if not context.args:
        lines = ["Формат: /importusers 123456:Имя 789012 (данные из /exportusers)"]
    await update.effective_message.reply_text("\n".join(lines))


async def test_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testemail — тестовое письмо всем адресатам."""
    if not await guard(update): return
    from app import emailer
    if not emailer.is_configured():
        await update.effective_message.reply_text(
            "Почта не настроена. На Railway используйте SendGrid (SMTP там заблокирован): "
            "переменные SENDGRID_API_KEY + EMAIL_FROM (верифицированный отправитель). "
            "Локально можно SMTP: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD.")
        return
    recipients = db.list_email_recipients()
    if not recipients:
        await update.effective_message.reply_text("Список адресов пуст: /addemail user@example.com")
        return
    ok = await emailer.send_email(
        "🔧 Тест email-рассылки Corporate Events Monitor",
        "<b>Тестовое письмо</b>\nЕсли вы его видите — дублирование уведомлений на почту работает.")
    await update.effective_message.reply_text(
        f"✅ Письмо отправлено на {len(recipients)} адрес(а)." if ok
        else "❌ Не удалось отправить: проверьте SMTP-настройки и логи.")


def notification_recipients() -> list[int]:
    """Все получатели рассылок: администратор + подписчики, без дублей."""
    recipients = []
    if settings.admin_telegram_id:
        recipients.append(settings.admin_telegram_id)
    for s in db.list_subscribers():
        if s["chat_id"] not in recipients:
            recipients.append(s["chat_id"])
    return recipients


_ON_ARGS, _OFF_ARGS = ("on", "вкл", "1"), ("off", "выкл", "0")


def _personal_reports_key(chat_id: int) -> str:
    return f"{SCAN_REPORTS_KEY}:{chat_id}"


def scan_reports_default() -> bool:
    """Настройка по умолчанию (для тех, кто личную не выбирал) — задаёт администратор."""
    return db.get_setting(SCAN_REPORTS_KEY, "1") == "1"


def scan_reports_personal(chat_id: int) -> bool | None:
    """Личная настройка получателя: True/False или None, если не выбиралась."""
    value = db.get_setting(_personal_reports_key(chat_id), "")
    return None if value == "" else value == "1"


def scan_reports_enabled(chat_id: int) -> bool:
    """Получать ли сводку проверки без новых событий: личная настройка, иначе общая."""
    personal = scan_reports_personal(chat_id)
    return scan_reports_default() if personal is None else personal


def scan_report_recipients(has_events: bool) -> list[int]:
    """Кому отправлять сводку проверки. С новыми событиями — всем получателям;
    без событий — только тем, у кого отчёты о проверках включены (/reports)."""
    return [chat_id for chat_id in notification_recipients()
            if has_events or scan_reports_enabled(chat_id)]


async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reports on|off — личная настройка: присылать ли сводку проверки, в которой
    новых событий нет. Уведомления о событиях приходят всегда.
    /reports all on|off — настройка по умолчанию для всех (только администратор)."""
    if not await viewer_guard(update): return
    args = [a.lower() for a in (context.args or [])]
    chat_id = update.effective_user.id
    if args and args[0] in ("all", "все", "всем"):
        if not await guard(update): return
        arg = args[1] if len(args) > 1 else ""
        if arg not in _ON_ARGS + _OFF_ARGS:
            await update.effective_message.reply_text(
                f"По умолчанию отчёты о проверках без событий: "
                f"{'включены' if scan_reports_default() else 'выключены'}.\n"
                "Изменить: /reports all on | /reports all off. "
                "Личный выбор получателей (/reports on|off) сильнее настройки по умолчанию.")
            return
        db.set_setting(SCAN_REPORTS_KEY, "1" if arg in _ON_ARGS else "0")
        await update.effective_message.reply_text(
            f"✅ По умолчанию отчёты о проверках без событий "
            f"{'включены' if arg in _ON_ARGS else 'выключены'}. "
            "Это касается получателей, которые не выбирали сами через /reports on|off.")
        return
    arg = args[0] if args else ""
    if arg in _ON_ARGS:
        db.set_setting(_personal_reports_key(chat_id), "1")
        await update.effective_message.reply_text(
            "✅ Отчёты о проверках включены: сводка будет приходить после каждой проверки, "
            "даже если новых событий нет (без звука). Выключить: /reports off")
    elif arg in _OFF_ARGS:
        db.set_setting(_personal_reports_key(chat_id), "0")
        await update.effective_message.reply_text(
            "🔕 Отчёты о проверках без событий выключены: сообщения будут приходить, "
            "только когда найдены новые события. Включить: /reports on")
    else:
        personal = scan_reports_personal(chat_id)
        enabled = scan_reports_enabled(chat_id)
        origin = "ваш выбор" if personal is not None else "настройка по умолчанию"
        text = (f"Отчёты о проверках без новых событий: {'включены' if enabled else 'выключены'} "
                f"({origin}).\n"
                "/reports on — сводка после каждой проверки;\n"
                "/reports off — сообщения только при новых событиях.\n"
                "Уведомления о самих событиях приходят всегда.")
        if authorized(update):
            text += ("\n\nПо умолчанию для всех: "
                     f"{'включены' if scan_reports_default() else 'выключены'} — /reports all on|off."
                     "\nИстория проверок: /scans")
        await update.effective_message.reply_text(text)


async def scans_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scans [N] — история проверок."""
    if not await guard(update): return
    limit = 15
    if context.args and context.args[0].isdigit():
        limit = min(int(context.args[0]), 50)
    await reply_html(update.effective_message, formatters.format_scan_history(db.list_scan_runs(limit)))


async def articles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/articles [N] [источник] — последние обработанные публикации.
    Примеры: /articles 30 · /articles SEC · /articles 20 tavily.
    Повторные публикации из кэша не «всплывают»: показывается только новое."""
    if not await guard(update): return
    limit, source_like = 15, None
    args = list(context.args)
    if args and args[0].isdigit():
        limit = min(int(args.pop(0)), 50)
    if args:
        source_like = " ".join(args)
    rows = db.recent_articles(limit, source_like=source_like)
    by_source = db.articles_by_source(hours=24)
    header = ""
    if by_source:
        top = " · ".join(f"{s}: {n}" for s, n in by_source[:6])
        header = f"Новых за 24 ч по источникам: {formatters.esc(top)}\n\n"
    await reply_html(update.effective_message, header + formatters.format_articles(rows))


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/adduser ID [имя] — подписать сотрудника на уведомления (только чтение)."""
    if not await guard(update): return
    args = list(context.args)
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "Формат: /adduser TELEGRAM_ID [имя]\n"
            "Сотрудник может узнать свой ID, отправив боту /id.")
        return
    chat_id = int(args[0])
    name = " ".join(args[1:]) or None
    if db.add_subscriber(chat_id, name):
        await update.effective_message.reply_text(
            f"Пользователь {chat_id}{f' ({name})' if name else ''} подписан на уведомления.\n"
            "Ему доступны: уведомления о событиях, /digest, /events, /event. "
            "Управляющие команды остаются только у вас.")
    else:
        await update.effective_message.reply_text(f"Пользователь {chat_id} уже подписан.")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        chat_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Формат: /removeuser TELEGRAM_ID")
        return
    if db.remove_subscriber(chat_id):
        await update.effective_message.reply_text(f"Пользователь {chat_id} отписан от уведомлений.")
    else:
        await update.effective_message.reply_text(f"Пользователь {chat_id} не найден. Список: /users")


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    subs = db.list_subscribers()
    if not subs:
        await update.effective_message.reply_text(
            "Подписчиков нет. Добавить: /adduser TELEGRAM_ID [имя]")
        return
    lines = [f"Подписчики уведомлений ({len(subs)}):"]
    for s in subs:
        lines.append(f"• {s['chat_id']}{f' — {s['name']}' if s['name'] else ''} (с {s['added_at'][:10]})")
    await update.effective_message.reply_text("\n".join(lines))


async def sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sources [АКТИВ] — источники и их состояние; без аргумента — сводка по всем."""
    if not await guard(update): return
    from app import health
    all_sources = db.list_source_defs(enabled_only=False)
    if context.args:
        asset = await resolve_or_reply(update, " ".join(context.args))
        if not asset: return
        own = [s for s in all_sources if asset["code"] in s["subscribers"]]
        lines = [f"Источники {asset['code']} ({len(own)}):"]
        for s in own:
            state = ("⛔ отключён" if not s["enabled"] else
                     f"❌ сбоев подряд: {s.get('fail_streak') or 0}" if (s.get("fail_streak") or 0) >= health.SOURCE_FAIL_THRESHOLD
                     else "✅")
            lines.append(f"{state} [{s['type']}] {s['name'][:40]}\n   {s['url'][:90]}")
        await update.effective_message.reply_text("\n".join(lines), disable_web_page_preview=True)
        return
    enabled = [s for s in all_sources if s["enabled"]]
    failing = [s for s in enabled if (s.get("fail_streak") or 0) >= health.SOURCE_FAIL_THRESHOLD]
    by_type = {}
    for s in enabled:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    # Активы без единого рабочего источника — главный риск пропуска.
    uncovered = []
    for a in db.list_assets(enabled_only=True):
        working = [s for s in a["sources"] if (s.get("fail_streak") or 0) < health.SOURCE_FAIL_THRESHOLD]
        if not working:
            uncovered.append(a["code"])
    lines = [f"Источников: {len(enabled)} активных, {len(all_sources) - len(enabled)} отключено",
             "По типам: " + ", ".join(f"{t}: {n}" for t, n in sorted(by_type.items())),
             f"Стабильно недоступны: {len(failing)}",
             "Активы без рабочих источников: " + (", ".join(uncovered) if uncovered else "нет ✅"),
             "", "Подробно по активу: /sources АКТИВ · отключить: /disablesource ID"]
    await update.effective_message.reply_text("\n".join(lines))


async def disable_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        source_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Формат: /disablesource ID (ID виден в /sources АКТИВ)")
        return
    with db.connect() as conn:
        n = conn.execute("UPDATE source_defs SET enabled=0 WHERE id=?", (source_id,)).rowcount
    await update.effective_message.reply_text("Источник отключён." if n else "Источник не найден.")


async def dbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    s = db.db_stats()
    last = s["last_run"]
    if last:
        last_text = (f"{last['started_at']} UTC → {last['finished_at'] or 'выполняется'} "
                     f"({last['status']}, новых событий: {last['new_events']})")
    else:
        last_text = "ещё не выполнялся"
    await update.effective_message.reply_text(
        "Состояние базы данных:\n"
        f"Путь: {s['path']}\n"
        f"{'Подключение' if s['backend'] == 'postgresql' else 'Файл существует'}: "
        f"{'да' if s['exists'] else 'нет'}\n"
        f"Размер: {s['size_bytes'] / 1024:.1f} КБ\n"
        f"Активов: {s['assets']}\n"
        f"Источников: {s['sources']}\n"
        f"Поисковых промптов: {s['event_prompts']}\n"
        f"Событий: {s['events']}\n"
        f"Запусков сканирования: {s['scan_runs']}\n"
        f"Последний запуск: {last_text}")


async def test_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    from app.monitor import _normalize_event
    raw = " ".join(context.args).strip()
    if not raw:
        await update.effective_message.reply_text("Формат: /testevent АКТИВ, например /testevent SPYF")
        return
    resolved, _ = db.resolve_asset(raw)
    if not resolved:
        await update.effective_message.reply_text(
            f"Актив {raw.upper()} не найден. Используйте тикер из /list.")
        return
    code = resolved["code"]
    # Расширенное искусственное событие: заполнены все блоки отчёта.
    event = _normalize_event(code, {
        "event_type": "Тестовое событие",
        "severity": "CRITICAL",
        "title_ru": f"Тестовое дробление для {code}",
        "summary_ru": "Искусственное событие для проверки конвейера: запись в базу, форматирование, "
                      "отправка и дедупликация. Эмитент условно объявил дробление бумаг 1:5. "
                      "Информация не является реальным корпоративным действием.",
        "impact_ru": "Реального влияния нет. В боевом событии здесь описывается эффект на цену, "
                     "количество бумаг и параметры производного инструмента.",
        "action_required": True,
        "recommended_action_ru": "Проверить необходимость корректировки параметров контракта.",
        "event_date": str(date.today()),
        "publication_date": str(date.today()),
        "effective_date": str(date.today()),
        "status": "confirmed",
        "underlying_ticker": "TEST",
        "confidence": 0.94,
        "key_parameters": [
            {"label": "Коэффициент дробления", "value": "1:5"},
            {"label": "Ex-date", "value": formatters.format_date_ru(str(date.today()))},
        ],
        "sources": [
            {"name": "Тестовый официальный источник", "url": "https://example.com/testevent"},
            {"name": "Тестовое дополнительное подтверждение", "url": "https://example.com/testevent-2"},
        ],
    })
    event["is_test"] = 1  # тестовые события не попадают в рабочие /events, /digest и рассылку
    if db.event_exists(event["fingerprint"]):
        await update.effective_message.reply_text(
            f"Дубликат определён корректно: событие с fingerprint {event['fingerprint'][:16]}… уже в базе, "
            "повторная отправка заблокирована.")
        return
    row_id = db.save_event(event)
    if row_id is None:
        await update.effective_message.reply_text("Ошибка: событие не записалось в базу. Подробности в логах.")
        return
    event["id"] = row_id
    asset = db.get_asset(code)
    await reply_html(update.effective_message,
                     formatters.format_event_short(event, asset),
                     reply_markup=formatters.event_details_button(row_id))
    db.mark_sent(event["fingerprint"])
    total = db.db_stats()["events"]
    await update.effective_message.reply_text(
        f"Записано: id={row_id}, fingerprint={event['fingerprint'][:16]}…, sent_at проставлен.\n"
        f"Всего событий в базе: {total}.\n"
        f"Полный отчёт: /event {row_id} или кнопка «Подробнее».\n"
        "Повторите /testevent с тем же тикером — должен определиться дубликат.")


def _format_match_result(r: matcher.MatchResult) -> str:
    return (f"Актив: {r.asset_code}\n"
            f"Score: {r.score:.2f} · уверенность: {r.confidence}\n"
            f"matched_by: {', '.join(r.matched_by) or '—'}\n"
            f"matched_terms: {', '.join(r.matched_terms) or '—'}\n"
            + (f"rejected_terms: {', '.join(r.rejected_terms)}\n" if r.rejected_terms else "")
            + f"Объяснение: {r.explanation}")


async def match_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/matchtest TICKER | TEXT — проверить сопоставление текста с активом."""
    if not await guard(update): return
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await update.effective_message.reply_text(
            "Формат: /matchtest АКТИВ | ТЕКСТ\n"
            "Пример: /matchtest SPYF | SPDR S&P 500 ETF Trust declares quarterly distribution")
        return
    asset = await resolve_or_reply(update, parts[0])
    if not asset: return
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True), db.list_matcher_rules())
    pub = {"title": parts[1], "text": "", "subscribers": []}
    target = next((p for p in profiles if p.code == asset["code"]), None)
    result = matcher.match_one(pub, target)
    lines = [f"Тест сопоставления с {asset['code']}:", _format_match_result(result)]
    all_results = matcher.match_publication(pub, profiles)
    best = all_results[0] if all_results else None
    if best and best.asset_code != asset["code"]:
        lines.append(f"\nЛучшее совпадение по всем активам: {best.asset_code} "
                     f"({best.score:.2f}, {best.confidence})")
    await update.effective_message.reply_text("\n".join(lines))


async def unmatched_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unmatched [АКТИВ] — очередь публикаций, не прошедших порог сопоставления."""
    if not await guard(update): return
    asset_code = None
    if context.args:
        asset = await resolve_or_reply(update, " ".join(context.args))
        if not asset: return
        asset_code = asset["code"]
    rows = db.list_unmatched(limit=20, asset_code=asset_code)
    if not rows:
        await update.effective_message.reply_text("Очередь unmatched пуста.")
        return
    blocks = []
    for r in rows:
        best = f"{r['best_asset']} ({(r['best_score'] or 0):.2f})" if r["best_asset"] else "нет кандидата"
        blocks.append(f"#{r['id']} · {(r['title'] or r['url'] or '')[:90]}\n"
                      f"  источник: {r['source'] or '—'} · лучший: {best} · причина: {r['reason']}\n"
                      f"  /match {r['id']} КОД — привязать · /ignorepublication {r['id']} — игнорировать")
    await reply_html(update.effective_message, formatters.esc("\n\n".join(blocks)))


async def match_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/match ID АКТИВ — вручную привязать публикацию из очереди unmatched к активу."""
    if not await guard(update): return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Формат: /match ID АКТИВ")
        return
    try:
        unmatched_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом: /match 17 SPYF")
        return
    row = db.get_unmatched(unmatched_id)
    if not row:
        await update.effective_message.reply_text(f"Запись #{unmatched_id} не найдена. /unmatched — список.")
        return
    asset = await resolve_or_reply(update, " ".join(context.args[1:]))
    if not asset: return
    db.remember_article(row["url"] or "", row["url_hash"], None, row.get("published_at"),
                        row.get("source"), "pending", status="matched",
                        best_asset=asset["code"], best_score=1.0, pending_llm=True)
    db.review_unmatched(unmatched_id, "matched")
    reply = [f"Публикация #{unmatched_id} привязана к {asset['code']}. "
             "Она будет проанализирована при следующем /scan."]
    # Предложить сохранить точную фразу как алиас (короткие/общие слова не предлагаем).
    phrase = _suggest_alias_phrase(row.get("title") or "", asset)
    if phrase:
        reply.append(f"Похоже, актив упомянут как «{phrase}». "
                     f"Сохранить как алиас: /alias {asset['code']} | {phrase}")
    await update.effective_message.reply_text("\n".join(reply))


def _suggest_alias_phrase(title: str, asset: dict) -> str | None:
    """Самая длинная капитализированная фраза заголовка — кандидат в алиасы."""
    known = {a.lower() for a in asset.get("aliases", [])} | {asset["name"].lower()}
    phrases = re.findall(r"(?:[A-Z][\w&.\-]*)(?:\s+(?:[A-Z][\w&.\-]*|of|the|and))+", title)
    phrases = [p.strip() for p in phrases
               if len(p.strip()) >= 8 and p.strip().lower() not in known
               and p.strip().lower() not in matcher.GENERIC_WORDS]
    return max(phrases, key=len) if phrases else None


async def ignore_publication(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ignorepublication ID — пометить публикацию нерелевантной навсегда."""
    if not await guard(update): return
    try:
        unmatched_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Формат: /ignorepublication ID")
        return
    row = db.get_unmatched(unmatched_id)
    if not row:
        await update.effective_message.reply_text(f"Запись #{unmatched_id} не найдена.")
        return
    db.set_article_status(row["url_hash"], "ignored", "ignored_by_admin")
    db.review_unmatched(unmatched_id, "ignored")
    await update.effective_message.reply_text(f"Публикация #{unmatched_id} помечена нерелевантной.")


async def set_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setid АКТИВ | ПОЛЕ | ЗНАЧЕНИЕ — точные идентификаторы вносит администратор."""
    if not await guard(update): return
    parts = [p.strip() for p in " ".join(context.args).split("|")]
    fields = ", ".join(sorted(db.ASSET_ID_FIELDS))
    if len(parts) != 3 or parts[1].lower() not in db.ASSET_ID_FIELDS:
        await update.effective_message.reply_text(f"Формат: /setid АКТИВ | ПОЛЕ | ЗНАЧЕНИЕ\nПоля: {fields}")
        return
    asset = await resolve_or_reply(update, parts[0])
    if not asset: return
    db.set_asset_field(asset["code"], parts[1].lower(), parts[2] or None)
    await update.effective_message.reply_text(
        f"У {asset['code']} установлено {parts[1].lower()} = {parts[2]}. "
        "Действует со следующего сканирования.")


async def rematch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rematch [АКТИВ|all] [days] — локальное пересопоставление сохранённого корпуса
    публикаций: без сети, без LLM. Если указан актив, matcher применяется только к нему.
    Новые high/medium получают pending_llm=1 и обрабатываются следующим /scan
    или командой /processqueue. События здесь не создаются."""
    if not await guard(update): return
    args = list(context.args)
    days = 7
    if args and args[-1].isdigit():
        days = int(args.pop())
    target = None
    if args and args[0].lower() != "all":
        target = await resolve_or_reply(update, " ".join(args))
        if not target: return
    rows = db.publications_for_rematch(days)
    corpus = db.rematch_corpus_stats(days)
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True), db.list_matcher_rules())
    target_profile = next((p for p in profiles if target and p.code == target["code"]), None)

    high = medium = low = unrelated = keyword_pass = queued = 0
    for row in rows:
        pub = {"url": row["url"], "title": row["title"] or "", "text": row["text"] or "",
               "subscribers": []}
        if not matcher.keyword_gate(pub, profiles):
            continue  # статус не трогаем: keyword-фильтр не менялся
        keyword_pass += 1
        if target_profile is not None:
            # Тикер-специфичный режим: matcher применяется ТОЛЬКО к целевому активу.
            result = matcher.match_one(pub, target_profile)
            results_for_queue = [result]
        else:
            all_results = matcher.match_publication(pub, profiles)
            result = all_results[0] if all_results else None
            results_for_queue = all_results
        confidence = result.confidence if result else "none"
        if confidence == "high":
            high += 1
        elif confidence == "medium":
            medium += 1
        elif confidence == "low":
            low += 1
        else:
            unrelated += 1
        confident = [r for r in results_for_queue if r and r.confidence in ("high", "medium")]
        if confident:
            best = confident[0]
            db.remember_article(row["url"], row["url_hash"], row["title_hash"],
                                row["published_at"], row["source"], "pending",
                                status=f"rematch_{best.confidence}",
                                best_asset=best.asset_code, best_score=best.score,
                                pending_llm=True)
            queued += 1

    label = target["code"] if target else "всех активов"
    suffix = f"для {target['code']}" if target else "(лучший актив)"
    lines = [f"🔄 Пересопоставление {label} за {days} дн. (локально, matcher v{matcher.MATCHER_VERSION})",
             "",
             f"• публикаций в локальном корпусе: {corpus['corpus']}",
             f"• без сохранённого текста: {corpus['without_text']}",
             f"• прошли keyword-фильтр: {keyword_pass}",
             f"• high {suffix}: {high}",
             f"• medium {suffix}: {medium}",
             f"• low {suffix}: {low}",
             f"• не относятся к {label}: {unrelated}",
             f"• добавлено в очередь на анализ: {queued}",
             "• вызовов LLM: 0",
             "• стоимость: $0.00000"]
    if queued:
        lines += ["", f"Новых кандидатов для Gemini: {queued}. "
                      "Они будут обработаны при следующем /scan или командой /processqueue."]
    await update.effective_message.reply_text("\n".join(lines))


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/queue — очередь публикаций pending_llm, ждущих анализа Gemini."""
    if not await guard(update): return
    qs = db.queue_stats()
    if not qs["total"]:
        await update.effective_message.reply_text(
            "Очередь pending_llm пуста. Наполняется через /rematch и /match.")
        return
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True), db.list_matcher_rules())
    lines = [f"📥 Очередь на анализ: {qs['total']} публикаций",
             "По активам: " + ", ".join(f"{k}: {v}" for k, v in qs["by_asset"].items()),
             f"Самая старая: {qs['oldest']} UTC", "", "Первые кандидаты:"]
    for row in db.pending_matched_publications(limit=10):
        profile = next((p for p in profiles if p.code == row["best_asset"]), None)
        matched_by = "—"
        if profile:
            r = matcher.match_one({"title": row["title"] or "", "text": row["text"] or "",
                                   "subscribers": []}, profile)
            matched_by = ", ".join(r.matched_by) or "—"
        lines.append(f"— {(row['title'] or '')[:80]}\n"
                     f"  {row['best_asset']} · score {(row['best_score'] or 0):.2f} · "
                     f"{row['source'] or '—'} · {matched_by}")
    lines.append("\nОбработать сейчас: /processqueue")
    await update.effective_message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def test_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Демонстрационный полный отчёт для визуальной проверки форматирования.
    Ничего не записывает в базу и не помечает отправленным."""
    if not await guard(update): return
    raw = " ".join(context.args).strip() or "SPYF"
    asset, _ = db.resolve_asset(raw)
    code = asset["code"] if asset else raw.upper()
    today = date.today()
    demo = {
        "asset_code": code,
        "event_type": "stock_split",
        "severity": "CRITICAL",
        "title": "Дробление бумаг в соотношении 1:5",
        "summary_ru": "Эмитент объявил дробление бумаг в соотношении 1:5. Каждая бумага будет "
                      "конвертирована в пять новых, совокупная стоимость позиции не изменится. "
                      "Это демонстрационный отчёт для проверки форматирования.",
        "impact_ru": "Цена бумаги снизится кратно коэффициенту дробления, количество бумаг в обращении "
                     "вырастет впятеро. Потребуется пересчёт параметров производного инструмента.",
        "action_required": True,
        "recommended_action": "Проверить необходимость корректировки параметров контракта.",
        "event_date": str(today + timedelta(days=7)),
        "publication_date": str(today - timedelta(days=1)),
        "status": "confirmed",
        "confidence": 0.94,
        "source_url": "https://example.com/official-notice",
        "source_name": "Официальный сайт эмитента",
        "key_parameters_json": json.dumps([
            {"label": "Коэффициент дробления", "value": "1:5"},
            {"label": "Ex-date", "value": formatters.format_date_ru(str(today + timedelta(days=6)))},
            {"label": "Record date", "value": formatters.format_date_ru(str(today + timedelta(days=4)))},
            {"label": "Дата вступления в силу", "value": formatters.format_date_ru(str(today + timedelta(days=7)))},
        ], ensure_ascii=False),
        "additional_sources_json": json.dumps(
            [{"name": "Пресс-релиз биржи", "url": "https://example.com/exchange-notice"}], ensure_ascii=False),
    }
    await update.effective_message.reply_text("Демонстрационный отчёт (в базу не записывается):")
    await reply_html(update.effective_message, formatters.format_event_full(demo, asset))


async def setup_commands(app):
    await app.bot.set_my_commands([
        BotCommand("scan","проверить сейчас"), BotCommand("list","активы"),
        BotCommand("find","найти актив"), BotCommand("add","добавить актив"),
        BotCommand("alias","добавить алиас"), BotCommand("addsource","добавить источник"),
        BotCommand("digest","сводка за 24 часа"), BotCommand("events","история событий"),
        BotCommand("status","статус"), BotCommand("reports","отчёты о проверках вкл/выкл"),
        BotCommand("pause","пауза"), BotCommand("resume","продолжить"),
        BotCommand("fullscan","проверка с резервным поиском"), BotCommand("cost","стоимость LLM"),
        BotCommand("debugscan","диагностика одного актива"), BotCommand("dbstats","состояние базы"),
    ])
