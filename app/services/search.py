from __future__ import annotations

import httpx
from app.config import Settings


async def simple_web_search(settings: Settings, query: str) -> str:
    # Uses OpenRouter's server-side web search when enabled. The actual web
    # retrieval is performed by OpenRouter; this function asks the model to
    # perform the search and return a concise sourced summary.
    if not settings.openrouter_enable_web_search:
        return "Búsqueda web desactivada."
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": "Busca información actual. Distingue hechos de rumores y devuelve una respuesta breve con fuentes."},
            {"role": "user", "content": query},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
        "tools": [{"type": "openrouter:web_search"}],
        "tool_choice": "auto",
    }
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"}
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_site_name:
        headers["X-OpenRouter-Title"] = settings.openrouter_site_name
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{settings.openrouter_base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return (data.get("choices", [{}])[0].get("message", {}).get("content") or "No encontré información.").strip()

async def discover_sources(settings: Settings, game_name: str) -> dict:
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": "Find legitimate current information sources for a video game. Prefer official developer/publisher sites, official community announcements, reputable RSS feeds and public APIs. Return JSON only."},
            {"role": "user", "content": f"For the game '{game_name}', find up to 12 sources useful for events, skins, patches, collaborations, releases and news. Return {{\"sources\":[{{\"name\":\"...\",\"url\":\"https://...\",\"kind\":\"web|rss|api\",\"reason\":\"...\"}}]}}. Do not invent URLs."},
        ],
        "temperature": 0.1,
        "max_tokens": 1600,
        "tools": [{"type": "openrouter:web_search"}],
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{settings.openrouter_base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
        import json
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
