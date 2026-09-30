import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from mtm_hbl.config import Settings


class OAuthStateStore:
    def __init__(self) -> None:
        self._valid_states: dict[str, tuple[str, float]] = {}

    def create(self, binding: str) -> str:
        now = time.monotonic()
        self._valid_states = {key: value for key, value in self._valid_states.items() if value[1] > now}
        state = secrets.token_urlsafe(32)
        self._valid_states[state] = (binding, now + 600)
        return state

    def consume(self, state: str, binding: str) -> bool:
        saved = self._valid_states.get(state)
        if saved is None:
            return False
        if saved[1] <= time.monotonic():
            self._valid_states.pop(state, None)
            return False
        if not binding or not secrets.compare_digest(binding.encode(), saved[0].encode()):
            return False
        self._valid_states.pop(state, None)
        return True


class ClickUpOAuthToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"


class LocalTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, token: ClickUpOAuthToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(token.model_dump(), handle, indent=2)
        self.path.chmod(0o600)

    def load(self) -> ClickUpOAuthToken | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return ClickUpOAuthToken.model_validate(data)


class ClickUpOAuthClient:
    authorization_url = "https://app.clickup.com/api"
    token_url = "https://api.clickup.com/api/v2/oauth/token"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.clickup_client_id,
                "redirect_uri": self.settings.clickup_redirect_uri,
                "state": state,
            }
        )
        return f"{self.authorization_url}?{query}"

    async def exchange_code(self, code: str) -> ClickUpOAuthToken:
        payload = {
            "client_id": self.settings.clickup_client_id,
            "client_secret": self.settings.clickup_client_secret,
            "code": code,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self.token_url, json=payload)
            response.raise_for_status()
            data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            raise ValueError("ClickUp token response did not include access_token.")
        return ClickUpOAuthToken(access_token=access_token)
