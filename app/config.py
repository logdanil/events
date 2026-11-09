from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default=os.getenv("TELEGRAM_BOT_TOKEN", ""), repr=False)
    admin_telegram_id: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    # LLM: все обращения к модели идут через app.llm.LLMProvider.
    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini")
    model: str = os.getenv("MODEL", "gemini-3.5-flash")
    google_api_key: str = field(default=os.getenv("GOOGLE_API_KEY", ""), repr=False)
    # Tavily — только резервный поиск (раз в сутки или /fullscan).
    tavily_api_key: str = field(default=os.getenv("TAVILY_API_KEY", ""), repr=False)
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
    db_path: str = os.getenv("DB_PATH", "./data/monitor.db")
    database_url: str = field(default=os.getenv("DATABASE_URL", ""), repr=False)
    timezone: str = os.getenv("TIMEZONE", "Europe/Amsterdam")
    max_search_results: int = int(os.getenv("MAX_SEARCH_RESULTS", "6"))
    lookback_days: int = int(os.getenv("LOOKBACK_DAYS", "3"))
    # Email-дублирование уведомлений (необязательно).
    # Приоритет транспортов: Gmail API → SMTP2GO → Brevo → SendGrid → SMTP.
    # HTTP API нужны, потому что Railway блокирует исходящий SMTP.
    gmail_client_id: str = os.getenv("GMAIL_CLIENT_ID", "")
    gmail_client_secret: str = field(default=os.getenv("GMAIL_CLIENT_SECRET", ""), repr=False)
    gmail_refresh_token: str = field(default=os.getenv("GMAIL_REFRESH_TOKEN", ""), repr=False)
    smtp2go_api_key: str = field(default=os.getenv("SMTP2GO_API_KEY", ""), repr=False)
    brevo_api_key: str = field(default=os.getenv("BREVO_API_KEY", ""), repr=False)
    sendgrid_api_key: str = field(default=os.getenv("SENDGRID_API_KEY", ""), repr=False)
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = field(default=os.getenv("SMTP_PASSWORD", ""), repr=False)
    email_from: str = os.getenv("EMAIL_FROM", "")
    # SEC и ряд бирж требуют User-Agent с контактом.
    user_agent: str = os.getenv("USER_AGENT", "corporate-events-monitor/1.0 (admin contact in .env)")
    # Большинство IR-сайтов и бирж (Nasdaq, JD, CME, Euronext…) сбрасывают соединение
    # с «ботовским» User-Agent, но нормально отвечают браузерному. SEC — наоборот,
    # требует контакт в UA. Поэтому для HTML/IR используется браузерный UA.
    browser_user_agent: str = os.getenv(
        "BROWSER_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    llm_batch_size: int = int(os.getenv("LLM_BATCH_SIZE", "20"))

settings = Settings()

# Секреты скрыты из repr(Settings) (traceback, вывод pytest) и маскируются в логах и сообщениях.
SECRET_FIELDS = tuple(f.name for f in fields(Settings) if not f.repr)


def redact(text: str, source: Settings | None = None) -> str:
    """Заменяет значения секретов в тексте на ***."""
    source = source or settings
    for name in SECRET_FIELDS:
        value = getattr(source, name)
        if value and len(value) >= 6:
            text = text.replace(value, "***")
    return text
