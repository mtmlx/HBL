from pathlib import Path
import re

from pypdf import PdfReader
import pytest

from mtm_hbl.models.canonical import ChargeLine
from mtm_hbl.pdf.hbl_package import (
    DOCUMENT_SET,
    build_document_page_set,
    generate_bill_of_lading_draft,
    generate_bill_of_lading_package,
    split_cargo_pages,
    split_description_pages,
    verification_id_for_page,
    verification_url_for_page,
)

from tests.conftest import valid_data


def package_data():
    data = valid_data()
    data.shipment.mtm_hbl_no = "WH26040006"
    data.shipment.mbl_no = "ONEYTAOG71637300"
    data.shipment.vessel = "NYK LAURA"
    data.shipment.voyage = "0665E"
    data.shipment.issue_date = "GUATEMALA, 20 DE ABRIL DE 2026"
    data.shipment.number_of_originals = "THREE(3)"
    data.routing.place_of_receipt = "QINGDAO, CHINA"
    data.routing.port_of_loading = "QINGDAO, CHINA"
    data.routing.port_of_discharge = "PUERTO QUETZAL, GUATEMALA"
    data.routing.place_of_delivery = "PUERTO QUETZAL, GUATEMALA"
    data.parties.delivery_apply_to.raw_text = ""
    data.cargo.description_raw = (
        "2 x 40'HQ FCL / CY-CY\n"
        "2 containers said to contain 1,701 packages\n"
        "72 units tricycle in CKD condition and spare parts"
    )
    data.cargo.total_packages = "1701"
    data.cargo.gross_weight = "41704.000"
    data.cargo.measurement = "136.000"
    data.containers[0].container_no = "ONEU5863839"
    data.containers[0].seal_no = "CN47063BF"
    data.containers[0].gross_weight = "20789.000"
    data.containers[0].measurement = "68.000"
    data.containers[0].package_count = "850"
    data.containers[0].marks_and_numbers = "N/M"
    second = data.containers[0].model_copy(deep=True)
    second.package_count = "851"
    second.container_no = "NYKU5174452"
    second.seal_no = "CN47140BF"
    second.gross_weight = "20915.000"
    second.measurement = "68.000"
    data.containers.append(second)
    data.carrier_receipt.container_count_numeric = "2"
    data.carrier_receipt.container_count_words = "TWO"
    data.charges.line_items = [
        ChargeLine(
            description="Ocean Basic Freight",
            rate="2500.00",
            unit="PER CONTAINER",
            currency="USD",
            collect_amount="5000.00",
        )
    ]
    return data


def test_hbl_package_generates_three_originals_and_three_copies(tmp_path):
    output = tmp_path / "package.pdf"

    generate_bill_of_lading_package(package_data(), output)

    reader = PdfReader(str(output))
    assert len(reader.pages) == 6
    expected = [
        "ORIGINAL 1/3",
        "ORIGINAL 2/3",
        "ORIGINAL 3/3",
        "COPY 1/3",
        "COPY 2/3",
        "COPY 3/3",
    ]
    for page, label in zip(reader.pages, expected):
        text = page.extract_text()
        assert label in text
    assert "ORIGINAL" in reader.pages[0].extract_text()
    assert "COPY" in reader.pages[5].extract_text()


def test_hbl_draft_generates_one_page_without_qr_verification(tmp_path):
    output = tmp_path / "draft.pdf"

    generate_bill_of_lading_draft(package_data(), output)

    reader = PdfReader(str(output))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    normalized_text = re.sub(r"\s+", " ", text)
    assert "DRAFT 1/1" in text
    assert "DRAFT issued on" in text
    assert "verification purposes only" in normalized_text
    assert "legal, negotiable, original, final, or binding House Bill of Lading" in normalized_text
    assert "Verify document" not in text
    assert "Andrea Piedad Velasquez Castellon" not in text
    assert "For MTM Logix Guatemala Sociedad Anonima" not in text
    assert "ORIGINAL 1/3" not in text
    assert "COPY 1/3" not in text


def test_party_blocks_show_five_consignee_and_notify_lines(tmp_path):
    data = package_data()
    data.parties.consignee.raw_text = "\n".join(
        [
            "CONSIGNEE LEGAL NAME",
            "CONSIGNEE ADDRESS LINE 1",
            "CONSIGNEE ADDRESS LINE 2",
            "CONSIGNEE TAX ID LINE",
            "CONSIGNEE PHONE LINE",
        ]
    )
    data.parties.notify_party.raw_text = "\n".join(
        [
            "NOTIFY LEGAL NAME",
            "NOTIFY ADDRESS LINE 1",
            "NOTIFY ADDRESS LINE 2",
            "NOTIFY TAX ID LINE",
            "NOTIFY PHONE LINE",
        ]
    )
    output = tmp_path / "party-lines.pdf"

    generate_bill_of_lading_draft(data, output)

    text = PdfReader(str(output)).pages[0].extract_text()
    assert "CONSIGNEE PHONE LINE" in text
    assert "NOTIFY PHONE LINE" in text


