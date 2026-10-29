from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  instrument_type TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  monitor_fields_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS asset_identifiers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_code TEXT NOT NULL,
  identifier TEXT NOT NULL COLLATE NOCASE,
  kind TEXT NOT NULL DEFAULT 'alias',
  UNIQUE(asset_code, identifier),
  FOREIGN KEY(asset_code) REFERENCES assets(code) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_asset_identifiers_identifier ON asset_identifiers(identifier);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_code TEXT NOT NULL,
  url TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'official',
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE(asset_code, url),
  FOREIGN KEY(asset_code) REFERENCES assets(code) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_defs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'HTML',          -- RSS | SEC | IR | EXCHANGE | NEWS | HTML | SEARCH
  priority INTEGER NOT NULL DEFAULT 2,        -- 1 = официальный уровень, больше = ниже приоритет
  url TEXT NOT NULL UNIQUE,
  parser TEXT NOT NULL DEFAULT 'auto',
  refresh_interval INTEGER NOT NULL DEFAULT 60,  -- минуты
  enabled INTEGER NOT NULL DEFAULT 1,
  last_checked_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS asset_sources (
  asset_code TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  UNIQUE(asset_code, source_id),
  FOREIGN KEY(asset_code) REFERENCES assets(code) ON DELETE CASCADE,
  FOREIGN KEY(source_id) REFERENCES source_defs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS processed_articles (
  url_hash TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  title_hash TEXT,
  published_at TEXT,
  processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source TEXT,
  llm_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_processed_title ON processed_articles(title_hash);
CREATE TABLE IF NOT EXISTS unmatched_publications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash TEXT NOT NULL UNIQUE,
  best_asset TEXT,
  best_score REAL,
  candidate_matches_json TEXT,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_at TEXT,
  review_status TEXT NOT NULL DEFAULT 'new'   -- new | matched | ignored
);
CREATE TABLE IF NOT EXISTS matcher_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_code TEXT NOT NULL,
  rule_type TEXT NOT NULL,        -- alias | negative
  pattern TEXT NOT NULL,
  weight REAL,
  source_domain TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_by TEXT,
  UNIQUE(asset_code, rule_type, pattern)
);
CREATE TABLE IF NOT EXISTS subscribers (
  chat_id INTEGER PRIMARY KEY,
  name TEXT,
  added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS health_incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component TEXT NOT NULL,
  key TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning',
  message TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  notified_at TEXT,
  resolved_at TEXT,
  UNIQUE(component, key)
);
CREATE TABLE IF NOT EXISTS email_recipients (
  email TEXT PRIMARY KEY COLLATE NOCASE,
  name TEXT,
  added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  cache_hits INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  instrument_type TEXT NOT NULL,
  search_phrase TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'critical'
);
CREATE TABLE IF NOT EXISTS events (
  fingerprint TEXT PRIMARY KEY,
  asset_code TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_date TEXT,
  title TEXT NOT NULL,
  summary_ru TEXT NOT NULL,
  impact_ru TEXT,
  source_url TEXT NOT NULL,
  source_name TEXT,
  confidence REAL NOT NULL,
  raw_json TEXT NOT NULL,
  detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sent_at TEXT,
  severity TEXT,
  underlying_ticker TEXT,
  publication_date TEXT,
  effective_date TEXT,
  status TEXT,
  action_required INTEGER,
  recommended_action TEXT,
  key_parameters_json TEXT,
  additional_sources_json TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  assets_scanned INTEGER NOT NULL DEFAULT 0,
  new_events INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
"""

@contextmanager
def connect():
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Колонки, добавленные после первого релиза: существующие базы мигрируются
# через ALTER TABLE, старые записи получают NULL. Данные не удаляются.
_EVENT_COLUMNS = {
    "severity": "TEXT",
    "underlying_ticker": "TEXT",
    "publication_date": "TEXT",
    "effective_date": "TEXT",
    "status": "TEXT",
    "action_required": "INTEGER",
    "recommended_action": "TEXT",
    "key_parameters_json": "TEXT",
    "additional_sources_json": "TEXT",
    "is_test": "INTEGER NOT NULL DEFAULT 0",
}
_SCAN_RUN_COLUMNS = {"errors": "INTEGER", "stats_json": "TEXT"}
_SOURCE_COLUMNS = {"fail_streak": "INTEGER NOT NULL DEFAULT 0"}
# Нормализованная модель базового актива: контракт МосБиржи — лишь один из идентификаторов.
_ASSET_COLUMNS = {
    "underlying_ticker": "TEXT", "issuer_name": "TEXT", "fund_provider": "TEXT",
    "exchange": "TEXT", "isin": "TEXT", "cusip": "TEXT", "cik": "TEXT",
    "lei": "TEXT", "adr_ticker": "TEXT", "local_ticker": "TEXT",
}
# Кэш публикаций: статусы жизненного цикла и версии локальных алгоритмов.
_PROCESSED_COLUMNS = {
    "status": "TEXT",              # fetched|keyword_filtered|matched|llm_analyzed|rejected|event_created|unmatched|ignored
    "title": "TEXT",
    "text": "TEXT",
    "matcher_version": "INTEGER",
    "filter_version": "INTEGER",
    "llm_prompt_version": "INTEGER",
    "best_asset": "TEXT",
    "best_score": "REAL",
    "pending_llm": "INTEGER NOT NULL DEFAULT 0",
    # Результат Gemini сохраняется и для отклонённых публикаций — для дневного Excel-отчёта.
    "llm_event_type": "TEXT",
    "llm_summary": "TEXT",
}


def _migrate_columns(conn, table: str, columns: dict[str, str]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, col_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            log.info("Миграция: добавлена колонка %s.%s", table, column)


def _infer_source_type(url: str) -> str:
    u = url.lower()
    if "sec.gov" in u:
        return "SEC"
    if any(x in u for x in ("rss", "feed", ".xml", "atom")):
        return "RSS"
    if any(x in u for x in ("nyse.com", "nasdaq", "hkex", "cmegroup", "theice.com", "lme.com", "moex")):
        return "EXCHANGE"
    return "IR"


def _source_name_from_url(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc.removeprefix("www.") or url[:60]


def _migrate_sources(conn) -> None:
    """Одноразовый перенос: пер-активные строки старой таблицы sources превращаются
    в единый реестр source_defs + подписки asset_sources. Источник хранится один раз,
    актив лишь подписывается на него. Старая таблица остаётся как резервная копия."""
    done = conn.execute("SELECT value FROM settings WHERE key='sources_v2_migrated'").fetchone()
    if done and done[0] == "1":
        return
    for r in conn.execute("SELECT asset_code, url, kind, enabled FROM sources").fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO source_defs(name,type,priority,url,parser,refresh_interval,enabled) "
            "VALUES(?,?,?,?,?,?,?)",
            (_source_name_from_url(r["url"]), _infer_source_type(r["url"]),
             1 if r["kind"] == "official" else 2, r["url"], "auto", 60, r["enabled"]))
        source_id = conn.execute("SELECT id FROM source_defs WHERE url=?", (r["url"],)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO asset_sources(asset_code,source_id) VALUES(?,?)",
                     (r["asset_code"], source_id))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('sources_v2_migrated','1')")
    log.info("Источники перенесены в source_defs/asset_sources")


# Глобальные источники первого уровня: опрашиваются один раз, публикации
# сопоставляются со всеми активами локально. Идемпотентный сид по URL.
# Все URL проверены живыми запросами перед добавлением.
_DEFAULT_GLOBAL_SOURCES = [
    {"name": "SEC EDGAR — свежие 8-K", "type": "SEC", "priority": 1, "parser": "rss", "refresh_interval": 30,
     "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom"},
    {"name": "SEC EDGAR — свежие 6-K", "type": "SEC", "priority": 1, "parser": "rss", "refresh_interval": 30,
     "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&company=&dateb=&owner=include&count=100&output=atom"},
    {"name": "SEC EDGAR — Form 25 (делистинг)", "type": "SEC", "priority": 1, "parser": "rss", "refresh_interval": 60,
     "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=25&company=&dateb=&owner=include&count=100&output=atom"},
    {"name": "Nasdaq Trader — новости листинга", "type": "EXCHANGE", "priority": 1, "parser": "rss", "refresh_interval": 30,
     "url": "https://www.nasdaqtrader.com/rss.aspx?feed=currentheadlines"},
]

# Проверенные источники, привязанные к конкретным активам (подписки).
_DEFAULT_ASSET_SOURCES = [
    {"name": "SAP News (пресс-релизы)", "type": "IR", "priority": 1, "parser": "rss",
     "refresh_interval": 60, "url": "https://news.sap.com/feed/", "subscribers": ["SAP"]},
    {"name": "Samsung Newsroom (пресс-релизы)", "type": "IR", "priority": 1, "parser": "rss",
     "refresh_interval": 60, "url": "https://news.samsung.com/global/feed", "subscribers": ["SAMSUNG"]},
    {"name": "HKEX — News Releases", "type": "EXCHANGE", "priority": 1, "parser": "html",
     "refresh_interval": 60, "url": "https://www.hkex.com.hk/News/News-Release?sc_lang=en",
     "subscribers": ["TENCENT", "XIA", "HANG"]},
    {"name": "ICE Futures Europe — циркуляры", "type": "EXCHANGE", "priority": 1, "parser": "html",
     "refresh_interval": 120, "url": "https://www.ice.com/futures-europe/circulars",
     "subscribers": ["BR, BRM", "TTF"]},
]


def _seed_default_sources(conn) -> None:
    for s in _DEFAULT_GLOBAL_SOURCES + _DEFAULT_ASSET_SOURCES:
        conn.execute(
            "INSERT OR IGNORE INTO source_defs(name,type,priority,url,parser,refresh_interval,enabled) "
            "VALUES(?,?,?,?,?,?,1)",
            (s["name"], s["type"], s["priority"], s["url"], s["parser"], s["refresh_interval"]))
        source_id = conn.execute("SELECT id FROM source_defs WHERE url=?", (s["url"],)).fetchone()[0]
        for code in s.get("subscribers", []):
            if conn.execute("SELECT 1 FROM assets WHERE code=?", (code,)).fetchone():
                conn.execute("INSERT OR IGNORE INTO asset_sources(asset_code,source_id) VALUES(?,?)",
                             (code, source_id))


_LEGAL_SUFFIXES = ("etf trust", "ucits etf", "etf", "trust", "ltd", "inc", "corp",
                   "corporation", "adr", "plc", "co", "fund", "futures", "index")


def _enrich_assets(conn) -> None:
    """Одноразовое безопасное обогащение из уже имеющихся данных (без выдумывания
    идентификаторов): тикеры из скобок в имени, имя без скобок и юр. суффиксов — в алиасы."""
    done = conn.execute("SELECT value FROM settings WHERE key='assets_enriched_v3'").fetchone()
    if done and done[0] == "1":
        return
    for a in conn.execute("SELECT code, name, underlying_ticker FROM assets").fetchall():
        name = a["name"]
        aliases: list[str] = []
        m = re.search(r"\(([^)]{1,40})\)\s*$", name)
        underlying = a["underlying_ticker"]
        if m:
            for token in re.split(r"[,;/]", m.group(1)):
                token = token.strip()
                if re.fullmatch(r"[A-Za-z0-9. \-]{2,12}", token):
                    aliases.append(token)
                    if not underlying and re.fullmatch(r"[A-Z0-9.]{2,6}", token):
                        underlying = token
            base = name[:m.start()].strip(" –-")
            if len(base) >= 8:
                aliases.append(base)
                stripped = base
                changed = True
                while changed:
                    changed = False
                    for suffix in _LEGAL_SUFFIXES:
                        if stripped.lower().endswith(" " + suffix):
                            stripped = stripped[: -len(suffix) - 1].strip(" ,–-")
                            changed = True
                if len(stripped) >= 8 and stripped.lower() != base.lower():
                    aliases.append(stripped)
        if underlying and not a["underlying_ticker"]:
            conn.execute("UPDATE assets SET underlying_ticker=? WHERE code=?", (underlying, a["code"]))
        _add_identifiers(conn, a["code"], aliases)
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('assets_enriched_v3','1')")
    log.info("Активы обогащены тикерами и алиасами из имеющихся данных")


def _backup_before_migration(path: Path) -> None:
    """Резервная копия базы перед крупной миграцией схемы (однократно)."""
    import shutil
    backup = path.with_suffix(path.suffix + ".bak-v3")
    if not backup.exists():
        shutil.copy2(path, backup)
        log.info("Создана резервная копия базы: %s", backup)


def init_db() -> None:
    path = Path(settings.db_path)
    needs_v3 = False
    if path.exists():
        with connect() as conn:
            has_settings = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
            if has_settings:
                needs_v3 = conn.execute(
                    "SELECT value FROM settings WHERE key='schema_v3'").fetchone() is None
        if needs_v3:
            _backup_before_migration(path)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_identifiers(conn)
        _migrate_columns(conn, "events", _EVENT_COLUMNS)
        _migrate_columns(conn, "scan_runs", _SCAN_RUN_COLUMNS)
        _migrate_columns(conn, "assets", _ASSET_COLUMNS)
        _migrate_columns(conn, "processed_articles", _PROCESSED_COLUMNS)
        _migrate_columns(conn, "source_defs", _SOURCE_COLUMNS)
        _migrate_sources(conn)
        _seed_default_sources(conn)
        _enrich_assets(conn)
        # Тестовые события, созданные /testevent до появления is_test, помечаются надёжно:
        # у них фиксированные event_type и source_url.
        conn.execute("UPDATE events SET is_test=1 WHERE is_test=0 AND "
                     "(event_type='Тестовое событие' OR source_url LIKE '%example.com/testevent%')")
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('schema_v3','1')")


def _add_identifiers(conn, code: str, aliases: Iterable[str]) -> None:
    conn.execute("INSERT OR IGNORE INTO asset_identifiers(asset_code,identifier,kind) VALUES(?,?,'code')",
                 (code, code))
    extra = list(aliases)
    # Составные коды вида "BR, BRM" — это несколько контрактных кодов одного базового
    # актива: каждый становится самостоятельным идентификатором.
    if "," in code:
        extra += code.split(",")
    for alias in extra:
        alias = (alias or "").strip()
        if alias:
            conn.execute("INSERT OR IGNORE INTO asset_identifiers(asset_code,identifier,kind) VALUES(?,?,'alias')",
                         (code, alias))


def _migrate_identifiers(conn) -> None:
    """Одноразовый перенос алиасов из aliases_json в таблицу asset_identifiers.
    После миграции источником истины по идентификаторам служит только эта таблица."""
    done = conn.execute("SELECT value FROM settings WHERE key='identifiers_migrated'").fetchone()
    if done and done[0] == "1":
        return
    for r in conn.execute("SELECT code, aliases_json FROM assets").fetchall():
        _add_identifiers(conn, r["code"], json.loads(r["aliases_json"] or "[]"))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('identifiers_migrated','1')")
    log.info("Идентификаторы активов перенесены в asset_identifiers")


def seed_from_json(assets_path: str, prompts_path: str) -> None:
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0:
            assets = json.loads(Path(assets_path).read_text(encoding="utf-8"))
            for a in assets:
                conn.execute(
                    "INSERT INTO assets(code,name,instrument_type,aliases_json,monitor_fields_json,enabled) VALUES(?,?,?,?,?,?)",
                    (a["code"], a["name"], a["instrument_type"], json.dumps(a.get("aliases", []), ensure_ascii=False),
                     json.dumps(a.get("monitor_fields", []), ensure_ascii=False), int(a.get("enabled", True))),
                )
                _add_identifiers(conn, a["code"], a.get("aliases", []))
                for s in a.get("sources", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO source_defs(name,type,priority,url,parser,refresh_interval,enabled) "
                        "VALUES(?,?,?,?,?,?,1)",
                        (_source_name_from_url(s["url"]), _infer_source_type(s["url"]),
                         1 if s.get("kind", "official") == "official" else 2, s["url"], "auto", 60))
                    source_id = conn.execute("SELECT id FROM source_defs WHERE url=?", (s["url"],)).fetchone()[0]
                    conn.execute("INSERT OR IGNORE INTO asset_sources(asset_code,source_id) VALUES(?,?)",
                                 (a["code"], source_id))
        if conn.execute("SELECT COUNT(*) FROM event_prompts").fetchone()[0] == 0:
            prompts = json.loads(Path(prompts_path).read_text(encoding="utf-8"))
            conn.executemany(
                "INSERT INTO event_prompts(event_type,instrument_type,search_phrase,severity) VALUES(?,?,?,?)",
                [(p["event_type"], p["instrument_type"], p["search_phrase"], p.get("severity", "critical")) for p in prompts],
            )
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('paused','0')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('interval_minutes',?)", (str(settings.scan_interval_minutes),))


def list_assets(enabled_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE a.enabled=1" if enabled_only else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM assets a {where} ORDER BY code").fetchall()
        result = []
        for r in rows:
            sources = conn.execute(
                "SELECT d.id, d.url, d.type AS kind, d.name, d.priority FROM asset_sources s "
                "JOIN source_defs d ON d.id = s.source_id "
                "WHERE s.asset_code=? AND d.enabled=1 ORDER BY d.priority, d.id", (r["code"],)).fetchall()
            aliases = [i["identifier"] for i in conn.execute(
                "SELECT identifier FROM asset_identifiers WHERE asset_code=? AND kind!='code' ORDER BY id",
                (r["code"],)).fetchall()]
            result.append({**dict(r), "aliases": aliases,
                           "monitor_fields": json.loads(r["monitor_fields_json"]),
                           "sources": [dict(s) for s in sources]})
        return result


def get_asset(code: str) -> dict[str, Any] | None:
    code = code.upper().strip()
    return next((a for a in list_assets() if a["code"].upper() == code), None)


def resolve_asset(query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Находит базовый актив по любому известному идентификатору: каноническому коду,
    алиасу, тикеру или имени. Сначала точные совпадения, затем поиск по подстроке.
    Возвращает (актив, []) при однозначном совпадении, иначе (None, кандидаты)."""
    q = query.strip()
    if not q:
        return None, []
    with connect() as conn:
        row = conn.execute("SELECT code FROM assets WHERE code=? COLLATE NOCASE", (q,)).fetchone()
        codes = [row["code"]] if row else []
        if not codes:
            codes = [r["asset_code"] for r in conn.execute(
                "SELECT DISTINCT asset_code FROM asset_identifiers WHERE identifier=?", (q,)).fetchall()]
        if not codes:
            codes = [r["code"] for r in conn.execute(
                "SELECT code FROM assets WHERE name=? COLLATE NOCASE", (q,)).fetchall()]
        if not codes:
            like = f"%{q}%"
            codes = [r["code"] for r in conn.execute(
                "SELECT DISTINCT a.code FROM assets a LEFT JOIN asset_identifiers i ON i.asset_code=a.code "
                "WHERE a.name LIKE ? OR i.identifier LIKE ? ORDER BY a.code", (like, like)).fetchall()]
    if len(codes) == 1:
        return get_asset(codes[0]), []
    candidates = [a for c in codes[:10] for a in (get_asset(c),) if a]
    return None, candidates


def upsert_asset(code: str, name: str, instrument_type: str, aliases: list[str] | None = None) -> None:
    with connect() as conn:
        conn.execute("""
        INSERT INTO assets(code,name,instrument_type,aliases_json,monitor_fields_json,enabled)
        VALUES(?,?,?,?,?,1)
        ON CONFLICT(code) DO UPDATE SET name=excluded.name,instrument_type=excluded.instrument_type,
          enabled=1,updated_at=CURRENT_TIMESTAMP
        """, (code.upper().strip(), name.strip(), instrument_type.upper().strip(), "[]", "[]"))
        _add_identifiers(conn, code.upper().strip(), aliases or [])


def add_alias(code: str, alias: str, kind: str = "alias") -> bool:
    """Добавляет идентификатор (алиас, тикер, ISIN…) к активу по каноническому коду."""
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO asset_identifiers(asset_code,identifier,kind) VALUES(?,?,?)",
                           (code.upper().strip(), alias.strip(), kind))
        return cur.rowcount > 0


