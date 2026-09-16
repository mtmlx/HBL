from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import os
from pathlib import Path
import re

from pypdf import PdfReader
import qrcode
from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from mtm_hbl.models.canonical import CanonicalHblData, ChargeLine, Container

try:
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover - reportlab normally installs Pillow.
    Image = None
    ImageChops = None


MM = 72 / 25.4
PAGE_SIZE = letter
PAGE_WIDTH, PAGE_HEIGHT = PAGE_SIZE
MARGIN = 10 * MM
HEADER_HEIGHT = 118
PARTY_HEIGHT = 195
SHIPPER_BLOCK_HEIGHT = 55
TERMS_VALIDATION_HEIGHT = 56
ROUTING_HEIGHT = 43
CARGO_TITLE_HEIGHT = 17
CARGO_TABLE_HEIGHT = 218
CARGO_HEADER_HEIGHT = 24
CARGO_TOTAL_HEIGHT = 23
DESCRIPTION_FONT_SIZE = 7.2
DESCRIPTION_LINE_GAP = 8.7
FREIGHT_ROWS_PER_PAGE = 4

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
LINE_COLOR = black
WATERMARK_COLOR = HexColor("#777777")
DEFAULT_SIGNATURE_IMAGE_PATH = Path(os.getenv("HBL_SIGNATURE_IMAGE_PATH", "assets/signature.png"))

PROHIBITED_TERMS = [
    "Carrier's Receipt",
    "Carrier’s Receipt",
    "received by Carrier",
    "Stamp and signature",
    "For the delivery of goods please apply to",
    "Freight payable at",
    "Payable at",
]

REQUIRED_TERMS = [
    "Freight Forwarder's Receipt",
    "received for forwarding",
    "Andrea Piedad Velasquez Castellon",
    "For MTM Logix Guatemala Sociedad Anonima",
    "Goods to be delivered to:",
    "Terms & Conditions / Document Validation",
    "Shipper's load, stow, count and seal. Particulars furnished by shipper.",
    "No. of original B(s)/L:",
]

TERMS_VALIDATION_TITLE = "TERMS & CONDITIONS / DOCUMENT VALIDATION"
TERMS_VALIDATION_TEXT = (
    "This Multimodal Transport Bill of Lading is issued subject to MTM Logix's Terms "
    "and Conditions, available through the QR code and validation URL printed on this "
    "document. Such Terms and Conditions are incorporated into and form part of this "
    "Bill of Lading. Scan the QR code to verify authenticity, document status, release "
    "status, and the applicable Terms and Conditions version."
)


@dataclass(frozen=True)
class DocumentPageConfig:
    type: str
    sequence: int
    total: int = 3
    page_number: int = 1
    page_total: int = 1

    @property
    def display(self) -> str:
        return f"{self.type} {self.sequence}/{self.total}"

    @property
    def page_display(self) -> str:
        return f"{self.page_number} of {self.page_total}"


@dataclass(frozen=True)
class CargoPageContent:
    description_lines: list[str]
    containers: list[Container]
    marks_lines: list[str]


@dataclass(frozen=True)
class FreightPageContent:
    charges: list[ChargeLine]
    show_total: bool = False


DOCUMENT_SET: tuple[DocumentPageConfig, ...] = (
    DocumentPageConfig("ORIGINAL", 1),
    DocumentPageConfig("ORIGINAL", 2),
    DocumentPageConfig("ORIGINAL", 3),
    DocumentPageConfig("COPY", 1),
    DocumentPageConfig("COPY", 2),
    DocumentPageConfig("COPY", 3),
)


def build_document_page_set(data: CanonicalHblData, *, draft: bool = False) -> tuple[DocumentPageConfig, ...]:
    page_total = _document_page_total(data)
    if draft:
        return tuple(DocumentPageConfig("DRAFT", 1, 1, page_number, page_total) for page_number in range(1, page_total + 1))
    pages: list[DocumentPageConfig] = []
    for base in DOCUMENT_SET:
        pages.extend(
            DocumentPageConfig(base.type, base.sequence, base.total, page_number, page_total)
            for page_number in range(1, page_total + 1)
        )
    return tuple(pages)


def split_description_pages(data: CanonicalHblData) -> list[list[str]]:
    return [page.description_lines for page in split_cargo_pages(data)]


def _document_page_total(data: CanonicalHblData) -> int:
    return max(len(split_cargo_pages(data)), len(split_freight_pages(data)), 1)


def split_cargo_pages(data: CanonicalHblData) -> list[CargoPageContent]:
    description_pages = _split_description_lines(data)
    container_pages = _split_container_pages(data)
    page_total = max(len(description_pages), len(container_pages), 1)
    pages: list[CargoPageContent] = []
    for index in range(page_total):
        pages.append(
            CargoPageContent(
                description_lines=description_pages[index] if index < len(description_pages) else [],
                containers=container_pages[index] if index < len(container_pages) else [],
                marks_lines=_first_marks_lines(data) if index == 0 else [],
            )
        )
    return pages


def split_freight_pages(data: CanonicalHblData) -> list[FreightPageContent]:
    charges = _display_charge_lines_for_data(data)
    if not charges:
        return []
    pages: list[FreightPageContent] = []
    remaining = list(charges)
    while remaining:
        page_charges = remaining[:FREIGHT_ROWS_PER_PAGE]
        remaining = remaining[FREIGHT_ROWS_PER_PAGE:]
        pages.append(FreightPageContent(page_charges, show_total=not remaining))
    return pages


def _split_description_lines(data: CanonicalHblData) -> list[list[str]]:
    lines = _description_lines(data)
    description_width = (PAGE_WIDTH - (2 * MARGIN)) * 0.46 - 10
    visual_lines = _wrapped_visual_lines(lines, description_width, FONT, DESCRIPTION_FONT_SIZE)
    page_capacity = _page_description_capacity()
    if len(visual_lines) <= page_capacity:
        return [visual_lines]

    chunks = [visual_lines[:page_capacity]]
    remaining = visual_lines[page_capacity:]
    while remaining:
        chunks.append(remaining[:page_capacity])
        remaining = remaining[page_capacity:]
    return chunks


