from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    clickup_client_id: str = ""
    clickup_client_secret: str = ""
    clickup_redirect_uri: str = "http://localhost:8000/auth/clickup/callback"
    clickup_api_base_url: str = "https://api.clickup.com/api/v2"
    clickup_workspace_id: str = ""
    app_base_url: str = "http://localhost:8000"
    app_secret_key: str = "change-me-for-local-development"
    runs_dir: Path = Path("runs")
    config_dir: Path = Path("config")
    token_store_path: Path = Path("runs/clickup_token.json")
    aws_region: str = "us-east-1"
    hbl_verification_base_url: str = ""
    hbl_verification_bucket: str = ""
    hbl_verification_table: str = ""
    gamma_api_key: str = ""
    gamma_api_base_url: str = "https://public-api.gamma.app/v1.0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration file must contain a mapping: {path}")
    return data


class AppConfig:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.settings = get_settings()
        self.config_dir = config_dir or self.settings.config_dir

    def load(self, name: str) -> dict[str, Any]:
        return load_yaml(self.config_dir / name)

    @property
    def clickup_fields(self) -> dict[str, Any]:
        return self.load("clickup_fields.yaml")

    @property
    def excel_cell_mapping(self) -> dict[str, Any]:
        return self.load("excel_cell_mapping.yaml")

    @property
    def entity_rules(self) -> dict[str, Any]:
        return self.load("entity_rules.yaml")

    @property
    def qa_rules(self) -> dict[str, Any]:
        return self.load("qa_rules.yaml")

    @property
    def source_of_truth_rules(self) -> dict[str, Any]:
        return self.load("source_of_truth_rules.yaml")

    @property
    def hbl_business_rules(self) -> dict[str, Any]:
        return self.load("hbl_business_rules.yaml")

    @property
    def file_naming_rules(self) -> dict[str, Any]:
        return self.load("file_naming_rules.yaml")

    @property
    def container_words(self) -> dict[int, str]:
        raw = self.load("container_words.yaml")
        return {int(key): str(value) for key, value in raw.items()}

    @property
    def date_formats(self) -> dict[str, Any]:
        return self.load("date_formats.yaml")
