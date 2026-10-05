from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import Game, GameEvent, Source, Subscription, User
from app.services.ai import OpenRouterAI


UTC = timezone.utc


def dt_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except Exception:
        return None


def fingerprint(game_id: int | None, title: str, url: str) -> str:
    return hashlib.sha256(f"{game_id}|{title.strip().lower()}|{url.strip()}".encode()).hexdigest()


class EventCollector:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession], ai: OpenRouterAI | None = None):
        self.settings = settings
        self.session_factory = session_factory
        self.ai = ai
        self.http = httpx.AsyncClient(timeout=settings.event_source_timeout, follow_redirects=True, headers={"User-Agent": "ShiroEventEngine/1.0"})

    async def close(self):
        await self.http.aclose()

    async def run_once(self) -> list[GameEvent]:
        created: list[GameEvent] = []
        async with self.session_factory() as session:
            sources = (await session.execute(select(Source).where(Source.active.is_(True)))).scalars().all()
            for source in sources:
                try:
                    events = await self.collect_source(session, source)
                    created.extend(events)
                    source.last_checked = datetime.now(UTC)
                except Exception as exc:
                    print(f"[events] source {source.id} failed: {exc}")
            await session.commit()
        return created

    async def collect_source(self, session: AsyncSession, source: Source) -> list[GameEvent]:
        game_name = None
        if source.game_id:
            game = (await session.execute(select(Game).where(Game.id == source.game_id))).scalar_one_or_none()
            game_name = game.name if game else None
        if source.kind == "rss":
            return await self._rss(session, source, game_name)
        if source.kind == "api":
            return await self._api(session, source, game_name)
        return await self._web(session, source, game_name)

    async def _rss(self, session: AsyncSession, source: Source, game_name: str | None) -> list[GameEvent]:
        r = await self.http.get(source.url)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        created = []
        for entry in feed.entries[:50]:
            title = str(entry.get("title", "")).strip()
            link = str(entry.get("link", source.url)).strip()
            if not title:
                continue
            fp = fingerprint(source.game_id, title, link)
            exists = (await session.execute(select(GameEvent).where(GameEvent.fingerprint == fp))).scalar_one_or_none()
            if exists:
                continue
            published = dt_parse(entry.get("published") or entry.get("updated"))
            summary = BeautifulSoup(str(entry.get("summary", "")), "html.parser").get_text(" ", strip=True)[:1500]
            ev = GameEvent(
                game_id=source.game_id,
                source_id=source.id,
                title=title[:500],
                event_type="news",
                starts_at=None,
                ends_at=None,
                url=link,
                summary=summary,
                confidence="source",
                fingerprint=fp,
            )
            session.add(ev)
            created.append(ev)
        return created

    async def _api(self, session: AsyncSession, source: Source, game_name: str | None) -> list[GameEvent]:
        r = await self.http.get(source.url)
        r.raise_for_status()
        text = r.text[:30000]
        return await self._extract_events(session, source, game_name, text, source.url)

    async def _web(self, session: AsyncSession, source: Source, game_name: str | None) -> list[GameEvent]:
        r = await self.http.get(source.url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        text = soup.get_text("\n", strip=True)
        text = text[:30000]
        return await self._extract_events(session, source, game_name, text, source.url)

    async def _extract_events(self, session: AsyncSession, source: Source, game_name: str | None, source_text: str, source_url: str) -> list[GameEvent]:
        if not self.ai:
            fp = fingerprint(source.game_id, f"source update {datetime.now(UTC).date()}", source_url)
            exists = (await session.execute(select(GameEvent).where(GameEvent.fingerprint == fp))).scalar_one_or_none()
            if exists:
                return []
            ev = GameEvent(
                game_id=source.game_id,
                source_id=source.id,
                title=f"Actualización: {game_name or source.name}",
                event_type="news",
                url=source_url,
                summary=source_text[:1500],
                confidence="source",
                fingerprint=fp,
            )
            session.add(ev)
            return [ev]

        prompt = f"""
Extrae novedades de videojuegos del contenido siguiente.
Juego asociado: {game_name or 'desconocido'}.
Fuente: {source_url}
Pista de la fuente: {source.prompt_hint}

Devuelve JSON con esta forma exacta:
{{"events":[{{"title":"...","event_type":"news|event|skin|patch|collab|code|release|maintenance|rumor","starts_at":"ISO-8601 o null","ends_at":"ISO-8601 o null","summary":"...","confidence":"confirmed|likely|rumor|unknown"}}]}}

Reglas:
- No inventes fechas.
- Extrae solo información respaldada por el texto.
- Si no hay novedades claras, devuelve events vacío.
- No incluyas la URL dentro de summary.

CONTENIDO:
{source_text}
"""
        data = await self.ai.structured_extract(prompt)
        items = data.get("events", []) if isinstance(data, dict) else []
        created = []
        for item in items[:20]:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            title = str(item["title"]).strip()
            fp = fingerprint(source.game_id, title, source.url)
            exists = (await session.execute(select(GameEvent).where(GameEvent.fingerprint == fp))).scalar_one_or_none()
            if exists:
                continue
            starts = _iso(item.get("starts_at"))
            ends = _iso(item.get("ends_at"))
            ev = GameEvent(
                game_id=source.game_id,
                source_id=source.id,
                title=title[:500],
                event_type=str(item.get("event_type", "news"))[:50],
                starts_at=starts,
                ends_at=ends,
                url=source.url,
                summary=str(item.get("summary", ""))[:2500],
                confidence=str(item.get("confidence", "unknown"))[:20],
                fingerprint=fp,
            )
            session.add(ev)
            created.append(ev)
        return created


def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None
