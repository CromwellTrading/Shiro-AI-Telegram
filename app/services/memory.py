from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ChatMessage, Memory, User, recent_messages


def format_user(user: User) -> str:
    username = f"@{user.username}" if user.username else user.full_name
    return (
        f"Usuario: {username} (Telegram ID {user.telegram_id})\n"
        f"Nivel: {user.level} | XP: {user.xp} | Mensajes válidos: {user.valid_messages}\n"
        f"Estrellas: {user.stars}\n"
        f"Memoria: {user.memory or 'sin memoria persistente'}"
    )


async def build_context(session: AsyncSession, user: User, chat_id: int) -> str:
    msgs = await recent_messages(session, chat_id, 18)
    memories = (await session.execute(
        select(Memory).where(Memory.user_id == user.id).order_by(desc(Memory.importance), desc(Memory.updated_at)).limit(12)
    )).scalars().all()
    mem_text = "\n".join(f"- [{m.importance}] {m.content}" for m in memories)
    history = "\n".join(
        f"{m.role}: {m.text[:500]}" for m in msgs if m.text
    )
    return f"{format_user(user)}\nMemorias:\n{mem_text or '- ninguna'}\n\nMensajes recientes:\n{history or '- ninguno'}"


async def remember_message(session: AsyncSession, chat_id: int, user: User, telegram_message_id: int, text: str, content_type: str = "text"):
    session.add(ChatMessage(
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        user_id=user.id,
        role="user",
        text=text,
        content_type=content_type,
    ))


async def add_memory(session: AsyncSession, user: User, content: str, importance: str = "normal"):
    session.add(Memory(user_id=user.id, content=content, importance=importance))
