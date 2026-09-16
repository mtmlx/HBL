from typing import Any
import asyncio

import httpx

from mtm_hbl.config import AppConfig, Settings
from mtm_hbl.models.clickup import ClickUpAttachment, ClickUpCustomField, ClickUpTaskData, ClickUpUser


class ClickUpClient:
    def __init__(self, settings: Settings, access_token: str) -> None:
        self.settings = settings
        self.access_token = access_token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def get_task(self, task_id: str) -> ClickUpTaskData:
        url = f"{self.settings.clickup_api_base_url}/task/{task_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
        return self._parse_task(data)

    async def post_comment(
        self,
        task_id: str,
        comment_text: str,
        *,
        assignee_id: str = "",
        notify_all: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.settings.clickup_api_base_url}/task/{task_id}/comment"
        payload: dict[str, Any] = {"comment_text": comment_text}
        if assignee_id:
            payload["assignee"] = int(assignee_id) if assignee_id.isdigit() else assignee_id
        if notify_all:
            payload["notify_all"] = True
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def update_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        url = f"{self.settings.clickup_api_base_url}/task/{task_id}"
        payload = {"status": status}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def upload_attachment(self, task_id: str, path: str) -> dict[str, Any]:
        url = f"{self.settings.clickup_api_base_url}/task/{task_id}/attachment"
        with open(path, "rb") as handle:
            files = {"attachment": handle}
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, headers=self.headers, files=files)
                response.raise_for_status()
                return response.json()

    async def upload_attachment_to_custom_field(
        self,
        task_id: str,
        field_id: str,
        path: str,
    ) -> dict[str, Any]:
        await self._clear_attachment_custom_field(task_id, field_id)
        workspace_id = await self.get_workspace_id()
        url = f"{self._api_v3_base_url}/workspaces/{workspace_id}/custom_fields/{field_id}/attachments"
        with open(path, "rb") as handle:
            files = {"attachment": handle}
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, headers=self.headers, files=files)
                response.raise_for_status()
                attachment = response.json()

        attachment_id = str(attachment.get("id", ""))
        if not attachment_id:
            raise ValueError("ClickUp attachment upload did not return an attachment id.")

        url = f"{self.settings.clickup_api_base_url}/task/{task_id}/field/{field_id}"
        payload = {"value": {"add": [attachment_id]}}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
        return attachment

    async def verify_attachment_custom_field(
        self,
        task_id: str,
        field_id: str,
        expected_filename: str,
        *,
        attempts: int = 5,
        delay_seconds: float = 1.0,
    ) -> None:
        for attempt in range(attempts):
            task = await self.get_task(task_id)
            field = task.field_by_id(field_id)
            value = field.value if field else None
            if _attachment_field_contains(value, expected_filename):
                return
            if attempt < attempts - 1:
                await asyncio.sleep(delay_seconds)
        raise ValueError(
            f"ClickUp field {field_id} does not contain uploaded attachment {expected_filename}."
        )

    async def _clear_attachment_custom_field(self, task_id: str, field_id: str) -> None:
        task = await self.get_task(task_id)
        field = task.field_by_id(field_id)
        existing = field.value if field else None
        if not isinstance(existing, list):
            return
        attachment_ids = [
            str(item.get("id", ""))
            for item in existing
            if isinstance(item, dict) and item.get("id")
        ]
        if not attachment_ids:
            return
        url = f"{self.settings.clickup_api_base_url}/task/{task_id}/field/{field_id}"
        payload = {"value": {"rem": attachment_ids}}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()

    async def get_workspace_id(self) -> str:
        if self.settings.clickup_workspace_id:
            return self.settings.clickup_workspace_id
        url = f"{self.settings.clickup_api_base_url}/team"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            teams = response.json().get("teams", [])
        if not teams:
            raise ValueError("ClickUp token did not return any workspaces.")
        return str(teams[0]["id"])

    @property
    def _api_v3_base_url(self) -> str:
        return self.settings.clickup_api_base_url.replace("/api/v2", "/api/v3").rstrip("/")

    def extract_configured_fields(self, task: ClickUpTaskData, app_config: AppConfig) -> dict[str, Any]:
        field_config = app_config.clickup_fields
        owner_country_name = field_config["owner_country"]["field_name"]
        hbl_field_id = field_config["hbl_number"]["field_id"]

        owner_country = task.field_by_name(owner_country_name)
        hbl_number = task.field_by_id(hbl_field_id)

        values: dict[str, Any] = {
            "task_id": task.id,
            "owner_country": self._stringify_field_value(owner_country.value if owner_country else None),
            "hbl_number": self._stringify_field_value(hbl_number.value if hbl_number else None),
        }

        for key, spec in field_config.get("known_fields", {}).items():
            field = self._find_configured_field(task, spec)
            values[key] = self._stringify_field_value(field.value if field else None)
            if not values[key] and "default" in spec:
                values[key] = str(spec["default"])

        emails = []
        for field_name in field_config.get("consignee_emails", {}).get("fields", []):
            field = task.field_by_name(field_name)
            value = self._stringify_field_value(field.value if field else None)
            if value:
                emails.append(value)
        values["consignee_emails"] = emails
        return values

    @staticmethod
    def _find_configured_field(
        task: ClickUpTaskData, spec: dict[str, Any]
    ) -> ClickUpCustomField | None:
        if "field_id" in spec:
            field = task.field_by_id(spec["field_id"])
            if field:
                return field
        names = []
        if "field_name" in spec:
            names.append(spec["field_name"])
        names.extend(spec.get("field_names", []))
        for name in names:
            field = task.field_by_name(name)
            if field:
                return field
        return None

    @staticmethod
    def _parse_task(data: dict[str, Any]) -> ClickUpTaskData:
        custom_fields = [
            ClickUpCustomField(
                id=str(field.get("id", "")),
                name=str(field.get("name", "")),
                value=field.get("value"),
            )
            for field in data.get("custom_fields", [])
        ]
        attachments = [
            ClickUpAttachment(
                id=str(attachment.get("id", "")),
                title=str(attachment.get("title", "")),
                url=str(attachment.get("url", "")),
                extension=str(attachment.get("extension", "")),
            )
            for attachment in data.get("attachments", [])
        ]
        assignees = [
            ClickUpUser(
                id=str(user.get("id", "")),
                username=str(user.get("username", "") or user.get("name", "")),
                email=str(user.get("email", "")),
            )
            for user in data.get("assignees", [])
            if isinstance(user, dict)
        ]
        return ClickUpTaskData(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            status=str((data.get("status") or {}).get("status", "")),
            description=str(data.get("description", "") or data.get("text_content", "") or ""),
            assignees=assignees,
            custom_fields=custom_fields,
            attachments=attachments,
        )

    @staticmethod
    def _stringify_field_value(value: object | None) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            for key in ("name", "label", "value", "text", "date"):
                if key in value:
                    return str(value[key]).strip()
            return str(value)
        if isinstance(value, list):
            return ", ".join(str(item) for item in value).strip()
        return str(value).strip()


def _attachment_field_contains(value: object | None, expected_filename: str) -> bool:
    if not isinstance(value, list):
        return False
    expected = expected_filename.casefold()
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).casefold()
        url = str(item.get("url", "")).casefold()
        url_w_query = str(item.get("url_w_query", "")).casefold()
        if expected in {title, url.rsplit("/", 1)[-1], url_w_query.rsplit("/", 1)[-1].split("?", 1)[0]}:
            return True
    return False
