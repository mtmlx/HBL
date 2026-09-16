from __future__ import annotations

from typing import Any

import httpx

from mtm_hbl.config import Settings, get_settings


class GammaClient:
    """Minimal Gamma API client for presentation generation workflows."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.api_key = api_key if api_key is not None else self.settings.gamma_api_key
        self.base_url = (base_url or self.settings.gamma_api_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("GAMMA_API_KEY is not configured.")
        return {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
        }

    async def list_themes(self) -> list[dict[str, Any]]:
        """Return the themes visible to the configured Gamma API key."""
        data = await self._request("GET", "/themes")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        themes = data.get("themes") if isinstance(data, dict) else None
        if isinstance(themes, list):
            return [item for item in themes if isinstance(item, dict)]
        return []

    async def create_generation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit a Gamma generation request.

        The payload shape is intentionally left caller-controlled because Gamma
        supports multiple input modes and export options.
        """
        return await self._request("POST", "/generations", json=payload)

    async def get_generation(self, generation_id: str) -> dict[str, Any]:
        """Fetch a Gamma generation by id."""
        if not generation_id:
            raise ValueError("generation_id is required.")
        return await self._request("GET", f"/generations/{generation_id}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method, url, headers=self.headers, json=json)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = _response_error_message(response)
            raise ValueError(f"Gamma API request failed: {response.status_code} {message}") from exc
        return response.json()


def _response_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            value = data.get(key)
            if value:
                return str(value)
    return str(data)