def add_source(code: str, url: str, kind: str = "official",
               source_type: str | None = None, refresh_interval: int = 60) -> None:
    """Регистрирует источник (один раз, по URL) и подписывает на него актив."""
    url = url.strip()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_defs(name,type,priority,url,parser,refresh_interval,enabled) "
            "VALUES(?,?,?,?,?,?,1)",
            (_source_name_from_url(url), source_type or _infer_source_type(url),
             1 if kind == "official" else 2, url, "auto", refresh_interval))
        source_id = conn.execute("SELECT id FROM source_defs WHERE url=?", (url,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO asset_sources(asset_code,source_id) VALUES(?,?)",
                     (code.upper().strip(), source_id))


def list_source_defs(enabled_only: bool = True, due_only: bool = False) -> list[dict[str, Any]]:
    """Реестр источников; due_only — только те, чей refresh_interval истёк."""
    where = ["enabled=1"] if enabled_only else []
    if due_only:
        where.append("(last_checked_at IS NULL OR "
                     "datetime(last_checked_at, '+' || refresh_interval || ' minutes') <= datetime('now'))")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM source_defs {clause} ORDER BY priority, id").fetchall()
        result = []
        for r in rows:
            subscribers = [s["asset_code"] for s in conn.execute(
                "SELECT asset_code FROM asset_sources WHERE source_id=?", (r["id"],)).fetchall()]
            result.append({**dict(r), "subscribers": subscribers})
        return result


def mark_source_checked(source_id: int, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE source_defs SET last_checked_at=CURRENT_TIMESTAMP, last_error=? WHERE id=?",
                     (error, source_id))


# --- Кэш обработанных публикаций -------------------------------------------

# Финальные статусы: публикация закрыта навсегда (или до обработки очереди).
_FINAL_STATUSES = {"llm_analyzed", "rejected", "event_created", "ignored", "matched",
                   "rematch_high", "rematch_medium"}
# Статусы, зависящие от версии matcher: переоткрываются при её росте.
_MATCHER_BOUND_STATUSES = {"unmatched", "fetched", "irrelevant_global",
                           "rematch_low", "rematch_unmatched"}


def _legacy_status(llm_result: str | None) -> str:
    """Статус для записей, созданных до введения колонки status."""
    if not llm_result or llm_result == "no_response":
        return "fetched"
    if llm_result.startswith("skipped:no_keywords"):
        return "keyword_filtered"
    if llm_result.startswith("skipped:no_asset_match"):
        return "unmatched"
    return "llm_analyzed"


def _row_blocks_reprocessing(row) -> bool:
    from app import versions
    if row["pending_llm"]:
        return True  # уже в очереди на анализ, не скачивать заново
    status = row["status"] or _legacy_status(row["llm_result"])
    if status in _FINAL_STATUSES:
        return True
    # Нефинальные статусы блокируют повтор только если версии алгоритмов не менялись:
    # улучшение matcher/фильтра открывает публикацию заново.
    if status == "keyword_filtered":
        return row["filter_version"] == versions.FILTER_VERSION
    if status in _MATCHER_BOUND_STATUSES:
        return (row["matcher_version"] == versions.MATCHER_VERSION
                and row["filter_version"] == versions.FILTER_VERSION)
    return True


def article_seen(url_hash: str, title_hash: str | None = None) -> bool:
    """Нужно ли пропустить публикацию: она финализирована либо уже
    отфильтрована актуальными версиями matcher/фильтра."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM processed_articles WHERE url_hash=?", (url_hash,)).fetchone()
        if row is None and title_hash:
            row = conn.execute("SELECT * FROM processed_articles WHERE title_hash=?", (title_hash,)).fetchone()
        return _row_blocks_reprocessing(row) if row else False


def remember_article(url: str, url_hash: str, title_hash: str | None,
                     published_at: str | None, source: str | None, llm_result: str | None,
                     status: str = "fetched", title: str | None = None, text: str | None = None,
                     best_asset: str | None = None, best_score: float | None = None,
                     pending_llm: bool = False, llm_event_type: str | None = None,
                     llm_summary: str | None = None) -> None:
    from app import versions
    with connect() as conn:
        conn.execute("""
        INSERT INTO processed_articles(url_hash,url,title_hash,published_at,source,llm_result,
          status,title,text,matcher_version,filter_version,llm_prompt_version,best_asset,best_score,
          pending_llm,llm_event_type,llm_summary)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(url_hash) DO UPDATE SET
          llm_result=excluded.llm_result, status=excluded.status,
          title=COALESCE(excluded.title, title), text=COALESCE(excluded.text, text),
          matcher_version=excluded.matcher_version, filter_version=excluded.filter_version,
          llm_prompt_version=excluded.llm_prompt_version,
          best_asset=COALESCE(excluded.best_asset, best_asset),
          best_score=COALESCE(excluded.best_score, best_score),
          pending_llm=excluded.pending_llm,
          llm_event_type=COALESCE(excluded.llm_event_type, llm_event_type),
          llm_summary=COALESCE(excluded.llm_summary, llm_summary),
          processed_at=CURRENT_TIMESTAMP
        """, (url_hash, url, title_hash, published_at, source, llm_result, status,
              title, text, versions.MATCHER_VERSION, versions.FILTER_VERSION,
              versions.LLM_PROMPT_VERSION, best_asset, best_score, int(pending_llm),
              llm_event_type, llm_summary))


def get_article(url_hash: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM processed_articles WHERE url_hash=?", (url_hash,)).fetchone()
        return dict(row) if row else None


def set_article_status(url_hash: str, status: str, llm_result: str | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE processed_articles SET status=?, llm_result=COALESCE(?, llm_result), "
                     "processed_at=CURRENT_TIMESTAMP WHERE url_hash=?", (status, llm_result, url_hash))


_REMATCHABLE = "('unmatched','fetched','keyword_filtered','irrelevant_global','rematch_low','rematch_unmatched')"


def publications_for_rematch(days: int = 7) -> list[dict[str, Any]]:
    """Сохранённые публикации, которые можно пересопоставить локально без сети:
    нефинальные статусы и сохранённый заголовок."""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM processed_articles WHERE title IS NOT NULL AND pending_llm=0 "
            f"AND COALESCE(status,'fetched') IN {_REMATCHABLE} "
            "AND processed_at >= datetime('now', ?) ORDER BY processed_at DESC",
            (f"-{int(days)} days",)).fetchall()
        return [dict(r) for r in rows]


def rematch_corpus_stats(days: int = 7) -> dict[str, int]:
    """Размер локального корпуса за период и сколько записей без сохранённого текста."""
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM processed_articles WHERE pending_llm=0 "
            f"AND COALESCE(status,'fetched') IN {_REMATCHABLE} "
            "AND processed_at >= datetime('now', ?)", (f"-{int(days)} days",)).fetchone()[0]
        without_text = conn.execute(
            f"SELECT COUNT(*) FROM processed_articles WHERE title IS NULL AND pending_llm=0 "
            f"AND COALESCE(status,'fetched') IN {_REMATCHABLE} "
            "AND processed_at >= datetime('now', ?)", (f"-{int(days)} days",)).fetchone()[0]
        return {"corpus": total, "without_text": without_text}


def pending_matched_publications(limit: int = 100) -> list[dict[str, Any]]:
    """Очередь pending_llm: публикации, сопоставленные через /rematch или /match
    и ждущие LLM-анализа при следующем /scan либо /processqueue."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM processed_articles WHERE pending_llm=1 AND title IS NOT NULL "
            "ORDER BY processed_at LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def queue_stats() -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM processed_articles WHERE pending_llm=1").fetchone()[0]
        by_asset = {r["best_asset"] or "?": r["n"] for r in conn.execute(
            "SELECT best_asset, COUNT(*) n FROM processed_articles WHERE pending_llm=1 "
            "GROUP BY best_asset ORDER BY n DESC").fetchall()}
        oldest = conn.execute(
            "SELECT MIN(processed_at) FROM processed_articles WHERE pending_llm=1").fetchone()[0]
        return {"total": total, "by_asset": by_asset, "oldest": oldest}


