from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def csv_ints(value: str | None) -> set[int]:
    if not value:
        return set()
    out: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            out.add(int(item))
    return out


@dataclass(slots=True)
class Settings:
    bot_token: str
    group_id: int
    admin_ids: set[int]
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    openrouter_site_url: str
    openrouter_site_name: str
    openrouter_enable_web_search: bool
    openrouter_temperature: float
    openrouter_max_tokens: int
    database_url: str
    webapp_host: str
    webapp_port: int
    webapp_base_url: str
    webapp_admin_token: str
    payment_webhook_secret: str
    parser_webhook_secret: str
    webhook_max_skew_seconds: int
    transfermovil_destination: str
    transfermovil_receiving_account: str
    transfermovil_receiving_phone: str
    transfermovil_bank_name: str
    xp_per_valid_message: int
    xp_daily_cap: int
    xp_cooldown_seconds: int
    star_amount_step: int
    star_per_step: int
    event_poll_minutes: int
    event_source_timeout: int
    event_ai_extract_model: str
    coupon_milestones: str
    admin_notify_unmatched_payments: bool

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/shiro.db")
        # Supabase/PostgreSQL URLs commonly arrive without the async driver suffix.
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return cls(
            bot_token=os.environ.get("BOT_TOKEN", ""),
            group_id=int(os.environ.get("GROUP_ID", "0")),
            admin_ids=csv_ints(os.environ.get("ADMIN_IDS")),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            openrouter_model=os.environ.get("OPENROUTER_MODEL", ""),
            openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            openrouter_site_url=os.environ.get("OPENROUTER_SITE_URL", ""),
            openrouter_site_name=os.environ.get("OPENROUTER_SITE_NAME", "Shiro Synthesis Two"),
            openrouter_enable_web_search=os.environ.get("OPENROUTER_ENABLE_WEB_SEARCH", "true").lower() == "true",
            openrouter_temperature=float(os.environ.get("OPENROUTER_TEMPERATURE", "0.95")),
            openrouter_max_tokens=int(os.environ.get("OPENROUTER_MAX_TOKENS", "900")),
            database_url=db_url,
            webapp_host=os.environ.get("WEBAPP_HOST", "0.0.0.0"),
            webapp_port=int(os.environ.get("WEBAPP_PORT", "8080")),
            webapp_base_url=os.environ.get("WEBAPP_BASE_URL", "http://localhost:8080"),
            webapp_admin_token=os.environ.get("WEBAPP_ADMIN_TOKEN", ""),
            payment_webhook_secret=os.environ.get("PAYMENT_WEBHOOK_SECRET", ""),
            parser_webhook_secret=os.environ.get("PARSER_WEBHOOK_SECRET", ""),
            webhook_max_skew_seconds=int(os.environ.get("WEBHOOK_MAX_SKEW_SECONDS", "300")),
            transfermovil_destination=os.environ.get("TRANSFERMOVIL_DESTINATION", ""),
            transfermovil_receiving_account=os.environ.get("TRANSFERMOVIL_RECEIVING_ACCOUNT", ""),
            transfermovil_receiving_phone=os.environ.get("TRANSFERMOVIL_RECEIVING_PHONE", ""),
            transfermovil_bank_name=os.environ.get("TRANSFERMOVIL_BANK_NAME", "Transfermóvil"),
            xp_per_valid_message=int(os.environ.get("XP_PER_VALID_MESSAGE", "8")),
            xp_daily_cap=int(os.environ.get("XP_DAILY_CAP", "120")),
            xp_cooldown_seconds=int(os.environ.get("XP_COOLDOWN_SECONDS", "18")),
            star_amount_step=int(os.environ.get("STAR_AMOUNT_STEP", "10")),
            star_per_step=int(os.environ.get("STAR_PER_STEP", "1")),
            event_poll_minutes=int(os.environ.get("EVENT_POLL_MINUTES", "30")),
            event_source_timeout=int(os.environ.get("EVENT_SOURCE_TIMEOUT", "20")),
            event_ai_extract_model=os.environ.get("EVENT_AI_EXTRACT_MODEL", ""),
            coupon_milestones=os.environ.get("COUPON_MILESTONES", "100:2,250:3,500:5,1000:7,2000:10"),
            admin_notify_unmatched_payments=os.environ.get("ADMIN_NOTIFY_UNMATCHED_PAYMENTS", "true").lower() == "true",
        )

    def validate(self) -> None:
        missing = []
        for name, value in {
            "BOT_TOKEN": self.bot_token,
            "OPENROUTER_API_KEY": self.openrouter_api_key,
            "OPENROUTER_MODEL": self.openrouter_model,
            "WEBAPP_ADMIN_TOKEN": self.webapp_admin_token,
            "PARSER_WEBHOOK_SECRET": self.parser_webhook_secret,
        }.items():
            if not value:
                missing.append(name)
        if not self.admin_ids:
            missing.append("ADMIN_IDS")
        if missing:
            raise RuntimeError(f"Faltan variables obligatorias: {', '.join(missing)}")
        Path(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
