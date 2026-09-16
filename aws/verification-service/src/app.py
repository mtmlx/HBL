import html
import json
import os
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import boto3


TABLE_NAME = os.environ["TABLE_NAME"]
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
TERMS_DIR = Path(__file__).resolve().parent / "terms"
DEFAULT_TERMS_LANGUAGE = "en"
TERMS_LANGUAGES = {
    "en": "English",
    "es": "Español",
    "pt-BR": "Português",
    "zh-CN": "中文",
}


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    path = event.get("rawPath", "")
    verification_id = event.get("pathParameters", {}).get("verification_id", "")
    requested_language = _requested_language(event)
    record = get_record(verification_id)

    if path.startswith("/api/verify/"):
        if not record:
            return response(
                404,
                json.dumps(
                    {
                        "status": "NOT_FOUND",
                        "verification_id": verification_id,
                    }
                ),
                "application/json",
            )
        return response(200, json.dumps(record, cls=DecimalEncoder), "application/json")

    return response(
        200 if record else 404,
        render_html(verification_id, record, requested_language),
        "text/html; charset=utf-8",
    )


def get_record(verification_id):
    if not verification_id:
        return None
    result = table.get_item(Key={"verification_id": verification_id})
    return result.get("Item")


def response(status_code, body, content_type):
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": content_type,
            "cache-control": "no-store",
        },
        "body": body,
    }


def render_html(verification_id, record, requested_language=DEFAULT_TERMS_LANGUAGE):
    language = _normalize_language(requested_language)
    if not record:
        return f"""<!doctype html>
<html>
<head>
  <title>MTM HBL Verification</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  {_styles()}
</head>
<body>
  <main class="page">
    <section class="hero">
      <p class="eyebrow">MTM Logix Document Validation</p>
      <h1>Document Not Found</h1>
      <p>Verification ID: <strong>{html.escape(verification_id)}</strong></p>
      <div class="alert alert-danger">
        This document could not be verified. Contact MTM Logix before accepting it.
      </div>
    </section>
  </main>
</body>
</html>"""

    status = str(record.get("status", "UNKNOWN"))
    status_class = "status-good" if status.upper() == "ISSUED" else "status-warning"
    warning = ""
    if status.upper() in {"VOID", "SUPERSEDED", "NOT_FOUND", "UNKNOWN"}:
        warning = (
            "<div class='alert alert-danger'>"
            f"WARNING: document status is {html.escape(status)}. Contact MTM Logix before accepting this document."
            "</div>"
        )

    document_label = " ".join(
        part
        for part in [
            str(record.get("document_type", "")),
            _sequence(record),
        ]
        if part
    )
    rows = [
        ("Status", status),
        ("HBL No.", record.get("hbl_number", "")),
        ("MBL No.", record.get("mbl_number", "")),
        ("Document", document_label),
        ("Page", _page_label(record)),
        ("Issued at", record.get("issued_at", "")),
        ("Issued by", record.get("issued_by", "")),
        ("Terms Version", record.get("terms_version", "3.0")),
        ("Terms Effective Date", record.get("terms_effective_date_display", "11-JUN-2026")),
        ("PDF SHA-256", record.get("pdf_sha256", "")),
        ("Package ID", record.get("package_id", "")),
    ]

    row_html = "\n".join(
        f"<tr><th>{html.escape(label)}</th><td>{_value_html(label, value, status_class)}</td></tr>"
        for label, value in rows
    )
    terms_html = render_terms_section(language, verification_id)

    return f"""<!doctype html>
<html>
<head>
  <title>MTM HBL Verification</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  {_styles()}
</head>
<body>
  <main class="page">
    <section class="hero">
      <p class="eyebrow">MTM Logix Document Validation</p>
      <div class="hero-grid">
        <div>
          <h1>House Bill of Lading Verification</h1>
          <p class="subhead">This page verifies issuance metadata and the terms version incorporated into the document.</p>
        </div>
        <div class="verification-id">
          <span>Verification ID</span>
          <strong>{html.escape(verification_id)}</strong>
        </div>
      </div>
      {warning}
    </section>

    <section class="card">
      <div class="section-heading">
        <h2>HBL Specific Information</h2>
        <span class="pill {status_class}">{html.escape(status)}</span>
      </div>
      <table class="metadata-table">
        {row_html}
      </table>
      <p class="footnote">
        This page verifies issuance metadata only. It does not expose the PDF publicly.
      </p>
    </section>

    {terms_html}
  </main>
</body>
</html>"""


