"""Опрос RSS/Atom-источников. Каждый источник скачивается один раз за цикл,
все источники — параллельно. Возвращает унифицированные публикации."""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

from app import db, fetcher, health
from app.config import settings

log = logging.getLogger(__name__)

MAX_ENTRIES_PER_FEED = 50
TEXT_LIMIT = 1200

# Заголовок SEC EDGAR: "8-K - COMPANY NAME (0001770561) (Filer)"
SEC_TITLE_RE = re.compile(
    r"^(?P<form>[0-9A-Z][0-9A-Z\-/]{0,9})\s*-\s*(?P<filer>.+?)\s*\((?P<cik>\d{7,10})\)")


# Персональная лента эмитента EDGAR (…browse-edgar?action=getcompany&CIK=…).
_SEC_COMPANY_RE = re.compile(r"sec\.gov/cgi-bin/browse-edgar\?.*action=getcompany.*?[?&]CIK=0*(\d+)",
                             re.IGNORECASE)
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
SEC_FILINGS_PER_COMPANY = 40   # столько же отдавала Atom-лента (count=40)

# Названия форм — как в заголовках Atom-ленты EDGAR («8-K - Current report»).
SEC_FORM_TITLES = {
    "8-K": "Current report", "8-K/A": "Current report, amendment",
    "6-K": "Report of foreign issuer [Rules 13a-16 and 15d-16]",
    "10-K": "Annual report", "10-Q": "Quarterly report", "20-F": "Annual report of foreign issuer",
    "25": "Notification of removal from listing",
    "25-NSE": "Notification of removal from listing (by exchange)",
    "15-12B": "Termination of registration", "15-12G": "Termination of registration",
    "DEF 14A": "Definitive proxy statement", "PRE 14A": "Preliminary proxy statement",
    "S-4": "Registration statement for business combinations",
    "SC TO-T": "Tender offer statement by third party", "SC TO-I": "Issuer tender offer statement",
    "SC 14D9": "Solicitation/recommendation statement",
    "425": "Prospectuses and communications, business combinations",
}
# Пункты 8-K — в Atom они были в описании; по ним matcher и модель узнают делистинг, сплит и т. п.
SEC_8K_ITEMS = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "5.01": "Changes in Control of Registrant",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure", "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


def sec_company_cik(url: str) -> str | None:
    m = _SEC_COMPANY_RE.search(url)
    return m.group(1) if m else None


def parse_sec_submissions(content: bytes | str, source: dict) -> list[dict]:
    """JSON data.sec.gov/submissions → публикации в том же виде, что из Atom-ленты
    эмитента (та же ссылка на индекс подачи — дедупликация не ломается)."""
    data = json.loads(content)
    cik = str(int(data.get("cik") or sec_company_cik(source["url"]) or 0))
    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    publications = []
    for i, accession in enumerate(accessions[:SEC_FILINGS_PER_COMPANY]):
        def col(name: str) -> str:
            values = recent.get(name) or []
            return str(values[i] or "") if i < len(values) else ""
        form = col("form").strip()
        if not accession or not form:
            continue
        description = col("primaryDocDescription").strip()
        if description.upper() in ("", form.upper(), f"FORM {form.upper()}"):
            description = SEC_FORM_TITLES.get(form, description or form)
        title = f"{form} - {description}"
        items = [f"Item {it}: {SEC_8K_ITEMS.get(it, '')}".rstrip(": ")
                 for it in col("items").split(",") if it.strip()]
        text = f"Filed: {col('filingDate')} AccNo: {accession}"
        if items:
            text += " " + "; ".join(items)
        published_dt = None
        accepted = col("acceptanceDateTime") or col("filingDate")
        try:
            published_dt = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        publications.append({
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{accession.replace('-', '')}/{accession}-index.htm",
            "title": title,
            "text": text[:TEXT_LIMIT],
            "published_at": published_dt.isoformat() if published_dt else accepted or None,
            "published_dt": published_dt,
            "source": source["name"],
            "source_id": source["id"],
            "source_type": source["type"],
            "subscribers": source.get("subscribers", []),
            "sec_cik": cik,
            "form_type": form,
        })
    return publications


