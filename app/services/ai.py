from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings


class OpenRouterAI:
    def __init__(self, settings: Settings, personality: str):
        self.settings = settings
        self.personality = personality
        self.client = httpx.AsyncClient(timeout=60)

    async def close(self):
        await self.client.aclose()

    def _tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.settings.openrouter_enable_web_search:
            # OpenRouter server-side tools. Availability depends on the selected model/route.
            # Search finds current sources; fetch allows the model to read a page in depth.
            tools.append({"type": "openrouter:web_search"})
            tools.append({"type": "openrouter:web_fetch"})
            tools.append({"type": "openrouter:datetime"})
        return tools

    async def chat(
        self,
        user_text: str,
        context: str,
        private: bool = False,
        extra_system: str = "",
    ) -> str:
        system = self.personality + "\n\n" + extra_system
        if private:
            system += "\n\nCONTEXTO: Estás en chat privado. Puedes ser más relajada."
        payload: dict[str, Any] = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "system", "content": f"CONTEXTO RECUPERADO:\n{context}"},
                {"role": "user", "content": user_text},
            ],
            "temperature": self.settings.openrouter_temperature,
            "max_tokens": self.settings.openrouter_max_tokens,
        }
        if self._tools():
            payload["tools"] = self._tools()
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        if self.settings.openrouter_site_name:
            headers["X-OpenRouter-Title"] = self.settings.openrouter_site_name

        response = await self.client.post(
            f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        if response.status_code in (400, 404, 422) and payload.get("tools"):
            # Some free models/routes do not expose tools. Fall back to a plain completion.
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            response = await self.client.post(
                f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            return "No pude construir una respuesta esta vez."
        return content.strip() or "SKIP"

    async def web_search(self, prompt: str, json_mode: bool = False) -> Any:
        payload = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": "Usa la búsqueda web cuando la necesites. Distingue fuentes oficiales de comunidades. No inventes URLs. Devuelve solo lo solicitado."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1400,
            "tools": [{"type": "openrouter:web_search"}, {"type": "openrouter:web_fetch"}, {"type": "openrouter:datetime"}],
            "tool_choice": "auto",
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}", "Content-Type": "application/json"}
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        if self.settings.openrouter_site_name:
            headers["X-OpenRouter-Title"] = self.settings.openrouter_site_name
        response = await self.client.post(f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
        if json_mode:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {}
        return content.strip()

    async def structured_extract(self, prompt: str, model: str | None = None) -> Any:
        payload = {
            "model": model or self.settings.event_ai_extract_model or self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": "Devuelve exclusivamente JSON válido. No inventes información ausente."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        response = await self.client.post(
            f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"].get("content") or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