# --- Очередь unmatched и правила matcher ------------------------------------

def save_unmatched(url_hash: str, best_asset: str | None, best_score: float | None,
                   candidates_json: str, reason: str) -> None:
    with connect() as conn:
        conn.execute("""
        INSERT INTO unmatched_publications(url_hash,best_asset,best_score,candidate_matches_json,reason)
        VALUES(?,?,?,?,?)
        ON CONFLICT(url_hash) DO UPDATE SET best_asset=excluded.best_asset,
          best_score=excluded.best_score, candidate_matches_json=excluded.candidate_matches_json,
          reason=excluded.reason
        """, (url_hash, best_asset, best_score, candidates_json, reason))


def list_unmatched(limit: int = 20, asset_code: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE u.review_status='new'"
    params: list = []
    if asset_code:
        where += " AND u.best_asset=?"
        params.append(asset_code.upper())
    with connect() as conn:
        rows = conn.execute(f"""
        SELECT u.*, p.title, p.source, p.url FROM unmatched_publications u
        LEFT JOIN processed_articles p ON p.url_hash = u.url_hash
        {where} ORDER BY u.id DESC LIMIT ?""", (*params, limit)).fetchall()
        return [dict(r) for r in rows]


def get_unmatched(unmatched_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT u.*, p.title, p.source, p.url, p.text, p.published_at FROM unmatched_publications u "
            "LEFT JOIN processed_articles p ON p.url_hash = u.url_hash WHERE u.id=?",
            (unmatched_id,)).fetchone()
        return dict(row) if row else None


def review_unmatched(unmatched_id: int, review_status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE unmatched_publications SET review_status=?, reviewed_at=CURRENT_TIMESTAMP "
                     "WHERE id=?", (review_status, unmatched_id))


def add_matcher_rule(asset_code: str, rule_type: str, pattern: str, weight: float | None = None,
                     source_domain: str | None = None, created_by: str | None = None) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO matcher_rules(asset_code,rule_type,pattern,weight,source_domain,created_by) "
            "VALUES(?,?,?,?,?,?)",
            (asset_code.upper(), rule_type, pattern.strip(), weight, source_domain, created_by))
        return cur.rowcount > 0


