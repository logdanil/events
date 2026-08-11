from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin_telegram_id: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    # LLM: все обращения к модели идут через app.llm.LLMProvider.
    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini")
    model: str = os.getenv("MODEL", "gemini-3.5-flash")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    # Tavily — только резервный поиск (раз в сутки или /fullscan).
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
    db_path: str = os.getenv("DB_PATH", "./data/monitor.db")
    timezone: str = os.getenv("TIMEZONE", "Europe/Amsterdam")
    max_search_results: int = int(os.getenv("MAX_SEARCH_RESULTS", "6"))
    lookback_days: int = int(os.getenv("LOOKBACK_DAYS", "3"))
    # Email-дублирование уведомлений (необязательно).
    # Приоритет транспортов: Brevo → SendGrid → SMTP. HTTP API нужны, потому что
    # Railway блокирует исходящий SMTP; SMTP работает при локальном запуске.
    brevo_api_key: str = os.getenv("BREVO_API_KEY", "")
    sendgrid_api_key: str = os.getenv("SENDGRID_API_KEY", "")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    email_from: str = os.getenv("EMAIL_FROM", "")
    # SEC и ряд бирж требуют User-Agent с контактом.
    user_agent: str = os.getenv("USER_AGENT", "corporate-events-monitor/1.0 (admin contact in .env)")
    llm_batch_size: int = int(os.getenv("LLM_BATCH_SIZE", "20"))

settings = Settings()
