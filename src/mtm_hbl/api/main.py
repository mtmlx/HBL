import uvicorn
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mtm_hbl.clickup_connector.client import ClickUpClient
from mtm_hbl.clickup_connector.oauth import (
    ClickUpOAuthClient,
    LocalTokenStore,
    OAuthStateStore,
)
from mtm_hbl.config import AppConfig, Settings, get_settings
from mtm_hbl.clickup_hbl_generator import (
    GenerationMode,
    _verification_id_suffix,
    generate_hbl_from_clickup,
)
from mtm_hbl.models.canonical import CanonicalHblData
from mtm_hbl.local_review import build_local_review
from mtm_hbl.pipeline import PipelineInput, build_review_packet
from mtm_hbl.excel.hbl_writer import ExcelHblWriter
from mtm_hbl.excel.template_ingestor import ingest_template
from mtm_hbl.excel.template_inspector import inspect_template
from mtm_hbl.pdf.pdf_generator import export_excel_to_pdf
from mtm_hbl.pdf.hbl_package import generate_bill_of_lading_package
from mtm_hbl.reports.qa_reporter import save_qa_json, save_qa_markdown
from mtm_hbl.review.review_packet import save_review_packet
from mtm_hbl.utils.file_naming import build_draft_excel_name
from mtm_hbl.verification.aws_repository import (
    AwsVerificationConfig,
    register_issued_package,
)

app = FastAPI(title="MTM Guatemala HBL Draft Generator", version="0.1.0")
state_store = OAuthStateStore()


class TaskReviewRequest(BaseModel):
    task_id: str


class TemplateInspectRequest(BaseModel):
    template_path: str


class TemplateIngestPathRequest(BaseModel):
    template_path: str


class DraftGenerationRequest(BaseModel):
    review_packet: CanonicalHblData
    template_path: str
    output_dir: str | None = None
    version: int = 1
    export_pdf: bool = True


class PackageGenerationRequest(BaseModel):
    review_packet: CanonicalHblData
    output_dir: str | None = None
    output_filename: str | None = None
    logo_path: str | None = None
    draft: bool = False
    verification_base_url: str | None = None


class PackageIssueRequest(PackageGenerationRequest):
    bucket: str | None = None
    table: str | None = None
    region: str | None = None
    status: str = "ISSUED"
    issued_by: str = "Andrea Piedad Velasquez Castellon"
    package_id: str | None = None


class ClickUpHblGenerationRequest(BaseModel):
    task_ref: str
    mode: GenerationMode = "auto"
    output_dir: str | None = None
    logo_path: str | None = "assets/mtm_logix_logo.png"
    attach_to_clickup: bool = False
    post_comment: bool = False
    verification_base_url: str | None = None
    bucket: str | None = None
    table: str | None = None
    region: str | None = None
    issued_by: str = "Andrea Piedad Velasquez Castellon"
    prevent_original_overwrite: bool = False


class LocalReviewRequest(BaseModel):
    shipment_name: str
    agent_hbl_pdf: str
    carrier_mbl_pdf: str
    owner_country: str = "Guatemala"
    clickup_hbl_number: str = ""
    clickup_vessel_voyage: str = ""
    notify_party_override: str = ""
    delivery_apply_to_override: str = ""
    freight_rate: str = ""
    freight_currency: str = ""
    freight_unit: str = ""
    freight_charge_description: str = ""
    freight_payable_at: str = ""
    customer_slug: str = ""
    source_strategy: str = ""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "routine": "MTM Guatemala HBL Draft Generator"}