def _sequence(record):
    sequence = record.get("sequence")
    total = record.get("sequence_total")
    if sequence and total:
        return f"{sequence}/{total}"
    return ""


def _page_label(record):
    page = record.get("page_number")
    total = record.get("page_total")
    if page and total:
        return f"{page} of {total}"
    return ""


def _requested_language(event):
    params = event.get("queryStringParameters") or {}
    return params.get("lang") or params.get("language") or DEFAULT_TERMS_LANGUAGE


def _normalize_language(language):
    value = str(language or "").strip()
    if value in TERMS_LANGUAGES:
        return value
    lowered = value.lower()
    for option in TERMS_LANGUAGES:
        if option.lower() == lowered:
            return option
    return DEFAULT_TERMS_LANGUAGE


def _value_html(label, value, status_class):
    escaped = html.escape(str(value or ""))
    if label == "Status":
        return f"<span class='pill {status_class}'>{escaped}</span>"
    if label in {"PDF SHA-256", "Package ID"}:
        return f"<code>{escaped}</code>"
    return escaped


def render_terms_section(language, verification_id):
    terms = load_terms(language)
    meta = terms["meta"]
    content = terms["content"]
    language_links = " ".join(
        _language_link(verification_id, code, label, language)
        for code, label in TERMS_LANGUAGES.items()
    )
    title = html.escape(meta.get("title", "MTM Logix Terms and Conditions"))
    version = html.escape(meta.get("version", "3.0"))
    effective = html.escape(meta.get("effective_date_display", "11-JUN-2026"))
    controlling = html.escape(meta.get("controlling_language", "en"))
    return f"""
    <section class="card terms-card">
      <div class="section-heading terms-heading">
        <div>
          <h2>{title}</h2>
          <p class="terms-meta">Version {version} · Effective {effective} · Controlling language: {controlling.upper()}</p>
        </div>
        <nav class="language-nav" aria-label="Terms language">
          {language_links}
        </nav>
      </div>
      <div class="terms-notice">
        These Terms and Conditions are incorporated into and form part of the Bill of Lading verified above.
      </div>
      <article class="terms-content">
        {content}
      </article>
    </section>
    """


def _language_link(verification_id, code, label, active_language):
    class_name = "active" if code == active_language else ""
    return (
        f"<a class='{class_name}' href='/verify/{html.escape(verification_id)}?lang={html.escape(code)}'>"
        f"{html.escape(label)}</a>"
    )


@lru_cache(maxsize=8)
def load_terms(language):
    normalized = _normalize_language(language)
    path = TERMS_DIR / f"mtm-logix-terms-v3.0-{normalized}.md"
    if not path.exists():
        path = TERMS_DIR / "mtm-logix-terms-v3.0-en.md"
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)
    return {"meta": meta, "content": markdown_to_html(body)}


def _parse_frontmatter(raw):
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2].strip()


def markdown_to_html(markdown):
    blocks = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            blocks.append(f"<h3>{_inline_markdown(line[3:])}</h3>")
        elif line.startswith("> "):
            blocks.append(f"<blockquote>{_inline_markdown(line[2:])}</blockquote>")
        elif re.match(r"^\d+\.\d+\s+", line):
            blocks.append(f"<p class='clause'>{_inline_markdown(line)}</p>")
        elif re.match(r"^\([a-z]\)\s+", line, flags=re.IGNORECASE):
            blocks.append(f"<p class='subclause'>{_inline_markdown(line)}</p>")
        else:
            blocks.append(f"<p>{_inline_markdown(line)}</p>")
    return "\n".join(blocks)


def _inline_markdown(text):
    escaped = html.escape(text.rstrip().rstrip("  "))
    return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", escaped)