def list_matcher_rules(enabled_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE enabled=1" if enabled_only else ""
    with connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM matcher_rules {where}").fetchall()]


# Поля активов, редактируемые командой /setid (точные идентификаторы не выдумываются —
# их вносит администратор).
ASSET_ID_FIELDS = {"isin", "cusip", "cik", "lei", "underlying_ticker", "adr_ticker",
                   "local_ticker", "issuer_name", "fund_provider", "exchange"}


# --- Подписчики уведомлений (сотрудники с доступом только на чтение) ---------

def add_subscriber(chat_id: int, name: str | None = None) -> bool:
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO subscribers(chat_id,name) VALUES(?,?)",
                           (int(chat_id), name))
        return cur.rowcount > 0


def remove_subscriber(chat_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM subscribers WHERE chat_id=?", (int(chat_id),)).rowcount > 0


def list_subscribers() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM subscribers ORDER BY added_at").fetchall()]


def is_subscriber(chat_id: int) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM subscribers WHERE chat_id=?", (int(chat_id),)).fetchone() is not None


# --- Email-адресаты (дублирование уведомлений на почту) ----------------------

def add_email_recipient(email: str, name: str | None = None) -> bool:
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO email_recipients(email,name) VALUES(?,?)",
                           (email.strip().lower(), name))
        return cur.rowcount > 0