def test_long_description_generates_continuation_pages_without_shrinking(tmp_path):
    data = package_data()
    data.cargo.description_raw = "\n".join(
        f"Line {index:02d} - spare parts and machinery components for production line verification"
        for index in range(1, 60)
    )
    output = tmp_path / "long-package.pdf"

    generate_bill_of_lading_package(data, output)

    reader = PdfReader(str(output))
    page_total = len(split_description_pages(data))
    assert page_total > 1
    assert len(reader.pages) == 6 * page_total

    first_page = reader.pages[0].extract_text()
    continuation_page = reader.pages[1].extract_text()
    last_page_in_first_original_set = reader.pages[page_total - 1].extract_text()
    assert "ORIGINAL 1/3" in first_page
    assert f"1 of {page_total}" in first_page
    assert "FREIGHT AND CHARGES" not in first_page
    assert "ORIGINAL 1/3" in continuation_page
    assert f"2 of {page_total}" in continuation_page
    assert "SHIPPER" in continuation_page
    assert "PARTICULARS FURNISHED BY SHIPPER" in continuation_page
    assert "CONTAINER: ONEU5863839" in first_page
    assert "CONTAINER: ONEU5863839" not in continuation_page
    assert "FREIGHT AND CHARGES" in last_page_in_first_original_set
    assert "Andrea Piedad Velasquez Castellon" in continuation_page
    assert f"WH26040006-O1-P2" in continuation_page


def test_long_description_draft_can_generate_continuation_pages_without_qr(tmp_path):
    data = package_data()
    data.cargo.description_raw = "\n".join(
        f"Line {index:02d} - detailed cargo description for draft review"
        for index in range(1, 60)
    )
    output = tmp_path / "long-draft.pdf"

    generate_bill_of_lading_draft(data, output)

    reader = PdfReader(str(output))
    page_total = len(split_description_pages(data))
    assert page_total > 1
    assert len(reader.pages) == page_total
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "DRAFT 1/1" in text
    assert f"2 of {page_total}" in text
    assert "Verify document" not in text
    assert "FREIGHT AND CHARGES" not in reader.pages[0].extract_text()
    assert "FREIGHT AND CHARGES" in reader.pages[-1].extract_text()


def test_long_container_list_continues_without_repeating_prior_containers(tmp_path):
    data = package_data()
    data.cargo.description_raw = "Short cargo description"
    base = data.containers[0]
    data.containers = []
    for index in range(1, 12):
        container = base.model_copy(deep=True)
        container.container_no = f"TLLU{index:07d}"
        container.seal_no = f"SEAL{index:04d}"
        container.gross_weight = "1000.000"
        container.measurement = "10.000"
        data.containers.append(container)
    data.cargo.gross_weight = "11000.000"
    data.cargo.measurement = "110.000"
    output = tmp_path / "many-containers.pdf"

    generate_bill_of_lading_package(data, output)

    reader = PdfReader(str(output))
    page_total = len(split_cargo_pages(data))
    assert page_total > 1
    assert len(reader.pages) == 6 * page_total
    page_1 = reader.pages[0].extract_text()
    page_2 = reader.pages[1].extract_text()
    assert "CONTAINER: TLLU0000001" in page_1
    assert "CONTAINER: TLLU0000001" not in page_2
    assert "CONTAINER: TLLU0000009" in page_2


def test_hbl_package_uses_freight_forwarder_terminology(tmp_path):
    output = tmp_path / "package.pdf"

    generate_bill_of_lading_package(package_data(), output)

    text = "\n".join(page.extract_text() for page in PdfReader(str(output)).pages)
    normalized = re.sub(r"\s+", " ", text)
    assert "Freight Forwarder's Receipt" in text
    assert "HBL NO." in text
    assert "BL No." not in text
    assert "received for forwarding" in normalized
    assert "Andrea Piedad Velasquez Castellon" in text
    assert "For MTM Logix Guatemala Sociedad Anonima" in text
    assert "Verify document" in text
    assert "WH26040006-O1" in text
    assert "Goods to be delivered to:" in text or "GOODS TO BE DELIVERED TO:" in text
    assert "Shipper's load, stow, count and seal." in text
    assert "Particulars furnished by shipper." in text
    assert "No. of original B(s)/L: THREE(3)" in text
    assert "TWO (2) CONTAINERS" in text
    assert "Carrier's Receipt" not in text
    assert "received by Carrier" not in text
    assert "Stamp and signature" not in text
    assert "Signature of issuing freight forwarder" not in text
    assert "For the delivery of goods please apply to" not in text
    assert "Freight payable at" not in text


def test_hbl_package_allows_zero_originals_label(tmp_path):
    data = package_data()
    data.shipment.number_of_originals = "ZERO (0)"
    output = tmp_path / "zero-originals.pdf"

    generate_bill_of_lading_package(data, output)

    text = "\n".join(page.extract_text() for page in PdfReader(str(output)).pages)
    assert "No. of original B(s)/L: ZERO (0)" in text


def test_hbl_package_rejects_charge_with_prepaid_and_collect(tmp_path):
    data = package_data()
    data.charges.line_items[0].prepaid_amount = "5000.00"

    with pytest.raises(ValueError, match="both prepaid and collect"):
        generate_bill_of_lading_package(data, tmp_path / "bad.pdf")


def test_hbl_package_treats_zero_prepaid_as_blank_when_collect_is_populated(tmp_path):
    data = package_data()
    data.charges.line_items[0].prepaid_amount = "0"

    output = tmp_path / "package.pdf"
    generate_bill_of_lading_package(data, output)

    text = "\n".join(page.extract_text() for page in PdfReader(str(output)).pages)
    assert "5,000.00" in text


def test_verification_ids_and_urls_are_stable():
    data = package_data()

    assert verification_id_for_page(data, DOCUMENT_SET[0]) == "WH26040006-O1"
    assert verification_id_for_page(data, DOCUMENT_SET[3]) == "WH26040006-C1"
    continuation_page = build_document_page_set(data)[0].__class__("ORIGINAL", 1, 3, 2, 3)
    assert verification_id_for_page(data, continuation_page) == "WH26040006-O1-P2"
    assert (
        verification_url_for_page(data, DOCUMENT_SET[0], "https://example.com")
        == "https://example.com/verify/WH26040006-O1"
    )