def _split_container_pages(data: CanonicalHblData) -> list[list[Container]]:
    containers = list(data.containers)
    if not containers:
        return [[]]
    capacity = _page_container_capacity()
    pages: list[list[Container]] = []
    current: list[Container] = []
    current_lines = len(_first_marks_lines(data)) + 1
    for container in containers:
        additional = 2 + (1 if current else 0)
        if current and current_lines + additional > capacity:
            pages.append(current)
            current = []
            current_lines = 0
            additional = 2
        current.append(container)
        current_lines += additional
    if current:
        pages.append(current)
    return pages


def _description_lines(data: CanonicalHblData) -> list[str]:
    lines = [
        "Shipper's load, stow, count and seal.",
        "Particulars furnished by shipper.",
        "",
    ]
    lines.extend(line.strip() for line in data.cargo.description_raw.splitlines() if line.strip())
    return lines


def _charge_lines_for_data(data: CanonicalHblData) -> list[ChargeLine]:
    if data.charges.line_items:
        return data.charges.line_items
    charge = ChargeLine(
        description=data.charges.charge_description or "Ocean Basic Freight",
        rate=data.charges.unit_rate,
        unit=data.charges.unit,
        currency=data.charges.currency,
        prepaid_amount=data.charges.prepaid_amount,
        collect_amount=data.charges.collect_amount,
    )
    return [charge]


def _display_charge_lines_for_data(data: CanonicalHblData) -> list[ChargeLine]:
    return [charge for charge in _charge_lines_for_data(data) if charge.show_on_hbl]


def _wrapped_visual_lines(lines: list[str], width: float, font: str, size: float) -> list[str]:
    wrapped: list[str] = []
    for source_line in lines:
        wrapped.extend(_wrap_text(source_line, width, font, size) if source_line else [""])
    return wrapped


def _first_marks_lines(data: CanonicalHblData) -> list[str]:
    if not data.containers:
        return ["MARKS & NOS.:", "N/M"]
    marks = data.containers[0].marks_and_numbers.strip()
    lines = [line.strip() for line in marks.splitlines() if line.strip()] if marks else ["N/M"]
    return ["MARKS & NOS.:", *lines]


def _page_description_capacity() -> int:
    y_top = PAGE_HEIGHT - MARGIN
    y_top -= HEADER_HEIGHT + PARTY_HEIGHT + ROUTING_HEIGHT
    y_title_bottom = y_top - CARGO_TITLE_HEIGHT
    y_bottom = y_title_bottom - CARGO_TABLE_HEIGHT
    body_top = y_title_bottom - CARGO_HEADER_HEIGHT
    total_top = y_bottom + CARGO_TOTAL_HEIGHT
    return _max_lines_for_height(body_top - 8, total_top + 5, DESCRIPTION_LINE_GAP)


def _page_container_capacity() -> int:
    y_top = PAGE_HEIGHT - MARGIN
    y_top -= HEADER_HEIGHT + PARTY_HEIGHT + ROUTING_HEIGHT
    y_title_bottom = y_top - CARGO_TITLE_HEIGHT
    y_bottom = y_title_bottom - CARGO_TABLE_HEIGHT
    body_top = y_title_bottom - CARGO_HEADER_HEIGHT
    total_top = y_bottom + CARGO_TOTAL_HEIGHT
    return _max_lines_for_height(body_top - 8, total_top + 5, 9)


