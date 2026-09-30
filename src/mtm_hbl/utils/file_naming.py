from pathlib import Path

from mtm_hbl.config import AppConfig
from mtm_hbl.safe_paths import filename_component


def build_draft_pdf_name(app_config: AppConfig, hbl_number: str, version: int) -> str:
    filename_component(hbl_number)
    pattern = app_config.file_naming_rules["draft"]["pattern"]
    return pattern.format(hbl_number=hbl_number, version=version)


def build_draft_excel_name(app_config: AppConfig, hbl_number: str, version: int) -> str:
    filename_component(hbl_number)
    pattern = app_config.file_naming_rules["populated_excel"]["pattern"]
    return pattern.format(hbl_number=hbl_number, version=version)


def next_versioned_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