def remove_email_recipient(email: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM email_recipients WHERE email=?",
                            (email.strip().lower(),)).rowcount > 0


def list_email_recipients() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM email_recipients ORDER BY added_at").fetchall()]


def set_asset_field(code: str, field: str, value: str | None) -> None:
    if field not in ASSET_ID_FIELDS:
        raise ValueError(f"Недопустимое поле: {field}")
    with connect() as conn:
        conn.execute(f"UPDATE assets SET {field}=?, updated_at=CURRENT_TIMESTAMP WHERE code=?",
                     (value, code.upper().strip()))


def prune_processed_articles(days: int = 30) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM processed_articles WHERE processed_at < datetime('now', ?)",
                     (f"-{int(days)} days",))


# --- Учёт стоимости LLM ------------------------------------------------------

def record_llm_usage(provider: str, model: str, calls: int, input_tokens: int,
                     output_tokens: int, cost_usd: float, cache_hits: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO llm_usage(provider,model,calls,input_tokens,output_tokens,cost_usd,cache_hits) "
            "VALUES(?,?,?,?,?,?,?)", (provider, model, calls, input_tokens, output_tokens, cost_usd, cache_hits))


def cost_stats() -> dict[str, Any]:
    with connect() as conn:
        def agg(where: str) -> dict:
            row = conn.execute(
                f"SELECT COALESCE(SUM(calls),0) c, COALESCE(SUM(input_tokens),0) ti, "
                f"COALESCE(SUM(output_tokens),0) to_, COALESCE(SUM(cost_usd),0) usd, "
                f"COALESCE(SUM(cache_hits),0) ch FROM llm_usage {where}").fetchone()
            return {"calls": row["c"], "input_tokens": row["ti"], "output_tokens": row["to_"],
                    "cost_usd": row["usd"], "cache_hits": row["ch"]}
        return {
            "today": agg("WHERE date(created_at) = date('now')"),
            "month": agg("WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"),
            "total": agg(""),
        }