def _max_lines_for_height(y_top: float, y_bottom: float, line_gap: float) -> int:
    return max(1, int((y_top - y_bottom) // line_gap) + 1)


def _wrap_text(text: object, width: float, font: str, size: float) -> list[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font, size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def generate_bill_of_lading_package(
    data: CanonicalHblData,
    output_path: Path,
    *,
    logo_path: Path | None = None,
    draft: bool = False,
    verification_base_url: str = "",
    verification_id_suffix: str = "",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output_path), pagesize=PAGE_SIZE)
    renderer = BillOfLadingPackageRenderer(
        pdf,
        data,
        logo_path=logo_path,
        draft=draft,
        verification_base_url=verification_base_url,
        verification_id_suffix=verification_id_suffix,
    )

    cargo_pages = split_cargo_pages(data)
    freight_pages = split_freight_pages(data)
    page_configs = build_document_page_set(data)
    freight_start_page = page_configs[0].page_total - len(freight_pages) + 1 if freight_pages else 0
    for index, page_config in enumerate(page_configs):
        if index:
            pdf.showPage()
        cargo_page = (
            cargo_pages[page_config.page_number - 1]
            if page_config.page_number <= len(cargo_pages)
            else CargoPageContent([], [], [])
        )
        freight_page = (
            freight_pages[page_config.page_number - freight_start_page]
            if freight_pages and page_config.page_number >= freight_start_page
            else None
        )
        renderer.render_page(
            page_config,
            cargo_page,
            freight_page=freight_page,
            show_totals=page_config.page_number == len(cargo_pages),
        )

    pdf.save()
    validate_bill_of_lading_package(output_path)
    return output_path


def generate_bill_of_lading_draft(
    data: CanonicalHblData,
    output_path: Path,
    *,
    logo_path: Path | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output_path), pagesize=PAGE_SIZE)
    renderer = BillOfLadingPackageRenderer(
        pdf,
        data,
        logo_path=logo_path,
        draft=True,
        verification_base_url="",
        include_verification=False,
    )
    cargo_pages = split_cargo_pages(data)
    freight_pages = split_freight_pages(data)
    page_configs = build_document_page_set(data, draft=True)
    freight_start_page = page_configs[0].page_total - len(freight_pages) + 1 if freight_pages else 0
    for index, page_config in enumerate(page_configs):
        if index:
            pdf.showPage()
        cargo_page = (
            cargo_pages[page_config.page_number - 1]
            if page_config.page_number <= len(cargo_pages)
            else CargoPageContent([], [], [])
        )
        freight_page = (
            freight_pages[page_config.page_number - freight_start_page]
            if freight_pages and page_config.page_number >= freight_start_page
            else None
        )
        renderer.render_page(
            page_config,
            cargo_page,
            freight_page=freight_page,
            show_totals=page_config.page_number == len(cargo_pages),
        )
    pdf.save()
    validate_bill_of_lading_draft(output_path)
    return output_path


def validate_bill_of_lading_package(path: Path) -> None:
    reader = PdfReader(str(path))
    if len(reader.pages) % len(DOCUMENT_SET) != 0:
        raise ValueError(f"Bill of Lading package page count must be a multiple of 6; got {len(reader.pages)}.")

    page_total = len(reader.pages) // len(DOCUMENT_SET)
    expected = [
        DocumentPageConfig(base.type, base.sequence, base.total, page_number, page_total)
        for base in DOCUMENT_SET
        for page_number in range(1, page_total + 1)
    ]
    for index, (page, expected_config) in enumerate(zip(reader.pages, expected), start=1):
        text = page.extract_text() or ""
        if expected_config.display not in text:
            raise ValueError(f"Page {index} is missing sequence label {expected_config.display}.")
        if expected_config.page_display not in text:
            raise ValueError(f"Page {index} is missing page label {expected_config.page_display}.")
        watermark = expected_config.type
        if watermark not in text:
            raise ValueError(f"Page {index} is missing {watermark} watermark text.")
        normalized = _normalize_pdf_text(text)
        for term in PROHIBITED_TERMS:
            if _normalize_pdf_text(term) in normalized:
                raise ValueError(f"Page {index} contains prohibited terminology: {term}")
        if expected_config.page_number == 1:
            for term in REQUIRED_TERMS:
                if _normalize_pdf_text(term) not in normalized:
                    raise ValueError(f"Page {index} is missing required terminology: {term}")


def validate_bill_of_lading_draft(path: Path) -> None:
    reader = PdfReader(str(path))
    if len(reader.pages) < 1:
        raise ValueError("Draft Bill of Lading must contain at least 1 page.")
    page_total = len(reader.pages)
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        normalized = _normalize_pdf_text(text)
        if "draft 1/1" not in normalized:
            raise ValueError(f"Draft Bill of Lading page {page_number} is missing DRAFT 1/1 label.")
        if f"{page_number} of {page_total}" not in normalized:
            raise ValueError(f"Draft Bill of Lading page {page_number} is missing page label.")
        if "verify document" in normalized:
            raise ValueError("Draft Bill of Lading must not include QR verification text.")
        if "original 1/3" in normalized or "copy 1/3" in normalized:
            raise ValueError("Draft Bill of Lading must not include original/copy sequence labels.")


def _normalize_pdf_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def verification_id_for_page(
    data: CanonicalHblData,
    page_config: DocumentPageConfig,
    *,
    suffix: str = "",
) -> str:
    hbl = data.shipment.mtm_hbl_no or "UNKNOWN"
    page_suffix = f"{page_config.type[0]}{page_config.sequence}"
    if page_config.page_total > 1:
        page_suffix = f"{page_suffix}-P{page_config.page_number}"
    if suffix:
        page_suffix = f"{page_suffix}-{suffix}"
    return f"{hbl}-{page_suffix}"


def verification_url_for_page(
    data: CanonicalHblData,
    page_config: DocumentPageConfig,
    verification_base_url: str,
    *,
    suffix: str = "",
) -> str:
    verification_id = verification_id_for_page(data, page_config, suffix=suffix)
    if not verification_base_url:
        return verification_id
    return f"{verification_base_url.rstrip('/')}/verify/{verification_id}"


class BillOfLadingPackageRenderer:
    def __init__(
        self,
        pdf: canvas.Canvas,
        data: CanonicalHblData,
        *,
        logo_path: Path | None = None,
        draft: bool = False,
        verification_base_url: str = "",
        include_verification: bool = True,
        verification_id_suffix: str = "",
    ) -> None:
        self.pdf = pdf
        self.data = data
        self.logo_path = logo_path if logo_path and logo_path.exists() else None
        self.draft = draft
        self.verification_base_url = verification_base_url
        self.include_verification = include_verification
        self.verification_id_suffix = verification_id_suffix
        self.issued_at = datetime.now().astimezone()
        self.left = MARGIN
        self.bottom = MARGIN
        self.width = PAGE_WIDTH - (2 * MARGIN)
        self.height = PAGE_HEIGHT - (2 * MARGIN)
        self.top = PAGE_HEIGHT - MARGIN

    def render_page(
        self,
        page_config: DocumentPageConfig,
        cargo_page: CargoPageContent,
        *,
        freight_page: FreightPageContent | None = None,
        show_totals: bool = True,
    ) -> None:
        self._draw_watermark(page_config.type)
        self._draw_outer_border()

        y = self.top
        y = self._draw_header(y, page_config)
        y = self._draw_party_section(y)
        y = self._draw_routing_section(y)
        y = self._draw_cargo_particulars(y, cargo_page, show_totals=show_totals)
        if freight_page:
            y = self._draw_freight_table(y, freight_page)
        self._draw_footer(y, page_config)

    def _draw_watermark(self, text: str) -> None:
        self.pdf.saveState()
        self.pdf.setFillColor(WATERMARK_COLOR, alpha=0.10)
        self.pdf.setFont(FONT_BOLD, 76)
        self.pdf.translate(PAGE_WIDTH / 2, PAGE_HEIGHT / 2)
        self.pdf.rotate(-25)
        self.pdf.drawCentredString(0, 0, text)
        self.pdf.restoreState()

    def _draw_outer_border(self) -> None:
        self.pdf.setStrokeColor(LINE_COLOR)
        self.pdf.setLineWidth(0.75)
        self.pdf.rect(self.left, self.bottom, self.width, self.height, stroke=1, fill=0)

    def _draw_header(self, y_top: float, page_config: DocumentPageConfig) -> float:
        height = 118
        x_mid = self.left + self.width * 0.50
        y_bottom = y_top - height
        self._section_box(self.left, y_bottom, self.width, height)
        self._vline(x_mid, y_bottom, height, 0.6)

        left_x = self.left + 8
        content_top = y_top - 10
        if self.logo_path:
            try:
                logo = self._logo_reader()
                image_width, image_height = logo.getSize()
                max_width = 150
                max_height = 50
                scale = min(max_width / image_width, max_height / image_height)
                draw_width = image_width * scale
                draw_height = image_height * scale
                self.pdf.drawImage(
                    logo,
                    left_x,
                    content_top - draw_height,
                    draw_width,
                    draw_height,
                    mask="auto",
                    preserveAspectRatio=True,
                )
                issuer_y = content_top - draw_height - 9
            except Exception:
                issuer_y = content_top
                self._text(left_x, issuer_y, "MTM Logix", FONT_BOLD, 14, color=HexColor("#13245A"))
                issuer_y -= 18
        else:
            self._text(left_x, content_top, "MTM Logix", FONT_BOLD, 14, color=HexColor("#13245A"))
            issuer_y = content_top - 20

        issuer_lines = [
            "MTM LOGIX GUATEMALA SOCIEDAD ANONIMA",
            "NIT: 109582985",
            "7A. AV. 13-78 ZONA 4",
            "EDIFICIO SEPTIMO, NIVEL 3, OFICINA 306",
            "GUATEMALA",
        ]
        self._draw_lines(left_x, issuer_y, issuer_lines, self.width * 0.45 - 12, 7.2, line_gap=9)

        right_x = x_mid + 8
        right_w = self.width * 0.50 - 16
        self._text(
            right_x + right_w / 2,
            y_top - 20,
            "MULTIMODAL TRANSPORT BILL OF LADING",
            FONT_BOLD,
            10.8,
            align="center",
        )
        ref_y = y_top - 43
        row_h = 15
        rows = [
            ("HBL NO.", self.data.shipment.mtm_hbl_no),
            ("MBL NO.", self.data.shipment.mbl_no),
            ("PAGE", page_config.page_display),
            ("DOCUMENT", page_config.display),
        ]
        if self.draft:
            rows.append(("STATUS", "DRAFT"))
        for label, value in rows:
            self._label_value(right_x, ref_y, right_w, row_h, label, value, bold_value=True)
            ref_y -= row_h

        return y_bottom

    def _logo_reader(self) -> ImageReader:
        if Image is None or ImageChops is None:
            return ImageReader(str(self.logo_path))
        image = Image.open(self.logo_path).convert("RGBA")
        rgb = image.convert("RGB")
        white = Image.new("RGB", image.size, (255, 255, 255))
        diff = ImageChops.difference(rgb, white)
        bbox = diff.getbbox()
        if bbox:
            left, top, right, bottom = bbox
            pad = 4
            image = image.crop(
                (
                    max(left - pad, 0),
                    max(top - pad, 0),
                    min(right + pad, image.width),
                    min(bottom + pad, image.height),
                )
            )
        return ImageReader(image)

    def _draw_party_section(self, y_top: float) -> float:
        height = PARTY_HEIGHT
        y_bottom = y_top - height
        x_mid = self.left + self.width * 0.50
        self._section_box(self.left, y_bottom, self.width, height)
        self._vline(x_mid, y_bottom, height, 0.6)

        consignee_block_height = (height - SHIPPER_BLOCK_HEIGHT) / 2
        blocks = [
            ("SHIPPER", self.data.parties.shipper.raw_text, SHIPPER_BLOCK_HEIGHT),
            ("CONSIGNEE", self.data.parties.consignee.raw_text, consignee_block_height),
            (
                "NOTIFY PARTY",
                self.data.parties.notify_party.raw_text or "SAME AS CONSIGNEE",
                consignee_block_height,
            ),
        ]
        box_top = y_top
        for index, (label, text, box_h) in enumerate(blocks):
            if index:
                self._hline(self.left, box_top, self.width * 0.50, 0.45)
            self._draw_labeled_block(self.left, box_top - box_h, self.width * 0.50, box_h, label, text)
            box_top -= box_h

        delivery = (
            self.data.parties.delivery_apply_to.raw_text
            or self.data.parties.consignee.raw_text
        )
        right_w = self.width * 0.50
        terms_h = TERMS_VALIDATION_HEIGHT
        delivery_h = height - terms_h
        terms_top = y_bottom + terms_h
        self._hline(x_mid, terms_top, right_w, 0.45)
        self._draw_labeled_block(
            x_mid,
            terms_top,
            right_w,
            delivery_h,
            "GOODS TO BE DELIVERED TO:",
            delivery,
        )
        self._draw_terms_validation_block(x_mid, y_bottom, right_w, terms_h)
        return y_bottom

    def _draw_terms_validation_block(self, x: float, y: float, w: float, h: float) -> None:
        pad = 5
        body_size = 5.0
        self._text(x + pad, y + h - 10, TERMS_VALIDATION_TITLE, FONT_BOLD, 6.0)
        self._wrapped_lines(
            x + pad,
            y + h - 20,
            self._wrap(TERMS_VALIDATION_TEXT, w - (2 * pad), FONT, body_size),
            w - (2 * pad),
            body_size,
            font=FONT,
            line_gap=6.0,
            max_lines=6,
        )

    def _draw_routing_section(self, y_top: float) -> float:
        height = 43
        y_bottom = y_top - height
        col_w = self.width / 5
        self._section_box(self.left, y_bottom, self.width, height)
        fields = [
            ("PLACE OF RECEIPT", self.data.routing.place_of_receipt),
            ("VESSEL / VOYAGE", self.data.shipment.vessel_voyage_display),
            ("PORT OF LOADING", self.data.routing.port_of_loading),
            ("PORT OF DISCHARGE", self.data.routing.port_of_discharge),
            ("PLACE OF DELIVERY", self.data.routing.place_of_delivery),
        ]
        for index, (label, value) in enumerate(fields):
            x = self.left + index * col_w
            if index:
                self._vline(x, y_bottom, height, 0.45)
            self._text(x + 4, y_top - 10, label, FONT_BOLD, 6.7)
            self._wrapped_text(x + 4, y_top - 22, value, col_w - 8, 7.4, max_lines=2)
        return y_bottom

    def _draw_cargo_particulars(
        self,
        y_top: float,
        cargo_page: CargoPageContent,
        *,
        show_totals: bool,
    ) -> float:
        title_h = CARGO_TITLE_HEIGHT
        table_h = CARGO_TABLE_HEIGHT
        y_title_bottom = y_top - title_h
        y_bottom = y_title_bottom - table_h

        self._section_box(self.left, y_title_bottom, self.width, title_h)
        self._text(
            self.left + self.width / 2,
            y_title_bottom + 5,
            "PARTICULARS FURNISHED BY SHIPPER",
            FONT_BOLD,
            8,
            align="center",
        )

        col_widths = [self.width * 0.26, self.width * 0.46, self.width * 0.14, self.width * 0.14]
        xs = [self.left]
        for width in col_widths[:-1]:
            xs.append(xs[-1] + width)
        self._section_box(self.left, y_bottom, self.width, table_h)
        for x in xs[1:]:
            self._vline(x, y_bottom, table_h, 0.45)

        header_h = CARGO_HEADER_HEIGHT
        total_h = CARGO_TOTAL_HEIGHT
        body_top = y_title_bottom - header_h
        total_top = y_bottom + total_h
        self._hline(self.left, body_top, self.width, 0.45)
        self._hline(self.left, total_top, self.width, 0.6)

        headers = [
            "CONTAINER NO. / SEAL NO. / MARKS & NOS.",
            "NUMBER AND KIND OF PACKAGES; DESCRIPTION OF GOODS",
            "GROSS WEIGHT",
            "MEASUREMENT",
        ]
        for index, header in enumerate(headers):
            self._wrapped_text(
                xs[index] + 4,
                y_title_bottom - 8,
                header,
                col_widths[index] - 8,
                6.8,
                font=FONT_BOLD,
                max_lines=2,
                align="center" if index >= 2 else "left",
            )

        self._draw_cargo_body(xs, col_widths, body_top, total_top, cargo_page)
        if show_totals:
            self._draw_cargo_totals(xs, col_widths, total_top, y_bottom)
        return y_bottom

    def _draw_cargo_body(
        self,
        xs: list[float],
        col_widths: list[float],
        body_top: float,
        total_top: float,
        cargo_page: CargoPageContent,
    ) -> None:
        y = body_top - 8
        containers = cargo_page.containers
        marks_font = 7.0
        marks_gap = 9
        values_font = 7.3
        marks_lines = [*cargo_page.marks_lines]
        if marks_lines and containers:
            marks_lines.append("")
        container_mark_ys = []
        for index, container in enumerate(containers):
            y_for_container = self._container_mark_y(
                len(marks_lines),
                y,
                line_gap=marks_gap,
            )
            container_mark_ys.append(y_for_container)
            marks_lines.extend(
                [
                    f"CONTAINER: {container.container_no}",
                    f"SEAL: {container.seal_no}",
                ]
            )
            if index < len(containers) - 1:
                marks_lines.append("")
        self._wrapped_lines(
            xs[0] + 5,
            y,
            marks_lines,
            col_widths[0] - 10,
            marks_font,
            line_gap=marks_gap,
        )

        self._draw_visual_lines(
            xs[1] + 5,
            y,
            cargo_page.description_lines,
            DESCRIPTION_FONT_SIZE,
            line_gap=DESCRIPTION_LINE_GAP,
        )

        container_count = max(len(containers), 1)
        for index, container in enumerate(containers):
            mark_y = container_mark_ys[index] if index < len(container_mark_ys) else None
            row_y = mark_y if mark_y is not None else self._container_value_y(index, container_count, body_top, total_top)
            self._text(
                xs[2] + col_widths[2] - 5,
                row_y,
                self._weight_text(container),
                FONT,
                values_font,
                align="right",
            )
            self._text(
                xs[3] + col_widths[3] - 5,
                row_y,
                self._measurement_text(container),
                FONT,
                values_font,
                align="right",
            )

    @staticmethod
    def _container_mark_y(line_count_before_container: int, start_y: float, *, line_gap: float) -> float:
        return start_y - (line_count_before_container * line_gap)

    @staticmethod
    def _container_value_y(index: int, container_count: int, body_top: float, total_top: float) -> float:
        if container_count == 1:
            return body_top - 72
        if container_count == 2:
            return body_top - 72 - (index * 36)
        top_y = body_top - 46
        bottom_y = total_top + 34
        step = (top_y - bottom_y) / max(container_count - 1, 1)
        return top_y - (index * step)

    def _draw_cargo_totals(
        self,
        xs: list[float],
        col_widths: list[float],
        total_top: float,
        y_bottom: float,
    ) -> None:
        text_y = y_bottom + 8
        self._text(xs[1] + col_widths[1] - 6, text_y, "TOTAL", FONT_BOLD, 7.2, align="right")
        self._text(
            xs[2] + col_widths[2] - 5,
            text_y,
            self._format_amount_with_unit(self.data.cargo.gross_weight, "KGS", 3),
            FONT_BOLD,
            7.2,
            align="right",
        )
        self._text(
            xs[3] + col_widths[3] - 5,
            text_y,
            self._format_amount_with_unit(self.data.cargo.measurement, "CBM", 3),
            FONT_BOLD,
            7.2,
            align="right",
        )

    def _draw_freight_table(self, y_top: float, freight_page: FreightPageContent) -> float:
        height = 70
        y_bottom = y_top - height
        self._section_box(self.left, y_bottom, self.width, height)
        widths = [
            self.width * 0.34,
            self.width * 0.14,
            self.width * 0.18,
            self.width * 0.10,
            self.width * 0.12,
            self.width * 0.12,
        ]
        xs = [self.left]
        for width in widths[:-1]:
            xs.append(xs[-1] + width)
        for x in xs[1:]:
            self._vline(x, y_bottom, height, 0.45)
        header_h = 18
        self._hline(self.left, y_top - header_h, self.width, 0.45)
        headers = ["FREIGHT AND CHARGES", "RATE", "UNIT", "CURRENCY", "PREPAID", "COLLECT"]
        for index, header in enumerate(headers):
            self._text(xs[index] + widths[index] / 2, y_top - 12, header, FONT_BOLD, 6.8, align="center")

        row_h = 9.8
        row_y = y_top - header_h - 8.3
        for index, charge in enumerate(freight_page.charges[:FREIGHT_ROWS_PER_PAGE]):
            self._validate_charge_payment(charge)
            prepaid = charge.prepaid_amount if self._has_nonzero_amount(charge.prepaid_amount) else ""
            collect = charge.collect_amount if self._has_nonzero_amount(charge.collect_amount) else ""
            values = [
                charge.description,
                self._money(charge.rate),
                charge.unit,
                charge.currency,
                self._money(prepaid),
                self._money(collect),
            ]
            for col, value in enumerate(values):
                align = "right" if col in {1, 4, 5} else "center" if col == 3 else "left"
                x = xs[col] + (widths[col] - 4 if align == "right" else widths[col] / 2 if align == "center" else 4)
                self._text(x, row_y - index * row_h, value, FONT, 6.1, align=align)
        if freight_page.show_total:
            total_y = row_y - (len(freight_page.charges[:FREIGHT_ROWS_PER_PAGE]) * row_h)
            self._hline(self.left, total_y + 5.2, self.width, 0.45)
            prepaid_total, collect_total = self._freight_totals()
            self._text(xs[0] + 4, total_y, "TOTAL FREIGHT", FONT_BOLD, 6.3)
            if prepaid_total:
                self._text(xs[4] + widths[4] - 4, total_y, self._format_decimal_money(prepaid_total), FONT_BOLD, 6.3, align="right")
            if collect_total:
                self._text(xs[5] + widths[5] - 4, total_y, self._format_decimal_money(collect_total), FONT_BOLD, 6.3, align="right")
        return y_bottom

    def _draw_footer(self, y_top: float, page_config: DocumentPageConfig) -> None:
        height = y_top - self.bottom
        x_mid = self.left + self.width * 0.50
        self._section_box(self.left, self.bottom, self.width, height)
        self._vline(x_mid, self.bottom, height, 0.6)

        left_w = self.width * 0.50
        right_w = self.width * 0.50
        y = y_top - 9
        self._text(self.left + 5, y, "FREIGHT FORWARDER'S RECEIPT", FONT_BOLD, 7.2)
        self._wrapped_text(
            self.left + 5,
            y - 11,
            "Freight Forwarder's Receipt. Total number of containers or packages received for forwarding:",
            left_w - 10,
            6.7,
            max_lines=2,
        )
        center_y = self.bottom + height * 0.44
        self._text(
            self.left + left_w / 2,
            center_y,
            self._forwarder_receipt_text(),
            FONT_BOLD,
            8,
            align="center",
        )
        lower_y = self.bottom + height * 0.24
        self._text(self.left + 5, lower_y, f"No. of original B(s)/L: {self.data.shipment.number_of_originals or 'THREE(3)'}", FONT_BOLD, 7.0)
        self._text(self.left + left_w - 5, lower_y, f"Movement: {self.data.shipment.movement}", FONT_BOLD, 7.0, align="right")
        self._text(self.left + 5, self.bottom + 7, page_config.display, FONT_BOLD, 7.0)

        right_x = x_mid + 5
        if not self.draft:
            self._text(right_x, y, "PLACE AND DATE OF ISSUE", FONT_BOLD, 7.0)
            self._text(right_x, y - 13, self._issued_place_datetime(), FONT, 7.4)
        qr_size = 34
        qr_column_w = 62
        qr_center_x = x_mid + right_w - (qr_column_w / 2) - 6
        qr_x = qr_center_x - qr_size / 2
        qr_y = self.bottom + 38
        line_y = self.bottom + 31
        signature_w = qr_x - right_x - 16
        if self.draft:
            self._draw_draft_disclaimer(right_x, y, right_w - 10)
        else:
            self._draw_signature_image(right_x, line_y - 8, signature_w, max_height=30)
            self._hline(right_x, line_y, signature_w, 0.45)
            self._text(
                right_x + signature_w / 2,
                line_y - 10,
                "Andrea Piedad Velasquez Castellon",
                FONT,
                6.8,
                align="center",
            )
            self._text(
                right_x + signature_w / 2,
                line_y - 19,
                "For MTM Logix Guatemala Sociedad Anonima",
                FONT,
                6.6,
                align="center",
            )
        if self.include_verification:
            self._draw_verification_block(qr_center_x, qr_y, qr_size, page_config)

    def _draw_draft_disclaimer(self, x: float, y_top: float, width: float) -> None:
        issued_at = datetime.now().strftime("%d-%b-%Y %H:%M").upper()
        rest = (
            ". This document is provided for verification purposes only and shall not be "
            "considered a legal, negotiable, original, final, or binding House Bill of Lading "
            "unless duly issued and signed by MTM Logix."
        )
        self._rich_wrapped_text(
            x,
            y_top,
            [("DRAFT issued on ", FONT), (issued_at, FONT_BOLD), (rest, FONT)],
            width,
            7.0,
            line_gap=8.2,
            max_lines=5,
        )

    def _issued_place_datetime(self) -> str:
        place = (self.data.shipment.issue_place or "GUATEMALA").strip().upper()
        issued_at = self.issued_at.strftime("%d-%b-%Y %H:%M").upper()
        return f"{place}, {issued_at}"

    def _draw_signature_image(
        self,
        x: float,
        y: float,
        width: float,
        *,
        max_height: float,
    ) -> None:
        if not DEFAULT_SIGNATURE_IMAGE_PATH.exists():
            return
        try:
            signature = self._signature_reader(DEFAULT_SIGNATURE_IMAGE_PATH)
            image_width, image_height = signature.getSize()
            scale = min(width * 0.86 / image_width, max_height / image_height)
            draw_width = image_width * scale
            draw_height = image_height * scale
            self.pdf.drawImage(
                signature,
                x + (width - draw_width) / 2,
                y,
                draw_width,
                draw_height,
                mask="auto",
                preserveAspectRatio=True,
            )
        except Exception:
            return

    def _section_box(self, x: float, y: float, w: float, h: float) -> None:
        self.pdf.setStrokeColor(LINE_COLOR)
        self.pdf.setLineWidth(0.6)
        self.pdf.rect(x, y, w, h, stroke=1, fill=0)

    def _draw_verification_block(
        self,
        center_x: float,
        y: float,
        size: float,
        page_config: DocumentPageConfig,
    ) -> None:
        x = center_x - size / 2
        verification_id = verification_id_for_page(
            self.data,
            page_config,
            suffix=self.verification_id_suffix,
        )
        verification_url = verification_url_for_page(
            self.data,
            page_config,
            self.verification_base_url,
            suffix=self.verification_id_suffix,
        )
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=1,
        )
        qr.add_data(verification_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        self.pdf.drawImage(
            ImageReader(buffer),
            x,
            y,
            size,
            size,
            preserveAspectRatio=True,
            mask="auto",
        )
        text_x = center_x
        self._text(text_x, y - 7, "Verify document", FONT_BOLD, 5.2, align="center")
        self._text(text_x, y - 14, "ID:", FONT, 4.8, align="center")
        self._text(text_x, y - 21, verification_id, FONT, 3.8, align="center")

    @staticmethod
    def _signature_reader(path: Path) -> ImageReader:
        if Image is None:
            return ImageReader(str(path))
        image = Image.open(path).convert("RGBA")
        pixels = image.load()
        for row in range(image.height):
            for col in range(image.width):
                red, green, blue, _alpha = pixels[col, row]
                luminance = (red * 0.299) + (green * 0.587) + (blue * 0.114)
                if luminance > 170:
                    pixels[col, row] = (255, 255, 255, 0)
                else:
                    alpha = 255 if luminance < 95 else 190
                    pixels[col, row] = (0, 0, 0, alpha)
        bbox = image.getchannel("A").getbbox()
        if bbox:
            pad = 12
            image = image.crop(
                (
                    max(bbox[0] - pad, 0),
                    max(bbox[1] - pad, 0),
                    min(bbox[2] + pad, image.width),
                    min(bbox[3] + pad, image.height),
                )
            )
        return ImageReader(image)

    def _vline(self, x: float, y: float, h: float, width: float) -> None:
        self.pdf.setLineWidth(width)
        self.pdf.line(x, y, x, y + h)

    def _hline(self, x: float, y: float, w: float, width: float) -> None:
        self.pdf.setLineWidth(width)
        self.pdf.line(x, y, x + w, y)

    def _draw_labeled_block(self, x: float, y: float, w: float, h: float, label: str, value: str) -> None:
        pad = 5
        self._text(x + pad, y + h - 10, label, FONT_BOLD, 6.8)
        lines = [line for line in value.splitlines() if line.strip()]
        if not lines:
            return
        current_y = y + h - 22
        line_count = len(lines)
        name_size = 7.0 if line_count > 5 else 7.5
        detail_size = 6.1 if line_count > 5 else 7.0
        detail_gap = 7.0 if line_count > 5 else 8.3
        name_lines = self._wrapped_text(
            x + pad,
            current_y,
            lines[0],
            w - (2 * pad),
            name_size,
            font=FONT_BOLD,
            max_lines=2,
        )
        current_y -= max(1, name_lines) * (name_size + 1.8)
        if len(lines) > 1:
            available_height = max(current_y - (y + 4), 0)
            max_detail_lines = max(int(available_height // detail_gap), 0)
            self._wrapped_lines(
                x + pad,
                current_y,
                lines[1:],
                w - (2 * pad),
                detail_size,
                line_gap=detail_gap,
                max_lines=max_detail_lines,
            )

    def _label_value(self, x: float, y: float, w: float, h: float, label: str, value: str, *, bold_value: bool = False) -> None:
        self._section_box(x, y - h + 3, w, h)
        self._text(x + 4, y - 7, f"{label}:", FONT_BOLD, 6.8)
        self._text(x + 62, y - 7, value, FONT_BOLD if bold_value else FONT, 7.6)

    def _text(
        self,
        x: float,
        y: float,
        text: object,
        font: str,
        size: float,
        *,
        align: str = "left",
        color=black,
    ) -> None:
        self.pdf.setFillColor(color)
        self.pdf.setFont(font, size)
        value = str(text or "")
        if align == "center":
            self.pdf.drawCentredString(x, y, value)
        elif align == "right":
            self.pdf.drawRightString(x, y, value)
        else:
            self.pdf.drawString(x, y, value)

    def _draw_lines(self, x: float, y: float, lines: list[str], width: float, size: float, *, line_gap: float) -> None:
        self._wrapped_lines(x, y, lines, width, size, line_gap=line_gap)

    def _draw_visual_lines(
        self,
        x: float,
        y: float,
        lines: list[str],
        size: float,
        *,
        font: str = FONT,
        line_gap: float,
    ) -> int:
        for index, line in enumerate(lines):
            self._text(x, y - (index * line_gap), line, font, size)
        return len(lines)

    def _wrapped_text(
        self,
        x: float,
        y: float,
        text: str,
        width: float,
        size: float,
        *,
        font: str = FONT,
        max_lines: int = 3,
        align: str = "left",
    ) -> int:
        return self._wrapped_lines(x, y, self._wrap(text, width, font, size), width, size, font=font, max_lines=max_lines, align=align)

    def _wrapped_lines(
        self,
        x: float,
        y: float,
        lines: list[str],
        width: float,
        size: float,
        *,
        font: str = FONT,
        line_gap: float | None = None,
        max_lines: int = 99,
        align: str = "left",
    ) -> int:
        gap = line_gap or size + 1.8
        drawn = 0
        for source_line in lines:
            wrapped = self._wrap(source_line, width, font, size) if source_line else [""]
            for line in wrapped:
                if drawn >= max_lines:
                    return drawn
                tx = x + (width / 2 if align == "center" else width if align == "right" else 0)
                self._text(tx, y - (drawn * gap), line, font, size, align=align)
                drawn += 1
        return drawn

    def _wrapped_line_count(self, lines: list[str], width: float, font: str, size: float) -> int:
        count = 0
        for source_line in lines:
            count += len(self._wrap(source_line, width, font, size)) if source_line else 1
        return count

    def _max_lines_for_height(self, y_top: float, y_bottom: float, line_gap: float) -> int:
        return max(1, int((y_top - y_bottom) // line_gap) + 1)

    def _fitted_text_style(
        self,
        lines: list[str],
        width: float,
        y_top: float,
        y_bottom: float,
        *,
        candidates: list[tuple[float, float]],
        font: str = FONT,
    ) -> tuple[float, float]:
        for size, gap in candidates:
            if self._wrapped_line_count(lines, width, font, size) <= self._max_lines_for_height(y_top, y_bottom, gap):
                return size, gap
        return candidates[-1]

    def _rich_wrapped_text(
        self,
        x: float,
        y: float,
        runs: list[tuple[str, str]],
        width: float,
        size: float,
        *,
        line_gap: float,
        max_lines: int,
    ) -> int:
        tokens: list[tuple[str, str]] = []
        for text, font in runs:
            for token in re.findall(r"\S+\s*", text):
                tokens.append((token, font))
        lines: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        current_width = 0.0
        for token, font in tokens:
            token_width = pdfmetrics.stringWidth(token, font, size)
            if current and current_width + token_width > width:
                lines.append(current)
                current = [(token, font)]
                current_width = token_width
            else:
                current.append((token, font))
                current_width += token_width
        if current:
            lines.append(current)

        for line_index, line in enumerate(lines[:max_lines]):
            cursor_x = x
            grouped = self._group_rich_line(line)
            for text, font in grouped:
                self.pdf.setFont(font, size)
                self.pdf.setFillColor(black)
                self.pdf.drawString(cursor_x, y - (line_index * line_gap), text)
                cursor_x += pdfmetrics.stringWidth(text, font, size)
        return min(len(lines), max_lines)

    @staticmethod
    def _group_rich_line(line: list[tuple[str, str]]) -> list[tuple[str, str]]:
        grouped: list[tuple[str, str]] = []
        for token, font in line:
            if grouped and grouped[-1][1] == font:
                grouped[-1] = (grouped[-1][0] + token, font)
            else:
                grouped.append((token, font))
        return grouped

    @staticmethod
    def _wrap(text: str, width: float, font: str, size: float) -> list[str]:
        words = str(text or "").split()
        if not words:
            return [""]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if pdfmetrics.stringWidth(trial, font, size) <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _first_marks(self) -> list[str]:
        if not self.data.containers:
            return ["N/M"]
        marks = self.data.containers[0].marks_and_numbers.strip()
        return [line.strip() for line in marks.splitlines() if line.strip()] if marks else ["N/M"]

    def _charge_lines(self) -> list[ChargeLine]:
        return _charge_lines_for_data(self.data)

    def _display_charge_lines(self) -> list[ChargeLine]:
        return _display_charge_lines_for_data(self.data)

    def _freight_totals(self) -> tuple[Decimal, Decimal]:
        prepaid_total = Decimal("0")
        collect_total = Decimal("0")
        for charge in self._display_charge_lines():
            if not charge.include_in_total:
                continue
            prepaid_total += self._amount_decimal(charge.prepaid_amount)
            collect_total += self._amount_decimal(charge.collect_amount)
        return prepaid_total, collect_total

    @staticmethod
    def _validate_charge_payment(charge: ChargeLine) -> None:
        if (
            BillOfLadingPackageRenderer._has_nonzero_amount(charge.prepaid_amount)
            and BillOfLadingPackageRenderer._has_nonzero_amount(charge.collect_amount)
        ):
            raise ValueError(f"Freight row cannot populate both prepaid and collect: {charge.description}")

    @staticmethod
    def _has_nonzero_amount(value: str) -> bool:
        if not value:
            return False
        try:
            return Decimal(str(value).replace(",", "").strip()) != 0
        except InvalidOperation:
            return bool(str(value).strip())

    @staticmethod
    def _amount_decimal(value: str) -> Decimal:
        if not value:
            return Decimal("0")
        try:
            return Decimal(str(value).replace(",", "").strip())
        except InvalidOperation:
            return Decimal("0")

    def _forwarder_receipt_text(self) -> str:
        count = str(len(self.data.containers) or self.data.carrier_receipt.container_count_numeric or "").strip()
        word = self.data.carrier_receipt.container_count_words or self._number_word(count)
        label = "CONTAINER" if count == "1" else "CONTAINERS"
        return f"{word} ({count}) {label}" if count else f"{word} CONTAINERS"

    @staticmethod
    def _number_word(count: str) -> str:
        return {
            "1": "ONE",
            "2": "TWO",
            "3": "THREE",
            "4": "FOUR",
            "5": "FIVE",
        }.get(count, count)

    def _weight_text(self, container: Container) -> str:
        return self._format_amount_with_unit(container.gross_weight, container.gross_weight_unit or "KGS", 3)

    def _measurement_text(self, container: Container) -> str:
        return self._format_amount_with_unit(container.measurement, container.measurement_unit or "CBM", 3)

    @staticmethod
    def _money(value: str) -> str:
        if not value:
            return ""
        try:
            return BillOfLadingPackageRenderer._format_decimal_money(Decimal(str(value).replace(',', '').strip()))
        except InvalidOperation:
            return value

    @staticmethod
    def _format_decimal_money(value: Decimal) -> str:
        return f"{value:,.2f}"

    @staticmethod
    def _format_amount_with_unit(value: str, unit: str, decimals: int) -> str:
        if not value:
            return ""
        try:
            number = Decimal(str(value).replace(",", "").strip())
            return f"{number:,.{decimals}f} {unit}".strip()
        except InvalidOperation:
            return f"{value} {unit}".strip()