def _styles():
    return """
  <style>
    :root {
      --ink: #111827;
      --muted: #475467;
      --line: #d0d5dd;
      --soft: #f8fafc;
      --brand: #13245a;
      --gold: #d8a600;
      --ok-bg: #ecfdf3;
      --ok: #027a48;
      --warn-bg: #fff6ed;
      --warn: #b54708;
      --danger-bg: #fef3f2;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f3f4f6;
      color: var(--ink);
      font-family: "Noto Sans", Aptos, Arial, sans-serif;
      line-height: 1.45;
    }
    .page {
      max-width: 1060px;
      margin: 0 auto;
      padding: 32px 18px 56px;
    }
    .hero,
    .card {
      background: white;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
    }
    .hero {
      padding: 26px 28px;
      margin-bottom: 16px;
    }
    .hero-grid,
    .section-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
    }
    .eyebrow {
      margin: 0 0 8px;
      color: var(--brand);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1.15;
    }
    h2 {
      margin: 0;
      font-size: 18px;
      line-height: 1.25;
    }
    h3 {
      margin: 24px 0 8px;
      padding-top: 14px;
      border-top: 1px solid #eaecf0;
      font-size: 15px;
      line-height: 1.35;
    }
    .subhead,
    .footnote,
    .terms-meta {
      color: var(--muted);
    }
    .subhead {
      margin: 8px 0 0;
      font-size: 14px;
    }
    .verification-id {
      min-width: 260px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--soft);
      text-align: right;
    }
    .verification-id span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .verification-id strong {
      display: block;
      margin-top: 4px;
      overflow-wrap: anywhere;
      font-size: 13px;
    }
    .card {
      padding: 20px 22px;
      margin-bottom: 16px;
    }
    .metadata-table {
      width: 100%;
      margin-top: 14px;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th,
    td {
      border: 1px solid var(--line);
      padding: 10px 12px;
      vertical-align: top;
      overflow-wrap: anywhere;
      font-size: 14px;
    }
    th {
      width: 210px;
      background: var(--soft);
      text-align: left;
      font-weight: 700;
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .status-good {
      background: var(--ok-bg);
      color: var(--ok);
    }
    .status-warning {
      background: var(--warn-bg);
      color: var(--warn);
    }
    .alert {
      margin-top: 16px;
      padding: 12px 14px;
      border-radius: 6px;
      font-weight: 700;
    }
    .alert-danger {
      background: var(--danger-bg);
      color: var(--danger);
      border: 1px solid #fecdca;
    }
    .footnote {
      margin: 14px 0 0;
      font-size: 12px;
    }
    .terms-heading {
      padding-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }
    .terms-meta {
      margin: 5px 0 0;
      font-size: 13px;
    }
    .language-nav {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
      min-width: 270px;
    }
    .language-nav a {
      color: var(--brand);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 700;
      text-decoration: none;
    }
    .language-nav a.active {
      color: white;
      border-color: var(--brand);
      background: var(--brand);
    }
    .terms-notice {
      margin: 16px 0;
      padding: 12px 14px;
      border-left: 4px solid var(--gold);
      background: #fffbeb;
      color: #533f03;
      font-size: 13px;
      font-weight: 700;
    }
    .terms-content {
      font-size: 13px;
    }
    .terms-content p,
    .terms-content blockquote {
      margin: 7px 0;
    }
    .terms-content blockquote {
      border-left: 3px solid var(--line);
      padding: 8px 12px;
      background: var(--soft);
      color: var(--muted);
    }
    .clause {
      padding-left: 12px;
      text-indent: -12px;
    }
    .subclause {
      padding-left: 28px;
    }
    @media (max-width: 720px) {
      .page { padding: 18px 10px 36px; }
      .hero, .card { padding: 18px; }
      .hero-grid, .section-heading { display: block; }
      .verification-id { margin-top: 16px; text-align: left; }
      .language-nav {
        justify-content: flex-start;
        min-width: 0;
        margin-top: 12px;
      }
      th, td { display: block; width: 100%; }
      th { border-bottom: 0; }
    }
  </style>
"""