def _entry_published(entry) -> tuple[str | None, datetime | None]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            dt = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            return dt.isoformat(), dt
    raw = entry.get("published") or entry.get("updated")
    return (raw, None) if raw else (None, None)


def parse_feed(content: bytes | str, source: dict) -> list[dict]:
    """Разбирает содержимое фида в публикации (синхронно, вызывается в thread)."""
    feed = feedparser.parse(content)
    if not feed.entries and isinstance(content, (bytes, str)):
        # HTML под RSS-адресом (bnbchain.org/blog/rss.xml) — отдаём HTML-парсеру.
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
        if "<html" in text[:400].lower():
            from app.html_monitor import parse_links
            return parse_links(text, source)
    publications = []
    for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
        url = entry.get("link") or ""
        title = (entry.get("title") or "").strip()
        if not url or not title:
            continue
        summary = fetcher.plain_text(entry.get("summary") or entry.get("description") or "")
        published_at, published_dt = _entry_published(entry)
        pub = {
            "url": url,
            "title": title,
            "text": summary[:TEXT_LIMIT],
            "published_at": published_at,
            "published_dt": published_dt,
            "source": source["name"],
            "source_id": source["id"],
            "source_type": source["type"],
            "subscribers": source.get("subscribers", []),
        }
        # SEC-фиды: сопоставление идёт по filer (имя + CIK), а не по тексту заголовка.
        if source["type"] == "SEC":
            m = SEC_TITLE_RE.match(title)
            if m:
                pub["form_type"] = m.group("form")
                pub["sec_filer"] = m.group("filer").strip()
                pub["sec_cik"] = m.group("cik")
            else:
                # Персональная лента эмитента (…getcompany&CIK=…): filer в заголовке
                # отсутствует, CIK берём из URL источника — матчинг по CIK детерминирован.
                cik_m = re.search(r"[?&]CIK=0*(\d+)", source["url"])
                if cik_m:
                    pub["sec_cik"] = cik_m.group(1)
                    form_m = re.match(r"^\s*([0-9A-Z][0-9A-Z\-/]{0,9}?)\s+-\s", title)
                    pub["form_type"] = form_m.group(1) if form_m else title[:10].strip()
        publications.append(pub)
    return publications


async def _fetch_sec_company(client: httpx.AsyncClient, source: dict, cik: str) -> list[dict]:
    """Лента эмитента SEC: сначала JSON data.sec.gov (отвечает за доли секунды),
    при сбое — исходная Atom-лента browse-edgar (с ноября 2026 отвечает 20–30 с)."""
    try:
        content = await fetcher.fetch(client, {**source, "url": SEC_SUBMISSIONS_URL.format(cik=cik)})
        return await fetcher.parse_in_thread(parse_sec_submissions, content, source)
    except Exception as exc:
        log.info("SEC JSON для CIK %s недоступен (%s: %s) — беру Atom-ленту",
                 cik, type(exc).__name__, exc)
    content = await fetcher.fetch(client, source)
    return await fetcher.parse_in_thread(parse_feed, content, source)


async def fetch_source(client: httpx.AsyncClient, source: dict) -> list[dict]:
    try:
        cik = sec_company_cik(source["url"])
        if cik:
            publications = await _fetch_sec_company(client, source, cik)
        else:
            content = await fetcher.fetch(client, source)
            publications = await fetcher.parse_in_thread(parse_feed, content, source)
            del content
        db.mark_source_checked(source["id"])
        alert = health.check_source(source, None)
        if alert:
            health.pending_alerts.append(alert)
        return publications
    except Exception as exc:
        db.mark_source_checked(source["id"], error=f"{type(exc).__name__}: {exc}"[:300])
        alert = health.check_source(source, f"{type(exc).__name__}: {exc}")
        if alert:
            health.pending_alerts.append(alert)
        log.warning("RSS-источник %s недоступен: %s: %s", source["name"], type(exc).__name__, exc)
        return []


async def fetch_all(client: httpx.AsyncClient, sources: list[dict]) -> list[dict]:
    """Скачивает все RSS-источники параллельно."""
    if not sources:
        return []
    results = await asyncio.gather(*(fetch_source(client, s) for s in sources))
    publications = [p for batch in results for p in batch]
    log.info("RSS: %d источников → %d публикаций", len(sources), len(publications))
    return publications
