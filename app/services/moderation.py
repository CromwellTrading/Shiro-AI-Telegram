from __future__ import annotations

import re
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User

URL_RE = re.compile(r"(?i)(https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|discord\.com/invite/)")


def contains_link(text: str) -> bool:
    return bool(URL_RE.search(text or ""))


def bot_invocation(text: str) -> bool:
    t = (text or "").lower()
    return any(name in t for name in ("shiro", "sst", "synthesis two"))


async def is_blacklisted(session: AsyncSession, telegram_id: int) -> bool:
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    return bool(user and user.is_blacklisted)


def media_explanation(content_type: str) -> str:
    if content_type in {"photo", "video", "animation"}:
        return (
            "No afirmes haber visto el contenido visual. En este proyecto Shiro recibe el evento de Telegram, "
            "pero el módulo de visión no está habilitado. Di de forma natural que no puedes ver la imagen/video todavía "
            "y pide una descripción si hace falta."
        )
    return ""
