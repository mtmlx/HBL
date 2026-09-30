from mtm_hbl.config import AppConfig, load_yaml
from mtm_hbl.models.canonical import CanonicalHblData, ChargeLine, Container, QaIssue


def restore_trusted_package_exception(data: CanonicalHblData, app_config: AppConfig) -> None:
    """Rebuild a narrow approved exception from installed configuration, never QA input."""
    if data.scope.owner_country != "Guatemala":
        return
    for path in sorted((app_config.config_dir / "customers").glob("*.yaml")):
        for learned in load_yaml(path).get("learned_examples", {}).values():
            cargo = learned.get("cargo") or {}
            containers = learned.get("containers") or []
            if (
                learned.get("approved_override") is True
                and learned.get("package_count_source") == "total_only"
                and learned.get("hbl_no") == data.shipment.mtm_hbl_no
                and str(cargo.get("total_packages", "")) == data.cargo.total_packages
                and containers
                and sorted(str(item.get("container_no", "")) for item in containers)
                == sorted(item.container_no for item in data.containers)
            ):
                _apply_learned_example(data, {"package_count_source": "total_only"})
                return


def apply_customer_profile(
    data: CanonicalHblData,
    app_config: AppConfig,
    customer_slug: str = "",
) -> CanonicalHblData:
    if not customer_slug:
        return data

    profile_path = app_config.config_dir / "customers" / f"{customer_slug}.yaml"
    if not profile_path.exists():
        data.qa.soft_warnings.append(
            QaIssue(
                id="customer_profile_missing",
                severity="soft_warning",
                field="customer",
                message=f"Customer profile was requested but not found: {profile_path}",
                blocking_scope="none",
                recommended_action="Create the customer profile or run without customer-specific rules.",
            )
        )
        data.qa.manual_review_required = True
        return data

    profile = load_yaml(profile_path)
    _apply_party_rules(data, profile)
    examples = profile.get("learned_examples", {})
    learned = examples.get(data.shipment.clickup_task_id) or examples.get(data.shipment.mtm_hbl_no) or {}
    if learned:
        _apply_learned_example(data, learned)
    return data


def _apply_party_rules(data: CanonicalHblData, profile: dict) -> None:
    rules = profile.get("party_rules", {})
    if rules.get("notify_party") == "same_as_consignee_when_agent_says_same":
        if data.parties.notify_party.raw_text == data.parties.consignee.raw_text:
            data.parties.notify_party.raw_text = "SAME AS CONSIGNEE"
    if rules.get("delivery_apply_to") == "consignee" and not data.parties.delivery_apply_to.raw_text:
        data.parties.delivery_apply_to.raw_text = data.parties.consignee.raw_text
    _apply_party_overrides(data, profile)
    if rules.get("delivery_apply_to") == "consignee":
        data.parties.delivery_apply_to.raw_text = data.parties.consignee.raw_text


def _apply_party_overrides(data: CanonicalHblData, profile: dict) -> None:
    overrides = profile.get("party_overrides", {})
    for field in ["shipper", "consignee", "notify_party", "delivery_apply_to"]:
        party = getattr(data.parties, field)
        for item in overrides.get(field, []):
            token = str(item.get("when_contains", "")).upper()
            replacement = str(item.get("raw_text", ""))
            if token and replacement and token in party.raw_text.upper():
                party.raw_text = replacement.rstrip()
                break