def remove_asset(code: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM assets WHERE code=?", (code.upper().strip(),))
        return cur.rowcount > 0


def get_prompts(instrument_type: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM event_prompts WHERE instrument_type=? ORDER BY event_type,id",
                            (instrument_type.upper(),)).fetchall()
        return [dict(r) for r in rows]


def event_exists(fingerprint: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM events WHERE fingerprint=?", (fingerprint,)).fetchone() is not None


def save_event(event: dict[str, Any]) -> int | None:
    """Возвращает rowid новой записи или None, если событие уже есть (дубликат fingerprint)."""
    action_required = event.get("action_required")
    with connect() as conn:
        try:
            cur = conn.execute("""
            INSERT OR IGNORE INTO events(fingerprint,asset_code,event_type,event_date,title,summary_ru,impact_ru,
              source_url,source_name,confidence,raw_json,severity,underlying_ticker,publication_date,
              effective_date,status,action_required,recommended_action,key_parameters_json,additional_sources_json,
              is_test)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (event["fingerprint"], event["asset_code"], event["event_type"], event.get("event_date"),
                  event["title"], event.get("summary_ru", ""), event.get("impact_ru"), event.get("source_url", ""),
                  event.get("source_name"), float(event.get("confidence", 0)), json.dumps(event, ensure_ascii=False),
                  event.get("severity"), event.get("underlying_ticker"), event.get("publication_date"),
                  event.get("effective_date"), event.get("status"),
                  None if action_required is None else int(bool(action_required)),
                  event.get("recommended_action"), event.get("key_parameters_json"),
                  event.get("additional_sources_json"), int(bool(event.get("is_test", 0)))))
        except sqlite3.IntegrityError:
            # Дубликат гасится OR IGNORE, поэтому сюда попадают только нарушения других ограничений.
            log.exception("IntegrityError при записи события %s", event.get("fingerprint", "")[:16])
            return None
        return cur.lastrowid if cur.rowcount > 0 else None


def mark_sent(fingerprint: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE events SET sent_at=CURRENT_TIMESTAMP WHERE fingerprint=?", (fingerprint,))


def unsent_events(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT rowid AS id, * FROM events WHERE sent_at IS NULL AND is_test=0 "
                            "ORDER BY detected_at LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def recent_events(limit: int = 20, asset_code: str | None = None,
                  test_only: bool = False) -> list[dict[str, Any]]:
    """Рабочие события (is_test=0); test_only=True — только тестовые.
    Фильтр по активу выполняется в SQL, до форматирования."""
    where = ["is_test=1" if test_only else "is_test=0"]
    params: list = []
    if asset_code:
        where.append("asset_code=?")
        params.append(asset_code.upper())
    with connect() as conn:
        rows = conn.execute(
            f"SELECT rowid AS id, * FROM events WHERE {' AND '.join(where)} "
            "ORDER BY detected_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [dict(r) for r in rows]


def similar_event_exists(asset_code: str, event_type: str, days: int = 14) -> bool:
    """Семантическая дедупликация: событие того же типа по тому же активу уже есть
    в окне N дней — новые статьи о том же (пересказы, обсуждения) не плодят уведомления."""
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE asset_code=? AND event_type=? AND is_test=0 "
            "AND detected_at >= datetime('now', ?) LIMIT 1",
            (asset_code.upper(), event_type, f"-{int(days)} days")).fetchone() is not None


def last_event_for_asset(asset_code: str) -> dict[str, Any] | None:
    events = recent_events(limit=1, asset_code=asset_code)
    return events[0] if events else None


def get_event(event_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT rowid AS id, * FROM events WHERE rowid=?", (event_id,)).fetchone()
        return dict(row) if row else None


def delete_event(event_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM events WHERE rowid=?", (event_id,)).rowcount > 0


def delete_test_events() -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM events WHERE is_test=1").rowcount


def events_since_hours(hours: int = 24) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT rowid AS id, * FROM events WHERE is_test=0 AND detected_at >= datetime('now', ?) "
            "ORDER BY detected_at DESC", (f"-{int(hours)} hours",)).fetchall()
        return [dict(r) for r in rows]


def last_ok_run() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scan_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def list_scan_runs(limit: int = 15) -> list[dict[str, Any]]:
    """История проверок (для /scans), новые первыми."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def recent_articles(limit: int = 15, source_like: str | None = None) -> list[dict[str, Any]]:
    """Последние обработанные публикации (для /articles): что бот смотрел и чем закончилось."""
    where, params = "", []
    if source_like:
        where = "WHERE source LIKE ?"
        params.append(f"%{source_like}%")
    with connect() as conn:
        rows = conn.execute(
            f"SELECT url, title, source, status, llm_result, best_asset, best_score, processed_at "
            f"FROM processed_articles {where} ORDER BY processed_at DESC LIMIT ?",
            (*params, limit)).fetchall()
        return [dict(r) for r in rows]


def daily_summary_data(hours: int = 24) -> dict[str, Any]:
    """Данные для ежедневного саммари: что было проанализировано за период,
    включая публикации, не прошедшие фильтры."""
    since = f"-{int(hours)} hours"
    with connect() as conn:
        status_counts = {r["s"]: r["n"] for r in conn.execute(
            "SELECT COALESCE(status,'fetched') s, COUNT(*) n FROM processed_articles "
            "WHERE processed_at >= datetime('now', ?) GROUP BY 1", (since,)).fetchall()}
        llm_seen = [dict(r) for r in conn.execute(
            "SELECT title, source, status, llm_result, best_asset FROM processed_articles "
            "WHERE processed_at >= datetime('now', ?) "
            "AND status IN ('llm_analyzed','rejected','event_created') "
            "ORDER BY processed_at DESC LIMIT 60", (since,)).fetchall()]
        near_miss = [dict(r) for r in conn.execute(
            "SELECT title, best_asset, best_score FROM processed_articles "
            "WHERE processed_at >= datetime('now', ?) AND status='unmatched' "
            "AND best_score >= 0.4 ORDER BY best_score DESC LIMIT 15", (since,)).fetchall()]
        scans = conn.execute("SELECT COUNT(*) FROM scan_runs WHERE started_at >= datetime('now', ?)",
                             (since,)).fetchone()[0]
    return {
        "status_counts": status_counts,
        "llm_seen": llm_seen,
        "near_miss": near_miss,
        "events": events_since_hours(hours),
        "scans": scans,
        "cost_today": cost_stats()["today"],
    }


def daily_report_rows(hours: int = 24) -> list[dict[str, Any]]:
    """Публикации за период, у которых есть кандидат-актив: всё, что прошло
    keyword-фильтр и было сопоставлено хотя бы предположительно — от слабых
    кандидатов до созданных событий. Основа дневного Excel-отчёта."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.url, p.title, p.source, COALESCE(p.status,'fetched') status, p.llm_result, "
            "p.best_asset, p.best_score, p.published_at, p.processed_at, p.pending_llm, "
            "p.llm_event_type, p.llm_summary, a.name asset_name, a.instrument_type "
            "FROM processed_articles p LEFT JOIN assets a ON a.code = p.best_asset "
            "WHERE p.processed_at >= datetime('now', ?) AND p.best_asset IS NOT NULL "
            "ORDER BY p.best_asset, p.processed_at DESC",
            (f"-{int(hours)} hours",)).fetchall()
        return [dict(r) for r in rows]


def articles_by_source(hours: int = 24) -> list[tuple[str, int]]:
    """Сводка: сколько новых публикаций обработано по источникам за период."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(source,'?') s, COUNT(*) n FROM processed_articles "
            "WHERE processed_at >= datetime('now', ?) GROUP BY 1 ORDER BY n DESC",
            (f"-{int(hours)} hours",)).fetchall()
        return [(r["s"], r["n"]) for r in rows]


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def start_scan_run() -> int:
    with connect() as conn:
        return conn.execute("INSERT INTO scan_runs DEFAULT VALUES").lastrowid


def finish_scan_run(run_id: int, status: str, assets_scanned: int, new_events: int,
                    error: str | None = None, errors: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE scan_runs SET finished_at=CURRENT_TIMESTAMP, status=?, assets_scanned=?, new_events=?, error=?, errors=? WHERE id=?",
            (status, assets_scanned, new_events, error, errors, run_id))


def db_stats() -> dict[str, Any]:
    path = Path(settings.db_path)
    stats: dict[str, Any] = {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
    with connect() as conn:
        for table in ("assets", "sources", "event_prompts", "events", "scan_runs"):
            stats[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        row = conn.execute(
            "SELECT started_at, finished_at, status, new_events FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        stats["last_run"] = dict(row) if row else None
    return stats