@app.get("/")
async def root_oauth_callback(
    code: str = Query(""),
    state: str = Query(""),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not code:
        return {
            "status": "ok",
            "message": "MTM HBL API is running. Use /auth/clickup/start to connect ClickUp.",
        }
    return await _store_clickup_oauth_token(code, state, settings)


@app.get("/auth/clickup/start")
def start_clickup_oauth(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if not settings.clickup_client_id:
        raise HTTPException(status_code=500, detail="CLICKUP_CLIENT_ID is not configured.")
    state = state_store.create()
    url = ClickUpOAuthClient(settings).build_authorization_url(state)
    return RedirectResponse(url)


@app.get("/auth/clickup/callback")
async def clickup_callback(
    code: str = Query(...),
    state: str = Query(""),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    return await _store_clickup_oauth_token(code, state, settings)


async def _store_clickup_oauth_token(
    code: str,
    state: str,
    settings: Settings,
) -> dict[str, str]:
    if state and not state_store.consume(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    token = await ClickUpOAuthClient(settings).exchange_code(code)
    LocalTokenStore(settings.token_store_path).save(token)
    return {"status": "connected", "message": "ClickUp OAuth token stored for local API use."}


@app.get("/auth/clickup/status")
def clickup_status(settings: Settings = Depends(get_settings)) -> dict[str, bool]:
    token = LocalTokenStore(settings.token_store_path).load()
    return {"connected": token is not None}


@app.post("/tasks/{task_id}/review", response_model=CanonicalHblData)
async def create_review_packet(
    task_id: str,
    settings: Settings = Depends(get_settings),
) -> CanonicalHblData:
    token = LocalTokenStore(settings.token_store_path).load()
    if token is None:
        raise HTTPException(status_code=401, detail="ClickUp is not connected. Start OAuth first.")

    app_config = AppConfig(settings.config_dir)
    clickup_client = ClickUpClient(settings, token.access_token)
    task = await clickup_client.get_task(task_id)
    clickup_values = clickup_client.extract_configured_fields(task, app_config)
    return build_review_packet(
        PipelineInput(clickup_task_id=task_id, clickup_values=clickup_values),
        app_config=app_config,
    )


@app.post("/templates/inspect")
def inspect_excel_template(request: TemplateInspectRequest) -> dict:
    path = Path(request.template_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template not found: {path}")
    return inspect_template(path)


@app.post("/templates/ingest-path")
def ingest_excel_template_from_path(
    request: TemplateIngestPathRequest,
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        return ingest_template(Path(request.template_path), Path("templates"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/templates/upload")
async def upload_excel_template(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Template upload must be .xlsx or .xlsm.")
    upload_dir = settings.runs_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_path = upload_dir / f"uploaded_template{suffix}"
    with upload_path.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)
    return ingest_template(upload_path, Path("templates"))


@app.post("/local/review", response_model=CanonicalHblData)
def create_local_review(
    request: LocalReviewRequest,
    settings: Settings = Depends(get_settings),
) -> CanonicalHblData:
    agent_path = Path(request.agent_hbl_pdf)
    carrier_path = Path(request.carrier_mbl_pdf)
    if not agent_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent HBL not found: {agent_path}")
    if not carrier_path.exists():
        raise HTTPException(status_code=404, detail=f"Carrier MBL not found: {carrier_path}")
    return build_local_review(
        shipment_name=request.shipment_name,
        agent_hbl_pdf=agent_path,
        carrier_mbl_pdf=carrier_path,
        owner_country=request.owner_country,
        clickup_hbl_number=request.clickup_hbl_number,
        clickup_vessel_voyage=request.clickup_vessel_voyage,
        notify_party_override=request.notify_party_override,
        delivery_apply_to_override=request.delivery_apply_to_override,
        freight_rate=request.freight_rate,
        freight_currency=request.freight_currency,
        freight_unit=request.freight_unit,
        freight_charge_description=request.freight_charge_description,
        freight_payable_at=request.freight_payable_at,
        customer_slug=request.customer_slug,
        source_strategy=request.source_strategy,
        app_config=AppConfig(settings.config_dir),
    )


@app.post("/drafts/generate")
def generate_draft(
    request: DraftGenerationRequest,
    settings: Settings = Depends(get_settings),
) -> dict[str, str | bool]:
    app_config = AppConfig(settings.config_dir)
    data = request.review_packet
    if not data.qa.draft_generation_allowed:
        raise HTTPException(
            status_code=409,
            detail="Draft generation is blocked by QA. Resolve blockers before generating.",
        )
    if not data.shipment.mtm_hbl_no:
        raise HTTPException(status_code=409, detail="HBL number is required for draft file naming.")

    output_dir = Path(request.output_dir) if request.output_dir else settings.runs_dir / "manual_drafts"
    output_dir.mkdir(parents=True, exist_ok=True)

    review_path = output_dir / "approved_review.json"
    qa_json_path = output_dir / "qa_report.json"
    qa_md_path = output_dir / "qa_report.md"
    excel_name = build_draft_excel_name(app_config, data.shipment.mtm_hbl_no, request.version)
    excel_path = output_dir / excel_name

    save_review_packet(data, review_path)
    save_qa_json(data, qa_json_path)
    save_qa_markdown(data, qa_md_path)
    ExcelHblWriter(app_config).write(Path(request.template_path), excel_path, data)

    response: dict[str, str | bool] = {
        "draft_generated": True,
        "excel_path": str(excel_path),
        "review_path": str(review_path),
        "qa_json_path": str(qa_json_path),
        "qa_markdown_path": str(qa_md_path),
    }
    if request.export_pdf:
        pdf_path = export_excel_to_pdf(excel_path, output_dir)
        response["pdf_path"] = str(pdf_path)
    return response


@app.post("/packages/generate")
def generate_package(
    request: PackageGenerationRequest,
    settings: Settings = Depends(get_settings),
) -> dict[str, str | bool]:
    data = request.review_packet
    if not data.shipment.mtm_hbl_no:
        raise HTTPException(status_code=409, detail="HBL number is required for package file naming.")

    output_dir = Path(request.output_dir) if request.output_dir else settings.runs_dir / "hbl_packages"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = request.output_filename or f"HBL_Package_{data.shipment.mtm_hbl_no}.pdf"
    output_path = output_dir / filename

    package_path = generate_bill_of_lading_package(
        data,
        output_path,
        logo_path=Path(request.logo_path) if request.logo_path else None,
        draft=request.draft,
        verification_base_url=request.verification_base_url or "",
    )
    return {"package_generated": True, "pdf_path": str(package_path)}


@app.post("/packages/issue-dev")
def issue_dev_package(
    request: PackageIssueRequest,
    settings: Settings = Depends(get_settings),
) -> dict:
    data = request.review_packet
    if not data.shipment.mtm_hbl_no:
        raise HTTPException(status_code=409, detail="HBL number is required for package file naming.")

    output_dir = Path(request.output_dir) if request.output_dir else settings.runs_dir / "hbl_packages"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = request.output_filename or f"HBL_Package_{data.shipment.mtm_hbl_no}.pdf"
    output_path = output_dir / filename

    verification_base_url = request.verification_base_url or settings.hbl_verification_base_url
    bucket = request.bucket or settings.hbl_verification_bucket
    table = request.table or settings.hbl_verification_table
    region = request.region or settings.aws_region
    missing = [
        name
        for name, value in [
            ("verification_base_url", verification_base_url),
            ("bucket", bucket),
            ("table", table),
        ]
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Missing verification configuration: {', '.join(missing)}",
        )

    package_id = request.package_id or f"pkg_{uuid4().hex}"
    verification_id_suffix = _verification_id_suffix(package_id)
    package_path = generate_bill_of_lading_package(
        data,
        output_path,
        logo_path=Path(request.logo_path) if request.logo_path else None,
        draft=request.draft,
        verification_base_url=verification_base_url,
        verification_id_suffix=verification_id_suffix,
    )
    registration = register_issued_package(
        data,
        package_path,
        AwsVerificationConfig(
            bucket_name=bucket,
            table_name=table,
            region_name=region,
            verification_base_url=verification_base_url,
        ),
        status=request.status,
        package_id=package_id,
        verification_id_suffix=verification_id_suffix,
        issued_by=request.issued_by,
    )

    return {
        "package_generated": True,
        "pdf_path": str(package_path),
        "package_id": registration.package_id,
        "pdf_s3_key": registration.pdf_s3_key,
        "canonical_json_s3_key": registration.canonical_json_s3_key,
        "pdf_sha256": registration.pdf_sha256,
        "canonical_json_sha256": registration.canonical_json_sha256,
        "verification_urls": registration.verification_urls,
    }


@app.post("/clickup/hbl/generate")
async def generate_hbl_from_clickup_link(
    request: ClickUpHblGenerationRequest,
    settings: Settings = Depends(get_settings),
) -> dict:
    token = LocalTokenStore(settings.token_store_path).load()
    if token is None:
        raise HTTPException(status_code=401, detail="ClickUp is not connected. Start OAuth first.")

    client = ClickUpClient(settings, token.access_token)
    try:
        result = await generate_hbl_from_clickup(
            task_ref=request.task_ref,
            client=client,
            settings=settings,
            app_config=AppConfig(settings.config_dir),
            mode=request.mode,
            output_dir=Path(request.output_dir) if request.output_dir else None,
            logo_path=Path(request.logo_path) if request.logo_path else None,
            attach_to_clickup=request.attach_to_clickup,
            post_comment=request.post_comment,
            verification_base_url=request.verification_base_url or "",
            bucket=request.bucket or "",
            table=request.table or "",
            region=request.region or "",
            issued_by=request.issued_by,
            prevent_original_overwrite=request.prevent_original_overwrite,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/clickup/tasks/{task_id}/hbl/generate")
async def generate_hbl_from_clickup_task(
    task_id: str,
    request: ClickUpHblGenerationRequest | None = None,
    settings: Settings = Depends(get_settings),
) -> dict:
    payload = request or ClickUpHblGenerationRequest(task_ref=task_id)
    payload.task_ref = task_id
    return await generate_hbl_from_clickup_link(payload, settings)


def run() -> None:
    uvicorn.run("mtm_hbl.api.main:app", host="127.0.0.1", port=8000, reload=True)