def _apply_learned_example(data: CanonicalHblData, learned: dict) -> None:
    if learned.get("mbl_no"):
        data.shipment.mbl_no = str(learned["mbl_no"])
    if learned.get("issue_date"):
        data.shipment.issue_date = str(learned["issue_date"])
    if learned.get("vessel_voyage"):
        vessel_voyage = str(learned["vessel_voyage"])
        data.shipment.vessel = vessel_voyage
        data.shipment.voyage = ""
    if learned.get("number_of_originals"):
        data.shipment.number_of_originals = str(learned["number_of_originals"])
    if learned.get("movement"):
        data.shipment.movement = str(learned["movement"])
    if learned.get("freight_term"):
        data.shipment.freight_term = str(learned["freight_term"])
    _apply_learned_parties(data, learned)
    _apply_learned_routing(data, learned)
    _apply_learned_cargo(data, learned)
    _apply_learned_containers(data, learned)
    if learned.get("approved_override"):
        _append_warning_once(
            data,
            QaIssue(
                id="approved_team_override_applied",
                severity="soft_warning",
                field="customer",
                message=(
                    "An approved team override was applied because the source PDFs were "
                    "superseded by operational review comments."
                ),
                blocking_scope="none",
                recommended_action="Keep the override trace with the review packet and validate before final issuance.",
            ),
        )
        data.qa.manual_review_required = True
    if learned.get("package_count_source") == "total_only":
        _append_warning_once(
            data,
            QaIssue(
                id="package_counts_total_only",
                severity="soft_warning",
                field="containers.package_count",
                message=(
                    "Only the total package count was approved; per-container package counts "
                    "were intentionally left blank."
                ),
                blocking_scope="none",
                recommended_action="Confirm the total package count is sufficient for this HBL.",
            ),
        )
        data.qa.manual_review_required = True
    charge_lines = learned.get("charge_lines") or []
    if charge_lines:
        charge_source = str(learned.get("charge_source", "learned finished HBL example"))
        data.charges.line_items = [
            ChargeLine(
                description=str(item.get("description", "")),
                rate=str(item.get("rate", "")),
                unit=str(item.get("unit", "Per Container")),
                currency=str(item.get("currency", "USD")),
                prepaid_amount=str(item.get("prepaid_amount", "")),
                collect_amount=str(item.get("collect_amount", "")),
            )
            for item in charge_lines
        ]
        data.qa.soft_warnings = [
            issue for issue in data.qa.soft_warnings if issue.id != "freight_rate_missing_clickup"
        ]
        _append_warning_once(
            data,
            QaIssue(
                id="training_charge_rows_from_finished_hbl",
                severity="soft_warning",
                field="charges",
                message=(
                    f"Freight charge rows were populated from {charge_source}. "
                    "Map the live ClickUp charge fields before using this without review."
                ),
                blocking_scope="none",
                recommended_action="Validate freight charges against the shipment quotation/rate fields.",
            )
        )
        data.qa.manual_review_required = True
    if learned.get("learned_from_finished_hbl") and any(
        learned.get(key) for key in ["parties", "routing", "cargo", "containers"]
    ):
        _append_warning_once(
            data,
            QaIssue(
                id="training_fields_from_finished_hbl",
                severity="soft_warning",
                field="customer",
                message=(
                    "Some HBL fields were populated from a learned finished HBL example because "
                    "the team-issued document is the precision reference for this customer rollout."
                ),
                blocking_scope="none",
                recommended_action=(
                    "Validate these learned values against ClickUp and the team-issued HBL before "
                    "promoting the rule to a reusable customer rule."
                ),
            ),
        )
        data.qa.manual_review_required = True


def _apply_learned_parties(data: CanonicalHblData, learned: dict) -> None:
    parties = learned.get("parties") or {}
    for field in ["shipper", "consignee", "notify_party", "delivery_apply_to"]:
        value = parties.get(field)
        if value:
            getattr(data.parties, field).raw_text = str(value).rstrip()


def _apply_learned_routing(data: CanonicalHblData, learned: dict) -> None:
    routing = learned.get("routing") or {}
    for field in ["place_of_receipt", "port_of_loading", "port_of_discharge", "place_of_delivery"]:
        value = routing.get(field)
        if value:
            setattr(data.routing, field, str(value))


def _apply_learned_cargo(data: CanonicalHblData, learned: dict) -> None:
    cargo = learned.get("cargo") or {}
    for field in ["description_raw", "total_packages", "package_type", "gross_weight", "measurement"]:
        value = cargo.get(field)
        if value:
            setattr(data.cargo, field, str(value))


def _apply_learned_containers(data: CanonicalHblData, learned: dict) -> None:
    containers = learned.get("containers") or []
    if not containers:
        return
    data.containers = [
        Container(
            container_no=str(item.get("container_no", "")),
            seal_no=str(item.get("seal_no", "")),
            container_type=str(item.get("container_type", "")),
            package_count=str(item.get("package_count", "")),
            package_type=str(item.get("package_type", "")),
            gross_weight=str(item.get("gross_weight", "")),
            measurement=str(item.get("measurement", "")),
            marks_and_numbers=str(item.get("marks_and_numbers", "")).rstrip(),
        )
        for item in containers
    ]


def _append_warning_once(data: CanonicalHblData, issue: QaIssue) -> None:
    if not any(existing.id == issue.id for existing in data.qa.soft_warnings):
        data.qa.soft_warnings.append(issue)
