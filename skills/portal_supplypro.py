"""
SupplyPro Portal Sync skill driver with real Playwright browser automation,
Gemini 2.5 Flash 4-factor semantic reconciliation auditing, daily batch queue
processing via BurgessApi, and token/cost telemetry.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import inspect
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional

# Ensure OpenSSL legacy handshake config is active before loading SQL Server drivers on macOS
_cnf_path = Path(__file__).resolve().parent.parent / "openssl_legacy.cnf"
if _cnf_path.exists():
    os.environ.setdefault("OPENSSL_CONF", str(_cnf_path))

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import URL, create_engine, text

from core.api_client import BurgessApiClient
from po_normalizer import NormalizedPOLookup, normalize_and_split_po
from utils.cost_tracker import CostTracker, DEFAULT_GEMINI_MODEL

load_dotenv(override=True)

logger = logging.getLogger(__name__)
progress_logger = logging.getLogger("burgess_buddy.progress")


class SupplyProOrderNotFoundError(RuntimeError):
    """Raised when a PO number produces 0 matching orders in SupplyPro Order Search."""


class SupplyProSessionExpiredError(RuntimeError):
    """Raised when SupplyPro redirects an active session back to login.asp."""


class SupplyProCsrBlockedError(RuntimeError):
    """Raised when an open SupplyPro order has its 'Complete Order' action missing or disabled."""

# Gemini 2.5 Flash standard pricing (USD per 1M tokens)
GEMINI_25_FLASH_INPUT_COST_PER_1M = 0.30
GEMINI_25_FLASH_OUTPUT_COST_PER_1M = 2.50


def _default_headless() -> bool:
    """Defaults to headless=False locally so browser automation can be observed in real time."""
    env_headless = os.getenv("SUPPLYPRO_HEADLESS")
    if env_headless is not None:
        return env_headless.strip().lower() in ("true", "1", "yes")
    app_env = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "development").strip().lower()
    return app_env == "production"


def _default_slow_mo() -> int:
    """Defaults to slow_mo=300ms locally for real-time visual observability."""
    env_slow_mo = os.getenv("SUPPLYPRO_SLOW_MO")
    if env_slow_mo and env_slow_mo.isdigit():
        return int(env_slow_mo)
    return 0 if _default_headless() else 300


def _to_cents(value: Any) -> Optional[Decimal]:
    """Converts a numeric or currency string into an exact 2-decimal `Decimal` (cents)."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if isinstance(value, (int, float)):
        return Decimal(f"{float(value):.4f}").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cleaned = re.sub(r"[^\d\.\-]", "", str(value).strip())
    if not cleaned:
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _get_case_insensitive(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Finds the first non-None value matching any candidate key (exact then case-insensitive)."""
    if not isinstance(data, dict):
        return default
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    lower_map = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        val = lower_map.get(k.lower())
        if val is not None:
            return val
    return default


def _safe_json_dict(raw_val: Any) -> dict[str, Any]:
    """Safely converts a dict or JSON string into a dictionary, returning `{}` on None, empty, or invalid inputs."""
    if not raw_val:
        return {}
    if isinstance(raw_val, dict):
        return dict(raw_val)
    if isinstance(raw_val, str):
        text = raw_val.strip()
        if not text or text.lower() == "null":
            return {}
        try:
            parsed = json.loads(text)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return {}


def _coerce_optional_bool(val: Any) -> Optional[bool]:
    """Converts a boolean or truthy/falsy string into an optional bool."""
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm in ("true", "1", "yes", "on"):
            return True
        if norm in ("false", "0", "no", "off"):
            return False
    return None


def _coerce_str_list(val: Any) -> list[str]:
    """Normalizes a list of strings, JSON array string, or comma-separated string into a clean list[str]."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if v is not None and str(v).strip()]
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if v is not None and str(v).strip()]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    return [str(val).strip()] if str(val).strip() else []


def extract_marked_items(params: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extracts a normalized list of target order descriptors (`{"poNumber": ..., "invoiceNumber": ...}`)
    from `items` (list of objects), `poNumbers` / `po_numbers`, and/or `invoiceNumbers` / `invoice_numbers`.
    """
    if not isinstance(params, dict):
        return []

    normalized_items: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _add_entry(
        po_val: Optional[str] = None,
        inv_val: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        clean_po = str(po_val).strip() if po_val is not None and str(po_val).strip() else None
        clean_inv = str(inv_val).strip() if inv_val is not None and str(inv_val).strip() else None
        if not clean_po and not clean_inv:
            return
        dedup_key = (clean_inv or "", clean_po or "")
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
        entry: dict[str, Any] = {}
        if extra:
            entry.update(extra)
        if clean_po:
            entry["poNumber"] = clean_po
        if clean_inv:
            entry["invoiceNumber"] = clean_inv
        normalized_items.append(entry)

    raw_items = _get_case_insensitive(
        params,
        "items",
        "markedItems",
        "marked_items",
        "selectedItems",
        "selected_items",
    )
    if isinstance(raw_items, str) and raw_items.strip().startswith("["):
        try:
            raw_items = json.loads(raw_items.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            raw_items = None

    if isinstance(raw_items, list):
        for elem in raw_items:
            if isinstance(elem, dict):
                po_v = _get_case_insensitive(elem, "poNumber", "po_number", "po", "PONumber")
                inv_v = _get_case_insensitive(elem, "invoiceNumber", "invoice_number", "invoice", "InvoiceNumber")
                note_v = _get_case_insensitive(elem, "userNote", "user_note", "UserNote")
                ext_v = _get_case_insensitive(elem, "extractType", "extract_type", "ExtractType")
                extra_fields: dict[str, Any] = {}
                if note_v is not None and str(note_v).strip():
                    extra_fields["userNote"] = str(note_v).strip()
                if ext_v is not None and str(ext_v).strip():
                    extra_fields["extractType"] = str(ext_v).strip()
                # Preserve any inline expected fields if supplied on the item object
                for field_name in (
                    "builderName",
                    "BuilderName",
                    "propertyAddress",
                    "PropertyAddress",
                    "inspectionType",
                    "InspectionType",
                    "TypeName",
                    "typeName",
                    "fee",
                    "Fee",
                    "inspectionId",
                    "InspectionID",
                    "reinspect",
                    "Reinspect",
                    "is_reinspect",
                    "isReinspect",
                ):
                    if field_name in elem and elem[field_name] is not None:
                        extra_fields[field_name] = elem[field_name]
                _add_entry(po_v, inv_v, extra=extra_fields)
            elif elem is not None and str(elem).strip():
                _add_entry(po_val=str(elem).strip())

    po_list = _coerce_str_list(
        _get_case_insensitive(params, "poNumbers", "po_numbers", "PONumbers")
    )
    inv_list = _coerce_str_list(
        _get_case_insensitive(params, "invoiceNumbers", "invoice_numbers", "InvoiceNumbers")
    )

    if po_list and inv_list and len(po_list) == len(inv_list) and not normalized_items:
        for po_v, inv_v in zip(po_list, inv_list):
            _add_entry(po_val=po_v, inv_val=inv_v)
    else:
        for po_v in po_list:
            _add_entry(po_val=po_v)
        for inv_v in inv_list:
            _add_entry(inv_val=inv_v)

    return normalized_items


def unpack_skill_parameters(raw_parameters: Any) -> dict[str, Any]:
    """
    Unpacks nested `SiteLogin`, `Emailer`, and `RunParametersJson` / `runParameters`
    dictionaries from `job["parameters"]`, while normalizing both `camelCase` and
    `snake_case` keys (`postedDate` / `posted_date`, `poNumber` / `po_number`,
    `items`, `poNumbers` / `po_numbers`, `invoiceNumbers` / `invoice_numbers`,
    `extractType` / `extract_type`, `dryRun` / `dry_run`, `invoiceNumber` / `invoice_number`,
    `userNote` / `user_note`).
    Safely falls back to `{}` for `None`, `""`, or `{}`.
    """
    params = _safe_json_dict(raw_parameters)
    site_params = _safe_json_dict(_get_case_insensitive(params, "SiteLogin", "site_login"))
    email_params = _safe_json_dict(_get_case_insensitive(params, "Emailer", "emailer"))
    run_params = _safe_json_dict(
        _get_case_insensitive(
            params,
            "runParameters",
            "run_parameters",
            "runParametersJson",
            "RunParametersJson",
            "run_parameters_json",
        )
    )

    merged = dict(params)
    for layer in (email_params, site_params, run_params):
        for k, v in layer.items():
            if v is not None and v != "":
                merged[k] = v

    # Normalize extractType (Revenue | Zero | Credit)
    ext_val = _get_case_insensitive(run_params, "extractType", "extract_type", "ExtractType") or _get_case_insensitive(
        merged, "extractType", "extract_type", "ExtractType"
    )
    if ext_val is not None and str(ext_val).strip():
        merged["extractType"] = str(ext_val).strip()

    # Normalize camelCase and snake_case runtime keys
    po_val = _get_case_insensitive(merged, "poNumber", "po_number", "po", "PONumber")
    if po_val is not None and str(po_val).strip():
        merged["po"] = str(po_val).strip()
        merged["poNumber"] = str(po_val).strip()

    posted_val = _get_case_insensitive(merged, "postedDate", "posted_date", "PostedDate")
    if posted_val is not None and str(posted_val).strip():
        merged["postedDate"] = str(posted_val).strip()
    else:
        merged.pop("postedDate", None)
        merged.pop("posted_date", None)

    inv_val = _get_case_insensitive(merged, "invoiceNumber", "invoice_number", "invoice", "InvoiceNumber")
    if inv_val is not None and str(inv_val).strip():
        merged["invoiceNumber"] = str(inv_val).strip()
    else:
        merged.pop("invoiceNumber", None)
        merged.pop("invoice_number", None)

    note_val = _get_case_insensitive(merged, "userNote", "user_note", "UserNote")
    if note_val is not None and str(note_val).strip():
        merged["userNote"] = str(note_val).strip()
    else:
        merged.pop("userNote", None)
        merged.pop("user_note", None)

    dry_val = _coerce_optional_bool(_get_case_insensitive(merged, "dryRun", "dry_run", "DryRun"))
    if dry_val is not None:
        merged["dryRun"] = dry_val

    po_nums = _coerce_str_list(_get_case_insensitive(merged, "poNumbers", "po_numbers", "PONumbers"))
    if po_nums:
        merged["poNumbers"] = po_nums

    inv_nums = _coerce_str_list(_get_case_insensitive(merged, "invoiceNumbers", "invoice_numbers", "InvoiceNumbers"))
    if inv_nums:
        merged["invoiceNumbers"] = inv_nums

    marked = extract_marked_items(merged)
    if marked:
        merged["items"] = marked

    model_val = _get_case_insensitive(merged, "modelName", "model_name", "model", "geminiModel")
    if model_val is not None and str(model_val).strip():
        merged["modelName"] = str(model_val).strip()

    return merged


class SupplyProParameters(BaseModel):
    """Validated job parameters for the SupplyPro PortalSync template."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    target_portal: str = Field(default="SupplyPro", alias="targetPortal")
    model_name: str = Field(default=DEFAULT_GEMINI_MODEL, alias="modelName")
    extract_type: str = Field(default="Revenue", alias="extractType")
    batch_limit: int = Field(default=50, alias="batchLimit", ge=1)
    posted_date: Optional[str] = Field(default=None, alias="postedDate")
    division: Optional[str] = Field(default=None)
    po: Optional[str] = Field(default=None, alias="poNumber")
    po_queue: list[str] = Field(default_factory=list, alias="poQueue")
    items: list[dict[str, Any]] = Field(default_factory=list)
    po_numbers: list[str] = Field(default_factory=list, alias="poNumbers")
    invoice_numbers: list[str] = Field(default_factory=list, alias="invoiceNumbers")
    builder_name: Optional[str] = Field(default=None, alias="builderName")
    property_address: Optional[str] = Field(default=None, alias="propertyAddress")
    inspection_type: Optional[str] = Field(default=None, alias="inspectionType")
    expected_fee: Optional[float] = Field(default=None, alias="expectedFee")
    invoice_number: Optional[str] = Field(default=None, alias="invoiceNumber")
    user_note: Optional[str] = Field(default=None, alias="userNote")
    dry_run: Optional[bool] = Field(default=None, alias="dryRun")
    recipient_email: Optional[str] = Field(default=None, alias="recipientEmail")
    attach_csv: bool = Field(default=True, alias="attachCsv")
    prompt_instructions: Optional[str] = Field(default=None, alias="promptInstructions")
    execution_id: Optional[int] = Field(default=None, alias="executionId")
    headless: bool = Field(default_factory=_default_headless)
    slow_mo: int = Field(default_factory=_default_slow_mo, alias="slowMo")
    login_url: str = Field(
        default_factory=lambda: (
            os.getenv("SUPPLYPRO_LOGIN_URL")
            or os.getenv("SUPPLYPRO_PORTAL_URL")
            or "https://www.hyphensolutions.com/MH2Supply/login.asp"
        ),
        alias="loginUrl",
    )


class SupplyProAuditResult(BaseModel):
    """Structured 4-factor reconciliation JSON verdict returned by Gemini 2.5 Flash."""

    builderMatch: bool
    addressMatch: bool
    inspectionTypeMatch: bool
    feeMatch: bool
    overallMatch: bool
    confidenceScore: float
    discrepancies: list[str] = Field(default_factory=list)
    auditNotes: str


class TokenTelemetry(BaseModel):
    """Accumulated Gemini token usage and estimated USD cost across a job execution."""

    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def add_usage(
        self,
        prompt_tokens: int,
        candidate_tokens: int,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        call_cost: Optional[float] = None,
    ) -> None:
        p_tok = int(prompt_tokens or 0)
        c_tok = int(candidate_tokens or 0)
        cache_tok = int(cached_tokens or 0)
        self.input_tokens += p_tok
        self.cached_tokens += cache_tok
        self.output_tokens += c_tok
        computed_total = int(total_tokens) if total_tokens else (p_tok + c_tok)
        self.total_tokens += computed_total
        if call_cost is not None:
            self.estimated_cost_usd = round(self.estimated_cost_usd + float(call_cost), 6)
        else:
            self.estimated_cost_usd = round(
                (self.input_tokens * GEMINI_25_FLASH_INPUT_COST_PER_1M / 1_000_000)
                + (self.output_tokens * GEMINI_25_FLASH_OUTPUT_COST_PER_1M / 1_000_000),
                6,
            )

    def sync_from_cost_tracker(self, tracker: CostTracker) -> None:
        """Synchronizes cumulative token and cost counts from a CostTracker instance."""
        self.input_tokens = int(tracker.total_input_tokens)
        self.cached_tokens = int(tracker.total_cached_tokens)
        self.output_tokens = int(tracker.total_output_tokens)
        self.total_tokens = int(tracker.total_input_tokens + tracker.total_output_tokens)
        self.estimated_cost_usd = round(float(tracker.total_cost), 6)


class SkillExecutionResult(BaseModel):
    """Standardized return payload from any Burgess Buddy skill execution."""

    status: str = "Completed"
    records_processed: int = 0
    records_exception: int = 0
    records_failed: int = 0
    output_summary: str = ""
    error_message: Optional[str] = None
    details: list[dict[str, Any]] = Field(default_factory=list)
    telemetry: TokenTelemetry = Field(default_factory=TokenTelemetry)


def _extract_reinspect_bool(row: dict[str, Any]) -> bool:
    """
    Safely extracts the `Reinspect` bit column (`bool(row.get("Reinspect"))`)
    from an `ai.vw_AccountingExtract` row or API DTO.
    """
    if not isinstance(row, dict):
        return False
    if "Reinspect" in row and row["Reinspect"] is not None:
        coerced = _coerce_optional_bool(row["Reinspect"])
        return bool(coerced) if coerced is not None else bool(row.get("Reinspect"))
    raw_val = _get_case_insensitive(
        row,
        "Reinspect",
        "reinspect",
        "isReinspect",
        "is_reinspect",
        "IsReinspect",
    )
    if raw_val is None:
        return False
    coerced = _coerce_optional_bool(raw_val)
    return bool(coerced) if coerced is not None else bool(raw_val)


def normalize_pending_order_record(raw_order: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizes a pending order record returned by `GET /api/accounting/supplypro/pending`
    or `ai.vw_AccountingExtract` into the standard `expected` dictionary structure.
    """
    po_num = str(
        _get_case_insensitive(
            raw_order,
            "poNumber",
            "PONumber",
            "po",
            "PurchaseOrderNumber",
            "builderOrderNumber",
            default="",
        )
    ).strip()
    insp_id_raw = _get_case_insensitive(raw_order, "inspectionId", "InspectionID", "id")
    insp_id = int(insp_id_raw) if insp_id_raw is not None and str(insp_id_raw).isdigit() else None

    inv_raw = _get_case_insensitive(raw_order, "invoiceNumber", "InvoiceNumber", "invoice")
    inv_str = str(inv_raw).strip() if inv_raw not in (None, "") else None

    fee_raw = _get_case_insensitive(raw_order, "fee", "Fee", "Amount", "expectedAmount", "amount")
    fee_dec = _to_cents(fee_raw)
    fee_val = float(fee_dec) if fee_dec is not None else None
    is_reinspect = _extract_reinspect_bool(raw_order)

    return {
        "poNumber": po_num,
        "inspectionId": insp_id,
        "invoiceNumber": inv_str,
        "builderName": str(
            _get_case_insensitive(
                raw_order,
                "builderName",
                "BuilderName",
                "ClientName",
                "clientName",
                "builder",
                default="",
            )
        ).strip(),
        "propertyAddress": str(
            _get_case_insensitive(
                raw_order,
                "propertyAddress",
                "PropertyAddress",
                "Address",
                "address",
                default="",
            )
        ).strip(),
        "inspectionType": str(
            _get_case_insensitive(
                raw_order,
                "inspectionType",
                "InspectionType",
                "TypeName",
                "typeName",
                "task",
                default="",
            )
        ).strip(),
        "fee": fee_val,
        "reinspect": is_reinspect,
        "is_reinspect": is_reinspect,
        "dbRecordFound": True,
    }


def fetch_expected_order_from_db(
    po_number: str,
    fallback_params: Optional[SupplyProParameters] = None,
) -> dict[str, Any]:
    """
    Queries `ai.vw_AccountingExtract` (with fallback to `ai.VW_AccountingExtractRevenue` /
    `ai.VW_AccountingExtract_Revenue`) for the target PO's expected internal record values,
    including the `Reinspect` bit column (`bool(row.get("Reinspect"))`).
    """
    defaults: dict[str, Any] = {
        "poNumber": po_number,
        "builderName": (fallback_params.builder_name if fallback_params else None) or "Unknown (No DB Record Found)",
        "propertyAddress": (fallback_params.property_address if fallback_params else None) or "Unknown (No DB Record Found)",
        "inspectionType": (fallback_params.inspection_type if fallback_params else None) or "Unknown (No DB Record Found)",
        "fee": fallback_params.expected_fee if (fallback_params and fallback_params.expected_fee is not None) else None,
        "invoiceNumber": fallback_params.invoice_number if fallback_params else None,
        "inspectionId": None,
        "reinspect": False,
        "is_reinspect": False,
        "dbRecordFound": False,
    }

    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_server = os.getenv("DB_SERVER")
    db_name = os.getenv("DB_NAME")

    if not (db_user and db_password and db_server and db_name):
        logger.warning("Database credentials not fully configured in .env; using fallback expected values.")
        return defaults

    connection_url = URL.create(
        "mssql+pymssql",
        username=db_user,
        password=db_password,
        host=db_server,
        database=db_name,
    )

    view_queries = [
        (
            "ai.vw_AccountingExtract",
            """
            SELECT TOP 1
                InspectionID,
                InvoiceNumber,
                BuilderName,
                PONumber,
                PropertyAddress,
                InspectionType,
                Fee,
                Reinspect
            FROM ai.vw_AccountingExtract
            WHERE PONumber = :exact_po OR PONumber LIKE :like_po
            ORDER BY
                CASE WHEN PONumber = :exact_po THEN 0 ELSE 1 END,
                InspectionDate DESC
            """,
        ),
        (
            "ai.vw_AccountingExtract",
            """
            SELECT TOP 1
                *,
                Reinspect
            FROM ai.vw_AccountingExtract
            WHERE PONumber = :exact_po OR PONumber LIKE :like_po
            """,
        ),
        (
            "ai.VW_AccountingExtractRevenue",
            """
            SELECT TOP 1
                InspectionID,
                InvoiceNumber,
                BuilderName,
                PONumber,
                PropertyAddress,
                InspectionType,
                Fee
            FROM ai.VW_AccountingExtractRevenue
            WHERE PONumber = :exact_po OR PONumber LIKE :like_po
            ORDER BY
                CASE WHEN PONumber = :exact_po THEN 0 ELSE 1 END,
                InspectionDate DESC
            """,
        ),
        (
            "ai.VW_AccountingExtract_Revenue",
            """
            SELECT TOP 1
                InspectionID,
                InvoiceNumber,
                BuilderName,
                PONumber,
                PropertyAddress,
                InspectionType,
                Fee
            FROM ai.VW_AccountingExtract_Revenue
            WHERE PONumber = :exact_po OR PONumber LIKE :like_po
            ORDER BY
                CASE WHEN PONumber = :exact_po THEN 0 ELSE 1 END,
                InspectionDate DESC
            """,
        ),
    ]

    try:
        engine = create_engine(connection_url)
        with engine.connect() as conn:
            for view_name, sql_text in view_queries:
                try:
                    row_mapping = conn.execute(
                        text(sql_text),
                        {"exact_po": po_number, "like_po": f"%{po_number}%"},
                    ).mappings().first()

                    if row_mapping:
                        row = dict(row_mapping)
                        is_reinspect = bool(row.get("Reinspect")) if "Reinspect" in row else _extract_reinspect_bool(row)
                        fee_raw = _get_case_insensitive(row, "Fee", "Amount", "fee", "amount")
                        fee_dec = _to_cents(fee_raw)
                        fee_val = float(fee_dec) if fee_dec is not None else defaults["fee"]
                        inv_raw = _get_case_insensitive(row, "InvoiceNumber", "invoiceNumber")
                        inv_val = str(inv_raw).strip() if inv_raw is not None else defaults["invoiceNumber"]
                        insp_id_raw = _get_case_insensitive(row, "InspectionID", "inspectionId")
                        record = {
                            "poNumber": str(_get_case_insensitive(row, "PONumber", "poNumber", default=po_number)).strip(),
                            "builderName": str(_get_case_insensitive(row, "BuilderName", "ClientName", default="")).strip(),
                            "propertyAddress": str(_get_case_insensitive(row, "PropertyAddress", "Address", default="")).strip(),
                            "inspectionType": str(_get_case_insensitive(row, "InspectionType", "TypeName", default="")).strip(),
                            "fee": fee_val,
                            "invoiceNumber": inv_val,
                            "inspectionId": int(insp_id_raw) if insp_id_raw is not None else None,
                            "reinspect": is_reinspect,
                            "is_reinspect": is_reinspect,
                            "dbRecordFound": True,
                        }
                        logger.info(
                            "Loaded expected record from %s for PO %s: builder=%r, address=%r, type=%r, fee=%s, reinspect=%s, invoice=%s",
                            view_name,
                            po_number,
                            record["builderName"],
                            record["propertyAddress"],
                            record["inspectionType"],
                            record["fee"],
                            record["reinspect"],
                            record["invoiceNumber"],
                        )
                        return record
                except Exception as view_err:
                    logger.debug("Query on %s skipped: %s", view_name, view_err)
                    continue
    except Exception as db_err:
        logger.warning("Database lookup failed for PO %s (%s); falling back to defaults.", po_number, db_err)

    logger.warning("No database record found for PO %s; using fallback expected values.", po_number)
    return defaults


# Default seed mappings from BORE.ai.SupplyProTaskMappings (used as fallback when DB is unreachable)
DEFAULT_SUPPLYPRO_TASK_MAPPINGS: list[dict[str, Any]] = [
    {
        "builderName": "Davidson Homes",
        "internalInspectionType": "Final",
        "supplyProTaskName": "Q.I. Reinspect",
        "isReinspect": True,
        "isActive": True,
    },
    {
        "builderName": "Davidson Homes",
        "internalInspectionType": "Final - Davidson Homes-2",
        "supplyProTaskName": "Q.I. Reinspect",
        "isReinspect": True,
        "isActive": True,
    },
    {
        "builderName": None,
        "internalInspectionType": "Final - Davidson Homes-2",
        "supplyProTaskName": "Q.I. Reinspect",
        "isReinspect": True,
        "isActive": True,
    },
]

_REINSPECT_TERM_RE = re.compile(r"\bre[\-\s]*inspect(?:ion)?\b", re.IGNORECASE)
_FIRST_TIME_TAG_RE = re.compile(
    r"(?:\(\s*1\s*\)|\bfirst[\-\s]+time\b|(?:^|[\sA-Za-z0-9_])\-1(?:\b|$))",
    re.IGNORECASE,
)


def is_reinspection_task_name(task_name: Optional[str]) -> bool:
    """
    Returns True if `task_name` contains reinspection terminology
    (`reinspect`, `reinspection`, `re-inspect`, `re-inspection`, case-insensitive).
    """
    if not task_name:
        return False
    return bool(_REINSPECT_TERM_RE.search(str(task_name)))


def has_forbidden_first_time_tag(task_name: Optional[str]) -> bool:
    """
    Returns True if `task_name` is tagged with `-1`, `(1)`, or `First-Time` / `First Time`
    (case-insensitive).
    """
    if not task_name:
        return False
    return bool(_FIRST_TIME_TAG_RE.search(str(task_name)))


def _normalize_mapping_entry(raw_row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalizes a row from `BORE.ai.SupplyProTaskMappings` into a standard dict."""
    if not isinstance(raw_row, dict):
        return None
    is_active_raw = _get_case_insensitive(raw_row, "IsActive", "isActive", "is_active", default=True)
    coerced_active = _coerce_optional_bool(is_active_raw)
    if coerced_active is False:
        return None

    internal_type = str(
        _get_case_insensitive(
            raw_row,
            "InternalInspectionType",
            "internalInspectionType",
            "internal_inspection_type",
            "TypeName",
            default="",
        )
    ).strip()
    supplypro_task = str(
        _get_case_insensitive(
            raw_row,
            "SupplyProTaskName",
            "supplyProTaskName",
            "supplypro_task_name",
            "ExternalTaskName",
            "TaskName",
            default="",
        )
    ).strip()
    if not internal_type or not supplypro_task:
        return None

    builder_raw = _get_case_insensitive(raw_row, "BuilderName", "builderName", "builder_name")
    builder_str = str(builder_raw).strip() if builder_raw is not None and str(builder_raw).strip() else None

    reinspect_raw = _get_case_insensitive(raw_row, "IsReinspect", "isReinspect", "is_reinspect", "Reinspect")
    is_reinspect = _coerce_optional_bool(reinspect_raw) if reinspect_raw is not None else None

    return {
        "builderName": builder_str,
        "internalInspectionType": internal_type,
        "supplyProTaskName": supplypro_task,
        "isReinspect": is_reinspect,
        "isActive": True,
    }


def load_supplypro_task_mappings(
    extra_mappings: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """
    Loads active task mapping rules from `BORE.ai.SupplyProTaskMappings` at the start of a job run,
    merging any explicit `extra_mappings` and default baseline rules.
    """
    loaded: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def _add(entry: Optional[dict[str, Any]]) -> None:
        if not entry:
            return
        key = (
            (entry.get("builderName") or "").lower(),
            entry["internalInspectionType"].lower(),
            entry["supplyProTaskName"].lower(),
        )
        if key not in seen_keys:
            seen_keys.add(key)
            loaded.append(entry)

    if extra_mappings:
        for item in extra_mappings:
            _add(_normalize_mapping_entry(item))

    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_server = os.getenv("DB_SERVER")
    db_name = os.getenv("DB_NAME")

    if db_user and db_password and db_server and db_name:
        connection_url = URL.create(
            "mssql+pymssql",
            username=db_user,
            password=db_password,
            host=db_server,
            database=db_name,
        )
        table_candidates = [
            "BORE.ai.SupplyProTaskMappings",
            "ai.SupplyProTaskMappings",
        ]
        try:
            engine = create_engine(connection_url)
            with engine.connect() as conn:
                for table_name in table_candidates:
                    try:
                        rows = (
                            conn.execute(
                                text(f"SELECT * FROM {table_name} WHERE IsActive = 1")
                            )
                            .mappings()
                            .all()
                        )
                        for r in rows:
                            _add(_normalize_mapping_entry(dict(r)))
                        logger.info(
                            "Loaded %d active task mapping(s) from %s.",
                            len(rows),
                            table_name,
                        )
                        break
                    except Exception as table_err:
                        logger.debug("Query on %s skipped: %s", table_name, table_err)
                        continue
        except Exception as db_err:
            logger.warning(
                "Could not query BORE.ai.SupplyProTaskMappings (%s); using built-in mappings.",
                db_err,
            )

    for default_item in DEFAULT_SUPPLYPRO_TASK_MAPPINGS:
        _add(_normalize_mapping_entry(default_item))

    return loaded


def _builder_matches_mapping(
    order_builder: Optional[str],
    mapping_builder: Optional[str],
) -> bool:
    """
    Returns True if `mapping_builder` matches `order_builder` (case-insensitive exact
    or corporate/division prefix match, e.g., 'Davidson Homes' vs 'Davidson Homes - Texas').
    """
    if not mapping_builder or not order_builder:
        return False
    ob = order_builder.strip().lower()
    mb = mapping_builder.strip().lower()
    return ob == mb or mb in ob or ob in mb


def _strip_po_brackets(task_str: str) -> str:
    """Strips bracketed PO metadata like `[00081-12488-1050][PO]` while preserving `(1)` or `-1`."""
    return re.sub(r"\s*\[[^\]]*\]", "", (task_str or "")).strip()


def match_sql_task_mapping(
    internal_inspection_type: str,
    supplypro_task_name: str,
    builder_name: Optional[str] = None,
    task_mappings: Optional[list[dict[str, Any]]] = None,
    is_reinspect: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """
    Checks `BORE.ai.SupplyProTaskMappings` for an active mapping matching
    `internal_inspection_type` -> `supplypro_task_name`.
    Evaluates builder-specific mappings (`BuilderName` + `InternalInspectionType`) first,
    then global (`BuilderName IS NULL`) rules.
    """
    mappings = task_mappings if task_mappings is not None else DEFAULT_SUPPLYPRO_TASK_MAPPINGS
    int_clean = (internal_inspection_type or "").strip().lower()
    int_base = _strip_po_brackets(internal_inspection_type or "").lower()
    sp_clean = (supplypro_task_name or "").strip().lower()
    sp_base = _strip_po_brackets(supplypro_task_name or "").lower()

    if not int_clean or not sp_clean:
        return None

    builder_specific = [
        m for m in mappings if m.get("builderName") and _builder_matches_mapping(builder_name, m.get("builderName"))
    ]
    global_rules = [m for m in mappings if not m.get("builderName")]

    for tier in (builder_specific, global_rules):
        for rule in tier:
            rule_reinspect = rule.get("isReinspect")
            if is_reinspect is not None and rule_reinspect is not None and bool(rule_reinspect) != bool(is_reinspect):
                continue

            map_int = (rule.get("internalInspectionType") or "").strip().lower()
            map_sp = (rule.get("supplyProTaskName") or "").strip().lower()
            if not map_int or not map_sp:
                continue

            sp_matched = sp_clean == map_sp or sp_base == map_sp
            if not sp_matched:
                continue

            # Direct match on InternalInspectionType
            if int_clean == map_int or int_base == map_int:
                return rule

            # Composite Rule 1.3 format: e.g., "Final - Davidson Homes-2" when internal_type="Final",
            # builder="Davidson Homes", and is_reinspect=True
            if builder_name and is_reinspect:
                composite_candidate = f"{int_base} - {builder_name.strip().lower()}-2"
                if map_int == composite_candidate or (
                    map_int.startswith(f"{int_base} - ")
                    and builder_name.strip().lower() in map_int
                ):
                    return rule

    return None


class SupplyProPortalDriver:
    """
    Playwright browser driver encapsulating the SupplyPro (Hyphen Solutions MH2Supply)
    portal lifecycle: browser setup, login (including force_signon session takeover),
    PO search, order detail scraping, order completion, and clean sign-out.
    """

    def __init__(self, params: SupplyProParameters, dry_run: bool = False):
        self.params = params
        self.dry_run = dry_run
        self.login_url = (
            os.getenv("SUPPLYPRO_LOGIN_URL")
            or os.getenv("SUPPLYPRO_PORTAL_URL")
            or params.login_url
        )
        self.username = os.getenv("SUPPLYPRO_USERNAME", "").strip()
        self.password = os.getenv("SUPPLYPRO_PASSWORD", "").strip()

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _active_target(self):
        """Returns the best active page or content frame for DOM interaction."""
        if not self._page:
            raise RuntimeError("Playwright page is not initialized.")
        for frame in self._page.frames:
            frame_name = (frame.name or "").lower()
            if frame_name in ("main", "mainframe", "content", "body", "workspace"):
                return frame
        return self._page

    def _find_visible_locator(self, target, selectors: list[str], timeout: int = 8000):
        """
        Waits up to `timeout` ms for any of the candidate selectors to become visible,
        returning the first visible Locator or None if none appear within the timeout.
        """
        combined_selector = ", ".join(selectors)
        try:
            combined_loc = target.locator(combined_selector).first
            combined_loc.wait_for(state="visible", timeout=timeout)
        except Exception:
            pass

        for selector in selectors:
            try:
                loc = target.locator(selector).first
                if loc.is_visible():
                    return loc
            except Exception:
                continue

        if self._page and target is self._page:
            for frame in self._page.frames:
                if frame is self._page.main_frame:
                    continue
                for selector in selectors:
                    try:
                        loc = frame.locator(selector).first
                        if loc.is_visible():
                            return loc
                    except Exception:
                        continue
        return None

    def _fill_first_matching(self, target, selectors: list[str], value: str, timeout: int = 8000) -> bool:
        """Waits for and fills the first visible input matching one of the candidate selectors."""
        loc = self._find_visible_locator(target, selectors, timeout=timeout)
        if loc is not None:
            loc.fill(value, timeout=timeout)
            return True
        return False

    def _click_first_matching(self, target, selectors: list[str], timeout: int = 8000) -> bool:
        """Waits for and clicks the first visible element matching one of the candidate selectors."""
        loc = self._find_visible_locator(target, selectors, timeout=timeout)
        if loc is not None:
            loc.click(timeout=timeout)
            return True
        return False

    def setup_browser(self) -> None:
        """Launches Playwright Chromium with local visual debugging support (headless=False, slow_mo=300)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed in the Python environment. "
                "Install it with: .venv/bin/pip install playwright && .venv/bin/playwright install chromium"
            ) from exc

        if not self.username or not self.password:
            raise ValueError(
                "SUPPLYPRO_USERNAME and SUPPLYPRO_PASSWORD must be set in .env to run SupplyPro automation."
            )

        logger.info(
            "Launching Playwright Chromium (headless=%s, slow_mo=%sms)...",
            self.params.headless,
            self.params.slow_mo,
        )
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.params.headless,
            slow_mo=self.params.slow_mo,
        )
        self._context = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        self._page = self._context.new_page()

    def login(self) -> None:
        """
        Navigates to SUPPLYPRO_LOGIN_URL, submits credentials, handles SupplyPro's
        `force_signon` active-session prompt if shown, and awaits dashboard navigation.
        """
        if not self._page:
            raise RuntimeError("Browser must be initialized via setup_browser() before login().")

        logger.info("Navigating to SupplyPro login URL: %s", self.login_url)
        self._page.goto(self.login_url, wait_until="domcontentloaded", timeout=30000)

        username_selectors = [
            "input#user_name",
            "input[name='user_name' i]",
            "input[name='userid' i]",
            "input[name='username' i]",
            "input[id*='user' i]",
            "input[type='text']",
        ]
        password_selectors = [
            "input#password",
            "input[name='password' i]",
            "input[type='password']",
        ]
        submit_selectors = [
            "input[name='cmdSubmit']",
            "button[type='submit']",
            "input[type='submit']",
            "input[value*='Sign In' i]",
            "button:has-text('Sign In')",
        ]

        target = self._active_target()
        if not self._fill_first_matching(target, username_selectors, self.username, timeout=15000):
            raise RuntimeError(f"Could not locate username input field on {self._page.url}")

        if not self._fill_first_matching(target, password_selectors, self.password, timeout=10000):
            raise RuntimeError(f"Could not locate password input field on {self._page.url}")

        logger.info("Submitting SupplyPro login credentials for user %r...", self.username)
        with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            if not self._click_first_matching(target, submit_selectors, timeout=10000):
                target.locator(password_selectors[0]).first.press("Enter")

        target = self._active_target()
        force_loc = self._find_visible_locator(target, ["input[name='force_signon']"], timeout=1500)
        if force_loc is not None:
            logger.info(
                "SupplyPro active-session prompt (force_signon) detected; "
                "checking force_signon and re-submitting credentials..."
            )
            force_loc.check(timeout=5000)
            self._fill_first_matching(target, password_selectors, self.password, timeout=5000)
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                if not self._click_first_matching(target, submit_selectors, timeout=10000):
                    target.locator(password_selectors[0]).first.press("Enter")

        try:
            self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        if "login.asp" in self._page.url.lower():
            raise RuntimeError(
                f"SupplyPro login did not advance past login screen (current URL: {self._page.url}). "
                "Verify SUPPLYPRO_USERNAME and SUPPLYPRO_PASSWORD."
            )

        logger.info("Successfully logged into SupplyPro (current URL: %s).", self._page.url)

    def is_session_expired(self) -> bool:
        """
        Checks whether SupplyPro has redirected the browser back to the login screen
        (e.g., due to session timeout during a large 700+ order batch).
        """
        if not self._page:
            return False
        current_url = (self._page.url or "").lower()
        if "login.asp" in current_url or "session_expired" in current_url or "timeout" in current_url:
            return True
        try:
            target = self._active_target()
            for sel in ("input#user_name", "input[name='user_name' i]", "input[name='force_signon']"):
                loc = target.locator(sel).first
                if loc.is_visible():
                    return True
        except Exception:
            pass
        return False

    def ensure_authenticated(self) -> bool:
        """
        Verifies the SupplyPro session is still active; if redirected to login.asp,
        automatically re-authenticates and returns True if a re-login was performed.
        """
        if self.is_session_expired():
            logger.warning("SupplyPro session timeout/redirect detected; re-authenticating...")
            self.login()
            progress_logger.info("Re-authenticated into SupplyPro after session timeout")
            return True
        return False

    def _select_best_order_detail_link(
        self,
        target,
        po_number: str,
        is_reinspect: bool = False,
        internal_inspection_type: Optional[str] = None,
    ):
        """
        Selects the best OrderDetail.asp link on Order Search results (`orderlist.asp`).
        When multiple PO rows are returned:
          - If `is_reinspect` is False: filters out rows with `-1`, `(1)`, `First-Time`,
            or `Reinspect` tags in favor of the standard `TypeName` (`internal_inspection_type`) row.
          - If `is_reinspect` is True: prefers rows containing reinspection terminology.
        """
        detail_link_selectors = [
            f"a:has-text('{po_number}')",
            "a[href*='OrderDetail.asp']",
            "a[href*='orderdetail.asp']",
            "a:has-text('Details')",
        ]

        try:
            row_locator = target.locator(
                "tr:has(a[href*='OrderDetail.asp' i]), tr:has(a[href*='orderdetail.asp' i])"
            )
            row_count = row_locator.count()
            if row_count > 1:
                candidates: list[tuple[str, Any]] = []
                for i in range(row_count):
                    row_el = row_locator.nth(i)
                    try:
                        if not row_el.is_visible():
                            continue
                        row_text = (row_el.inner_text(timeout=1500) or "").strip()
                        link_el = row_el.locator(
                            f"a:has-text('{po_number}'), a[href*='OrderDetail.asp' i], a[href*='orderdetail.asp' i]"
                        ).first
                        if link_el.is_visible():
                            candidates.append((row_text, link_el))
                    except Exception:
                        continue

                if len(candidates) > 1:
                    int_type_lower = _strip_po_brackets(internal_inspection_type or "").lower()
                    if not is_reinspect:
                        filtered = [
                            (txt, lnk)
                            for txt, lnk in candidates
                            if not is_reinspection_task_name(txt) and not has_forbidden_first_time_tag(txt)
                        ]
                        pool = filtered if filtered else candidates
                        if int_type_lower:
                            for txt, lnk in pool:
                                if int_type_lower in txt.lower():
                                    return lnk
                        return pool[0][1]
                    else:
                        reinspect_rows = [
                            (txt, lnk)
                            for txt, lnk in candidates
                            if is_reinspection_task_name(txt)
                        ]
                        if reinspect_rows:
                            return reinspect_rows[0][1]
        except Exception as row_filter_err:
            logger.debug("Multi-row SupplyPro filter fallback on PO %s: %s", po_number, row_filter_err)

        return self._find_visible_locator(target, detail_link_selectors, timeout=12000)

    def _is_complete_order_action_available(self, target) -> bool:
        """
        Checks whether the 'Complete Order' action dropdown (or checkbox) is present and enabled
        on SupplyPro's Order Details page. Returns False when CSR-blocked or unavailable.
        """
        action_select = self._find_complete_order_select(target, timeout=1500)
        if action_select is not None:
            try:
                if hasattr(action_select, "is_disabled") and action_select.is_disabled():
                    return False
                option_texts = action_select.locator("option").all_inner_texts()
                if option_texts and not any("complete order" in t.strip().lower() for t in option_texts):
                    return False
                complete_opt = action_select.locator("option", has_text="Complete Order").first
                if hasattr(complete_opt, "is_disabled") and complete_opt.is_disabled():
                    return False
                return True
            except Exception:
                return True

        complete_checkbox_selectors = [
            "input[type='checkbox'][name*='complete' i]",
            "input[type='checkbox'][id*='complete' i]",
            "input[type='checkbox'][value*='complete' i]",
        ]
        checkbox = self._find_visible_locator(target, complete_checkbox_selectors, timeout=500)
        if checkbox is not None:
            try:
                if hasattr(checkbox, "is_disabled") and checkbox.is_disabled():
                    return False
            except Exception:
                pass
            return True
        return False

    def search_and_scrape_order(
        self,
        po_number: str,
        is_reinspect: bool = False,
        internal_inspection_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Navigates to Order Search (`Orders/orderlist.asp`), enters the PO number into
        `input#bldr_order_filter`, clicks `input#cmdSearch`, opens `OrderDetail.asp`,
        and scrapes builder, address, taskDescription, amount, and completion availability.
        """
        if not self._page:
            raise RuntimeError("Browser is not initialized.")

        self.ensure_authenticated()

        logger.info("Navigating to Order Search for PO %s...", po_number)
        target = self._active_target()

        order_search_selectors = [
            "a:has-text('Order Search')",
            "a[href*='orderlist.asp' i]",
            "a:has-text('Orders')",
            "a[href*='Orders/Default.asp' i]",
        ]
        nav_loc = self._find_visible_locator(target, order_search_selectors, timeout=10000)
        if nav_loc is not None:
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                nav_loc.click(timeout=10000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

        if self.is_session_expired():
            raise SupplyProSessionExpiredError(
                f"SupplyPro session redirected to login screen ({self._page.url}) before searching PO {po_number}."
            )

        target = self._active_target()

        if "orderlist.asp" not in self._page.url.lower():
            sub_nav = self._find_visible_locator(
                target,
                ["a:has-text('Order Search')", "a[href*='orderlist.asp' i]"],
                timeout=5000,
            )
            if sub_nav is not None:
                with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                    sub_nav.click(timeout=10000)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                target = self._active_target()

        if self.is_session_expired():
            raise SupplyProSessionExpiredError(
                f"SupplyPro session redirected to login screen ({self._page.url}) while opening Order Search for PO {po_number}."
            )

        po_input_selectors = [
            "input#bldr_order_filter",
            "input[name='bldr_order_filter']",
            "input#quick_search",
            "input[name='quick_search']",
            "input.filter_search",
        ]
        if not self._fill_first_matching(target, po_input_selectors, po_number, timeout=15000):
            if self.is_session_expired():
                raise SupplyProSessionExpiredError(
                    f"SupplyPro session expired while locating search field for PO {po_number} on {self._page.url}."
                )
            raise RuntimeError(
                f"Could not locate PO / Builder Order # search field for PO {po_number} on {self._page.url}."
            )

        search_btn_selectors = [
            "input#cmdSearch",
            "input[name='cmdSearch']",
            "input[type='button'][value='Search']",
            "input[type='submit'][value*='Search']",
            "button:has-text('Search')",
        ]
        logger.info("Executing PO search for %s...", po_number)
        search_btn = self._find_visible_locator(target, search_btn_selectors, timeout=10000)
        if search_btn is not None:
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                search_btn.click(timeout=10000)
        else:
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                target.locator(po_input_selectors[0]).first.press("Enter")

        try:
            self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        if self.is_session_expired():
            raise SupplyProSessionExpiredError(
                f"SupplyPro session redirected to login screen ({self._page.url}) after searching PO {po_number}."
            )

        target = self._active_target()

        detail_link = self._select_best_order_detail_link(
            target,
            po_number=po_number,
            is_reinspect=is_reinspect,
            internal_inspection_type=internal_inspection_type,
        )
        if detail_link is not None:
            logger.info("Opening Order Details page for PO %s...", po_number)
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                detail_link.click(timeout=10000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            if self.is_session_expired():
                raise SupplyProSessionExpiredError(
                    f"SupplyPro session redirected to login screen ({self._page.url}) when opening OrderDetail for PO {po_number}."
                )
            target = self._active_target()
        elif "orderdetail.asp" not in self._page.url.lower():
            if self.is_session_expired():
                raise SupplyProSessionExpiredError(
                    f"SupplyPro session redirected to login screen ({self._page.url}) while searching PO {po_number}."
                )
            raise SupplyProOrderNotFoundError(
                f"PO {po_number} was not found in SupplyPro Order Search results on {self._page.url}."
            )

        target.locator("body").wait_for(state="visible", timeout=10000)
        raw_text = target.locator("body").inner_text(timeout=10000)
        extracted_fields = self._extract_labeled_fields(raw_text, po_number)

        amount_str = extracted_fields.get("amount", "")
        numeric_amount = self._parse_currency(amount_str)

        action_available = self._is_complete_order_action_available(target)
        already_complete = self._is_order_already_complete(target, action_select_present=action_available)

        scraped_payload = {
            "poNumber": po_number,
            "pageUrl": self._page.url,
            "builder": extracted_fields.get("builder", ""),
            "address": extracted_fields.get("address", ""),
            "taskDescription": extracted_fields.get("description", ""),
            "amount": numeric_amount if numeric_amount is not None else amount_str,
            "amountText": amount_str,
            "orderStatus": extracted_fields.get("status", ""),
            "isAlreadyComplete": already_complete,
            "isActionAvailable": True if already_complete else action_available,
            "isCsrBlocked": (not already_complete) and (not action_available),
            "rawPageText": raw_text[:12000],
        }
        return scraped_payload

    @staticmethod
    def _parse_currency(text_val: str) -> Optional[float]:
        """Extracts a 2-decimal float from a currency string like '$150.00'."""
        dec = _to_cents(text_val)
        return float(dec) if dec is not None else None

    @staticmethod
    def _extract_labeled_fields(raw_text: str, po_number: str) -> dict[str, str]:
        """Extracts Builder, Address, Task Description, Total Amount, and Order Status from SupplyPro OrderDetail.asp text."""
        def _match_line(patterns: list[str]) -> str:
            for pat in patterns:
                match = re.search(pat, raw_text, flags=re.IGNORECASE | re.MULTILINE)
                if match:
                    return match.group(1).strip()
            return ""

        builder = _match_line([
            r"^\s*([^\n\r\t]+?)\s*\r?\n\s*Order Information",
            r"(?:Builder|Customer|Company|Client)\s*(?:Name)?\s*[:\-]\s*([^\n\r]+)",
        ])

        address = _match_line([
            r"Job Address[^\n\r]*\r?\n([^\n\r]+(?:\r?\n[^\n\r\t]+)?)",
            r"Job:\s*(?:\d+\s*-\s*)?([^\n\r\t]+)",
            r"(?:Address|Job\s*Address|Site\s*Address)\s*[:\-]\s*([^\n\r]+)",
        ])
        address = re.sub(r"\s+", " ", address).strip()

        description = _match_line([
            r"Task:\s*([^\n\r\t]+)",
            r"(?:Task|Description|Inspection\s*Type|Item\s*Description)\s*[:\-]\s*([^\n\r]+)",
        ])

        amount = _match_line([
            r"Total:\s*(\$\s*\d[\d,]*\.?\d*)",
            r"Subtotal:\s*(\$\s*\d[\d,]*\.?\d*)",
            r"(?:Total(?:\s*Order)?\s*Amount|Order\s*Total|Unit\s*Price|Amount)\s*[:\-]?\s*(\$?\s*\d[\d,]*\.?\d*)",
            r"(\$\s*\d[\d,]*\.\d{2})",
        ])

        status = _match_line([
            r"(?:Order\s*Status|Status)\s*[:\-]?\s*(Complete(?:d)?|Open|Pending|Accepted|Received|Approved|Cancelled|In\s*Progress)\b",
            r"(?:Order\s*Status|Status)\s*[:\-]\s*([^\n\r\t]+)",
        ])

        return {
            "poNumber": po_number,
            "builder": builder,
            "address": address,
            "description": description,
            "amount": amount,
            "status": status,
        }

    def _find_complete_order_select(self, target, timeout: int = 5000):
        """
        Locates the `<select>` dropdown whose initial value is '-- Select Action --'
        and that contains an `<option>` for 'Complete Order'.
        """
        contexts = [target]
        if self._page and target is self._page:
            for frame in self._page.frames:
                if frame is not self._page.main_frame:
                    contexts.append(frame)

        # 1. Filter <select> elements containing an <option> with text 'Complete Order' or '-- Select Action --'
        for ctx in contexts:
            try:
                filtered = ctx.locator("select").filter(
                    has=ctx.locator("option", has_text="Complete Order")
                ).first
                if filtered.is_visible():
                    return filtered
            except Exception:
                pass
            try:
                filtered_default = ctx.locator("select").filter(
                    has=ctx.locator("option", has_text="Select Action")
                ).first
                if filtered_default.is_visible():
                    return filtered_default
            except Exception:
                pass

        # 2. Fallback to explicit CSS/Playwright selectors with wait timeout
        select_selectors = [
            "select[name='execute_actions']",
            "select:has(option:has-text('Complete Order'))",
            "select:has(option:has-text('-- Select Action --'))",
            "select[name*='action' i]",
            "select[id*='action' i]",
        ]
        loc = self._find_visible_locator(target, select_selectors, timeout=timeout)
        if loc is not None:
            return loc

        # 3. Re-check filtered locator after wait
        for ctx in contexts:
            try:
                filtered = ctx.locator("select").filter(
                    has=ctx.locator("option", has_text="Complete Order")
                ).first
                if filtered.is_visible():
                    return filtered
            except Exception:
                continue
        return None

    def _is_order_already_complete(self, target, action_select_present: bool = False) -> bool:
        """
        Determines whether the SupplyPro Order Details page indicates the order is
        already Complete (and the action `<select>` dropdown has disappeared).
        """
        if action_select_present:
            return False

        raw_text = ""
        try:
            raw_text = target.locator("body").inner_text(timeout=3000) or ""
        except Exception:
            if self._page:
                try:
                    raw_text = self._page.locator("body").inner_text(timeout=3000) or ""
                except Exception:
                    raw_text = ""

        status_patterns = [
            r"(?:Order\s*Status|Status)\s*[:\-]?\s*Complete(?:d)?\b",
            r"\bStatus\b[\s\t:\-]*Complete(?:d)?\b",
            r"\bOrder\s+Complete(?:d)?\b",
            r"\bCompleted\s+Date\b",
            r"\bDate\s+Completed\b",
        ]
        for pat in status_patterns:
            if re.search(pat, raw_text, flags=re.IGNORECASE):
                return True

        # On OrderDetail.asp, if the action <select> dropdown has disappeared and
        # 'Complete' / 'Completed' appears in the order detail text, treat as Already Complete
        current_url = (self._page.url if self._page else "") or ""
        if "orderdetail.asp" in current_url.lower() and re.search(r"\bComplete(?:d)?\b", raw_text, flags=re.IGNORECASE):
            return True

        return False

    @staticmethod
    def _select_complete_order_option(action_select) -> None:
        """Selects the 'Complete Order' option in the action `<select>` matching its exact option label."""
        matched_label = "Complete Order"
        try:
            option_texts = action_select.locator("option").all_inner_texts()
            for opt_text in option_texts:
                if "complete order" in opt_text.strip().lower():
                    matched_label = opt_text.strip()
                    break
        except Exception:
            pass
        action_select.select_option(label=matched_label)

    def complete_order_on_portal(self, invoice_number: str) -> str:
        """
        When dry_run=False and overallMatch=True on SupplyPro OrderDetail.asp:
          1. Checks whether the order is already marked Complete (action dropdown absent
             and order status is Complete/Completed); if so, logs and returns 'Already Complete'.
          2. Enters the Burgess invoice number into 'Supplier's Order Number' (`input[name='order_num']`)
             and clicks 'Update Supplier Info' (`input[name='cmdSubmit2']`) if present.
          3. Locates the `<select>` dropdown (`-- Select Action --` / `Complete Order`),
             selects 'Complete Order' via `select_option(label=...)`, and clicks 'Update'.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] Safety guardrail active: skipping 'Complete Order' and 'Update'.")
            return "DryRun"

        target = self._active_target()

        # Check upfront if the action dropdown is absent and the order is already Complete
        initial_select = self._find_complete_order_select(target, timeout=2000)
        if initial_select is None and self._is_order_already_complete(target, action_select_present=False):
            logger.info("Order is already Complete in SupplyPro — skipping")
            return "Already Complete"

        supplier_order_selectors = [
            "input[name='order_num']",
            "input[name*='supplier' i][name*='order' i]",
            "input[id*='supplier' i][id*='order' i]",
            "input[name*='supplierorder' i]",
            "input[name*='invnum' i]",
            "input[name*='invoice' i]",
        ]
        if invoice_number:
            logger.info("Populating Supplier's Order Number with %r...", invoice_number)
            if self._fill_first_matching(target, supplier_order_selectors, str(invoice_number), timeout=4000):
                update_sup_btn = self._find_visible_locator(
                    target,
                    ["input[name='cmdSubmit2']", "input[value*='Update Supplier Info']"],
                    timeout=3000,
                )
                if update_sup_btn is not None:
                    with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                        update_sup_btn.click(timeout=5000)
                    target = self._active_target()

        action_selected = False
        action_select = self._find_complete_order_select(target, timeout=5000)
        if action_select is not None:
            self._select_complete_order_option(action_select)
            action_selected = True
        else:
            complete_checkbox_selectors = [
                "input[type='checkbox'][name*='complete' i]",
                "input[type='checkbox'][id*='complete' i]",
                "input[type='checkbox'][value*='complete' i]",
            ]
            checkbox = self._find_visible_locator(target, complete_checkbox_selectors, timeout=2000)
            if checkbox is not None:
                checkbox.check(timeout=5000)
                action_selected = True

        if not action_selected:
            if self._is_order_already_complete(target, action_select_present=False):
                logger.info("Order is already Complete in SupplyPro — skipping")
                return "Already Complete"
            raise SupplyProCsrBlockedError(
                "Exception: CSR Blocked / Action Unavailable in SupplyPro"
            )

        update_btn_selectors = [
            "input[type='button'][value='Update']",
            "input[type='submit'][value='Update']",
            "input[value='Update' i]",
            "button:has-text('Update')",
            "a:has-text('Update')",
        ]
        logger.info("Clicking 'Update' to complete the order on SupplyPro...")

        def _accept_dialog(dialog) -> None:
            try:
                logger.info("Accepting SupplyPro browser confirmation dialog: %s", dialog.message)
                dialog.accept()
            except Exception:
                pass

        if self._page is not None:
            try:
                self._page.once("dialog", _accept_dialog)
            except Exception:
                pass

        if not self._click_first_matching(target, update_btn_selectors, timeout=8000):
            raise RuntimeError("Could not locate 'Update' button on Order Details page.")

        self._page.wait_for_load_state("domcontentloaded", timeout=30000)
        self._click_first_matching(target, ["button#modalOkButton", "button:has-text('OK')"], timeout=3000)
        logger.info("Order successfully marked as Completed and updated on SupplyPro.")
        return "Completed"

    def teardown_browser(self) -> None:
        """Signs out of SupplyPro (to avoid dangling session locks) and closes Playwright resources."""
        if self._page is not None:
            try:
                sign_out = self._find_visible_locator(
                    self._page,
                    ["a:has-text('Sign Out')", "a[href*='xt_logout.asp']"],
                    timeout=2000,
                )
                if sign_out is not None:
                    logger.info("Signing out of SupplyPro session...")
                    sign_out.click(timeout=5000)
                    self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception as exc:
                logger.debug("Ignored error during SupplyPro sign-out: %s", exc)

        for resource_name, resource, method in [
            ("page", self._page, "close"),
            ("context", self._context, "close"),
            ("browser", self._browser, "close"),
            ("playwright", self._playwright, "stop"),
        ]:
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception as exc:
                    logger.debug("Ignored error closing %s: %s", resource_name, exc)
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None


AUDIT_SYSTEM_INSTRUCTION = """
You are the Senior Reconciliation Auditor AI for Burgess Construction Consultants.
Your job is to perform a 4-factor semantic reconciliation between an internal Burgess database record (`expected`) and a scraped SupplyPro portal order (`scraped`).

STRICT 4-FACTOR MATCHING RULES:
1. Builder (`builderMatch`):
   - Allow corporate vs. division/regional naming variances (e.g., "Kendall Homes" vs. "Kendall Homes - Texas Division", or "Ashton Woods" vs. "Ashton Woods - Dallas" is a MATCH = true).
2. Address (`addressMatch`):
   - Allow standard street/directional abbreviation differences (e.g., "St" vs. "Street", "Ct" vs. "Court", "Way" vs. "Wy", "Ln" vs. "Lane") and the presence or absence of City, State, Zip Code, or Lot/Block prefixes, provided the core street number and street name match.
3. Inspection Type (`inspectionTypeMatch`):
   - Perform semantic matching using residential construction terminology.
   - IMPORTANT: "Frame", "Frame Inspection", "FRAME INSPECTION [00081-12488-1050][PO]", "Pre-Drywall", and "Pre-Drywall Frame" are semantically equivalent stages of construction and MUST be evaluated as `inspectionTypeMatch = true`.
4. Fee (`feeMatch`):
   - Require an exact cent-for-cent numerical match ($0.00 variance; compare rounded to 2 decimal places).
   - If no expected fee is available in the database record (`fee` is null/None), verify that the scraped amount is a valid positive fee (> 0).
5. Overall Verdict (`overallMatch`):
   - `overallMatch` MUST be `true` ONLY if all four checks (`builderMatch`, `addressMatch`, `inspectionTypeMatch`, and `feeMatch`) are `true`.
   - If any check is `false`, `overallMatch` MUST be `false` and each failed check must be clearly described in `discrepancies`.
"""


def _build_audit_system_instruction(
    prompt_instructions: Optional[str] = None,
    task_mappings: Optional[list[dict[str, Any]]] = None,
    is_reinspect: bool = False,
) -> str:
    """
    Combines the base 4-factor reconciliation system instruction with active
    `ai.SupplyProTaskMappings` rules, reinspect constraints, and optional
    job-specific `promptInstructions`.
    """
    base = AUDIT_SYSTEM_INSTRUCTION.strip()
    mappings_block = ""
    if task_mappings:
        lines = []
        for m in task_mappings:
            b_label = m.get("builderName") or "GLOBAL (All Builders)"
            lines.append(
                f"  - Builder: {b_label} | Internal TypeName: {m.get('internalInspectionType')!r} "
                f"<-> SupplyPro Task: {m.get('supplyProTaskName')!r}"
            )
        mappings_block = (
            "\n\nAuthorized SQL Task Equivalences (`BORE.ai.SupplyProTaskMappings` - ONLY permitted non-exact task equivalences):\n"
            + "\n".join(lines)
        )

    reinspect_guard = (
        "\n\nCRITICAL REINSPECTION & FIRST-TIME GUARDRAILS:\n"
        f"- Current order Reinspect bit (`is_reinspect`): {bool(is_reinspect)}\n"
        "- When `is_reinspect` is False (`Reinspect = 0`), you MUST REJECT (`inspectionTypeMatch = false`) any "
        "SupplyPro task containing reinspection terminology (`Reinspect`, `Reinspection`, `Re-Inspect`) OR tagged "
        "with `-1`, `(1)`, or `First-Time` / `First Time`.\n"
        "- Only approve non-exact inspection type names if they match an authorized equivalence in `ai.SupplyProTaskMappings`."
    )

    extra = (prompt_instructions or "").strip()
    extra_block = (
        f"\n\nAdditional Job-Specific Business Rules & Aliases:\n{extra}\n"
        "(Note: The 4-factor JSON output schema and the exact $0.00 fee match rule remain mandatory.)"
        if extra
        else ""
    )
    return f"{base}{mappings_block}{reinspect_guard}{extra_block}"


def audit_order_with_gemini(
    expected: dict[str, Any],
    scraped: dict[str, Any],
    po_number: Optional[str] = None,
    telemetry: Optional[TokenTelemetry] = None,
    prompt_instructions: Optional[str] = None,
    user_note: Optional[str] = None,
    cost_tracker: Optional[CostTracker] = None,
    model_name: str = DEFAULT_GEMINI_MODEL,
    task_mappings: Optional[list[dict[str, Any]]] = None,
    is_reinspect: Optional[bool] = None,
) -> SupplyProAuditResult:
    """
    Performs a 4-factor semantic comparison between the internal Burgess record (`expected`)
    and the scraped SupplyPro order (`scraped`) using Gemini, while accumulating
    token usage and cost telemetry via `CostTracker` and `TokenTelemetry`.
    """
    active_model = (model_name or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    effective_reinspect = (
        bool(is_reinspect)
        if is_reinspect is not None
        else _extract_reinspect_bool(expected)
    )
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").strip().upper() == "TRUE"
    client = genai.Client(
        vertexai=use_vertex,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )

    po_label = po_number or expected.get("poNumber") or scraped.get("poNumber") or "N/A"
    expected_fee_dec = _to_cents(expected.get("fee"))
    scraped_amt_dec = _to_cents(
        scraped.get("amount") if scraped.get("amount") is not None else scraped.get("orderAmountText")
    )

    expected_clean = {
        "builderName": expected.get("builderName"),
        "propertyAddress": expected.get("propertyAddress"),
        "inspectionType": expected.get("inspectionType"),
        "reinspect": effective_reinspect,
        "fee": f"{expected_fee_dec:.2f}" if expected_fee_dec is not None else None,
        "invoiceNumber": expected.get("invoiceNumber"),
        "dbRecordFound": expected.get("dbRecordFound", True),
    }
    scraped_clean = {
        "builder": scraped.get("builder") or scraped.get("customerBuilderName"),
        "address": scraped.get("address") or scraped.get("lotSubdivisionAddress"),
        "taskDescription": scraped.get("taskDescription"),
        "amount": f"{scraped_amt_dec:.2f}" if scraped_amt_dec is not None else scraped.get("amount"),
    }

    prompt = f"""
Perform a 4-factor reconciliation audit for Purchase Order #{po_label}.

1. EXPECTED (Internal Burgess Database Record - `ai.vw_AccountingExtract`):
{json.dumps(expected_clean, indent=2, default=str)}

2. SCRAPED (SupplyPro Order Detail Page):
{json.dumps(scraped_clean, indent=2, default=str)}

3. Raw SupplyPro Order Detail Page Text (for additional context):
\"\"\"
{scraped.get('rawPageText', '')}
\"\"\"

Evaluate `builderMatch`, `addressMatch`, `inspectionTypeMatch`, `feeMatch`, `overallMatch`, `confidenceScore` (0.0 to 1.0), `discrepancies` (empty list if all match), and `auditNotes`.
"""

    cleaned_user_note = (user_note or "").strip()
    if cleaned_user_note:
        prompt = (
            f"{prompt.rstrip()}\n\n"
            f"Accountant Override Note (treat as authoritative context for this order): {cleaned_user_note}\n"
        )

    system_instruction = _build_audit_system_instruction(
        prompt_instructions=prompt_instructions,
        task_mappings=task_mappings,
        is_reinspect=effective_reinspect,
    )

    response = client.models.generate_content(
        model=active_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=SupplyProAuditResult,
            temperature=0.0,
        ),
    )

    if cost_tracker is not None:
        call_cost = cost_tracker.record_usage(response, model_name=active_model)
        if telemetry is not None:
            telemetry.sync_from_cost_tracker(cost_tracker)
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        output_tokens = (
            int(getattr(usage, "candidates_token_count", 0) or 0)
            + int(getattr(usage, "thoughts_token_count", 0) or 0)
            if usage
            else 0
        )
        logger.info(
            "Gemini (%s) usage for PO %s: input=%d, output=%d, call_cost=$%.6f (cumulative cost=$%.6f)",
            active_model,
            po_label,
            prompt_tokens,
            output_tokens,
            call_cost,
            cost_tracker.total_cost,
        )
    else:
        usage = getattr(response, "usage_metadata", None)
        if usage is not None and telemetry is not None:
            temp_tracker = CostTracker(model_name=active_model)
            call_cost = temp_tracker.record_usage(response, model_name=active_model)
            prompt_tokens = temp_tracker.total_input_tokens
            output_tokens = temp_tracker.total_output_tokens
            cached_tokens = temp_tracker.total_cached_tokens
            total_tokens = int(getattr(usage, "total_token_count", 0) or (prompt_tokens + output_tokens))
            telemetry.add_usage(
                prompt_tokens,
                output_tokens,
                total_tokens=total_tokens,
                cached_tokens=cached_tokens,
                call_cost=call_cost,
            )
            logger.info(
                "Gemini (%s) usage for PO %s: input=%d, output=%d, total=%d (cumulative cost=$%.6f)",
                active_model,
                po_label,
                prompt_tokens,
                output_tokens,
                total_tokens,
                telemetry.estimated_cost_usd,
            )

    if response.parsed and isinstance(response.parsed, SupplyProAuditResult):
        verdict = response.parsed
    else:
        raw_json = (response.text or "{}").strip()
        verdict = SupplyProAuditResult.model_validate_json(raw_json)

    # Enforce deterministic Decimal cent-for-cent fee match ($0.00 variance)
    if expected_fee_dec is not None and scraped_amt_dec is not None:
        exact_cent_match = expected_fee_dec == scraped_amt_dec
        if exact_cent_match and not verdict.feeMatch:
            verdict.feeMatch = True
        elif not exact_cent_match and verdict.feeMatch:
            verdict.feeMatch = False
            msg = f"Fee mismatch: expected ${expected_fee_dec:.2f}, scraped ${scraped_amt_dec:.2f} ($0.00 variance required)."
            if msg not in verdict.discrepancies:
                verdict.discrepancies.append(msg)

    # Enforce hard invariant: overallMatch can only be True if all 4 checks are True
    enforced_overall = (
        verdict.builderMatch
        and verdict.addressMatch
        and verdict.inspectionTypeMatch
        and verdict.feeMatch
    )
    if verdict.overallMatch != enforced_overall:
        verdict.overallMatch = enforced_overall

    return verdict


def validate_supplypro_task(
    internal_inspection_type: str,
    supplypro_task_name: str,
    is_reinspect: bool,
    builder_name: Optional[str] = None,
    task_mappings: Optional[list[dict[str, Any]]] = None,
    expected_record: Optional[dict[str, Any]] = None,
    scraped_data: Optional[dict[str, Any]] = None,
    po_number: Optional[str] = None,
    telemetry: Optional[TokenTelemetry] = None,
    cost_tracker: Optional[CostTracker] = None,
    model_name: str = DEFAULT_GEMINI_MODEL,
    prompt_instructions: Optional[str] = None,
    user_note: Optional[str] = None,
) -> tuple[bool, SupplyProAuditResult]:
    """
    Validates the SupplyPro task against `internal_inspection_type` and `is_reinspect`
    following Rules 1.1–1.5 & 4.3:

    - If `is_reinspect` is True (`Reinspect = 1`):
        1. Check explicit active mappings in `ai.SupplyProTaskMappings` (matching `BuilderName` +
           `InternalInspectionType` first, then global `BuilderName IS NULL` rules, such as
           Rule 1.3 `Final - Davidson Homes-2 -> Q.I. Reinspect`) — $0.00 AI cost.
        2. Otherwise, accept any SupplyPro PO task whose name contains reinspection terminology
           (`reinspect`, `reinspection`, `re-inspect`, `re-inspection`, case-insensitive) — $0.00 AI cost.
        3. Reject standard/first-time SupplyPro PO tasks that do NOT indicate a reinspection.

    - If `is_reinspect` is False (`Reinspect = 0`):
        1. Reject SupplyPro PO tasks containing reinspection terminology (`reinspect`, `reinspection`, etc.)
           AND reject SupplyPro PO tasks tagged with `-1`, `(1)`, or `First-Time` (case-insensitive) — $0.00 AI cost.
        2. Compare `TypeName` (`internal_inspection_type`) against `supplypro_task_name` using:
           (a) exact case-insensitive match ($0.00 AI cost),
           (b) active rules in `ai.SupplyProTaskMappings` ($0.00 AI cost), or
           (c) guarded Gemini fallback passing `ai.SupplyProTaskMappings` as the only permitted equivalences.
    """
    int_type = (internal_inspection_type or "").strip()
    sp_task = (supplypro_task_name or "").strip()
    mismatch_note = (
        f"Exception: Inspection Type Mismatch "
        f"(Reinspect={int(bool(is_reinspect))}, Internal: '{int_type}' vs SupplyPro: '{sp_task}')"
    )

    def _deterministic_verdict(matched: bool, notes: str) -> SupplyProAuditResult:
        return SupplyProAuditResult(
            builderMatch=True,
            addressMatch=True,
            inspectionTypeMatch=matched,
            feeMatch=True,
            overallMatch=matched,
            confidenceScore=1.0 if matched else 0.0,
            discrepancies=[] if matched else [mismatch_note],
            auditNotes=notes if matched else mismatch_note,
        )

    if is_reinspect:
        # 1. Explicit active mappings in ai.SupplyProTaskMappings ($0.00 AI cost)
        mapped = match_sql_task_mapping(
            internal_inspection_type=int_type,
            supplypro_task_name=sp_task,
            builder_name=builder_name,
            task_mappings=task_mappings,
            is_reinspect=True,
        )
        if mapped is not None:
            note = (
                f"Matched SQL task mapping for reinspection "
                f"('{mapped.get('internalInspectionType')}' -> '{mapped.get('supplyProTaskName')}')."
            )
            return True, _deterministic_verdict(True, note)

        # 2. Any SupplyPro task containing reinspection terminology ($0.00 AI cost)
        if is_reinspection_task_name(sp_task):
            note = f"Matched reinspection terminology in SupplyPro task '{sp_task}' (Reinspect=1)."
            return True, _deterministic_verdict(True, note)

        # 3. Reject standard/first-time tasks when Reinspect=1
        return False, _deterministic_verdict(False, mismatch_note)

    # is_reinspect is False (Reinspect = 0)
    # 1. Reject reinspection terminology and -1 / (1) / First-Time tags deterministically ($0.00 AI cost)
    if is_reinspection_task_name(sp_task) or has_forbidden_first_time_tag(sp_task):
        return False, _deterministic_verdict(False, mismatch_note)

    # 2(a). Exact case-insensitive match ($0.00 AI cost)
    if int_type and sp_task and int_type.lower() == sp_task.lower():
        note = f"Exact case-insensitive inspection type match ('{int_type}' == '{sp_task}')."
        return True, _deterministic_verdict(True, note)

    # 2(b). Active rules in ai.SupplyProTaskMappings ($0.00 AI cost)
    mapped = match_sql_task_mapping(
        internal_inspection_type=int_type,
        supplypro_task_name=sp_task,
        builder_name=builder_name,
        task_mappings=task_mappings,
        is_reinspect=False,
    )
    if mapped is not None:
        note = (
            f"Matched SQL task mapping "
            f"('{mapped.get('internalInspectionType')}' -> '{mapped.get('supplyProTaskName')}')."
        )
        return True, _deterministic_verdict(True, note)

    # 2(c). Guarded Gemini fallback
    exp_dict = dict(expected_record or {})
    exp_dict.setdefault("builderName", builder_name or "")
    exp_dict.setdefault("inspectionType", int_type)
    exp_dict["reinspect"] = False

    scr_dict = dict(scraped_data or {})
    scr_dict.setdefault("taskDescription", sp_task)

    audit_kwargs: dict[str, Any] = {
        "expected": exp_dict,
        "scraped": scr_dict,
        "po_number": po_number or exp_dict.get("poNumber"),
        "telemetry": telemetry,
        "prompt_instructions": prompt_instructions,
    }
    if user_note:
        audit_kwargs["user_note"] = user_note

    sig_params = inspect.signature(audit_order_with_gemini).parameters
    if "cost_tracker" in sig_params:
        audit_kwargs["cost_tracker"] = cost_tracker
    if "model_name" in sig_params and model_name != DEFAULT_GEMINI_MODEL:
        audit_kwargs["model_name"] = model_name
    if "task_mappings" in sig_params:
        audit_kwargs["task_mappings"] = task_mappings
    if "is_reinspect" in sig_params:
        audit_kwargs["is_reinspect"] = False

    verdict = audit_order_with_gemini(**audit_kwargs)
    if telemetry is not None and cost_tracker is not None:
        if telemetry.input_tokens == 0 and cost_tracker.total_input_tokens > 0:
            telemetry.sync_from_cost_tracker(cost_tracker)
        elif telemetry.input_tokens > 0 and cost_tracker.total_input_tokens == 0:
            cost_tracker.total_input_tokens = telemetry.input_tokens
            cost_tracker.total_output_tokens = telemetry.output_tokens
            cost_tracker.total_cost = telemetry.estimated_cost_usd

    if not verdict.inspectionTypeMatch:
        verdict.overallMatch = False
        verdict.auditNotes = mismatch_note
        if mismatch_note not in verdict.discrepancies:
            verdict.discrepancies = [mismatch_note] + list(verdict.discrepancies)
        return False, verdict

    return True, verdict


def _print_reconciliation_report(
    po_number: str,
    expected: dict[str, Any],
    scraped: dict[str, Any],
    audit_verdict: SupplyProAuditResult,
    telemetry: TokenTelemetry,
    dry_run: bool,
) -> None:
    """Prints the expected vs. scraped comparison, Gemini's 4-factor verdict, and token telemetry."""
    divider = "=" * 80
    mode_tag = "DRY-RUN" if dry_run else "LIVE"
    print(f"\n{divider}")
    print(f"🔍 SUPPLYPRO 4-FACTOR RECONCILIATION REPORT (PO #{po_number}) [{mode_tag}]")
    print(divider)
    print("🗄️  Expected (Internal DB - ai.VW_AccountingExtractRevenue):")
    print(f"   • Builder Name        : {expected.get('builderName')}")
    print(f"   • Property Address    : {expected.get('propertyAddress')}")
    print(f"   • Inspection Type     : {expected.get('inspectionType')}")
    print(f"   • Expected Fee        : ${float(expected['fee']):,.2f}" if expected.get("fee") is not None else "   • Expected Fee        : N/A")
    print(f"   • Burgess Invoice #   : {expected.get('invoiceNumber') or 'N/A'}")
    print("\n📄 Scraped (SupplyPro OrderDetail.asp):")
    print(f"   • Page URL            : {scraped.get('pageUrl', 'N/A')}")
    print(f"   • Builder             : {scraped.get('builder')}")
    print(f"   • Address             : {scraped.get('address')}")
    print(f"   • Task Description    : {scraped.get('taskDescription')}")
    scraped_amt = scraped.get("amount")
    if isinstance(scraped_amt, (int, float)):
        print(f"   • Scraped Amount      : ${scraped_amt:,.2f}")
    else:
        print(f"   • Scraped Amount      : {scraped_amt}")
    print("\n📊 Gemini 2.5 Flash Telemetry:")
    print(
        f"   • Tokens              : {telemetry.input_tokens:,} input / "
        f"{telemetry.output_tokens:,} output ({telemetry.total_tokens:,} total)"
    )
    print(f"   • Estimated Cost      : ${telemetry.estimated_cost_usd:.6f} USD")
    print("\n🤖 Gemini 2.5 Flash 4-Factor Verdict (JSON):")
    print(json.dumps(audit_verdict.model_dump(), indent=2))
    print(divider + "\n")


def _is_not_found_error(err: Exception) -> bool:
    """Returns True if the exception indicates that the PO was not found in SupplyPro Order Search."""
    if isinstance(err, SupplyProOrderNotFoundError):
        return True
    msg = str(err).lower()
    return "was not found in supplypro" in msg or "not found in supplypro" in msg


def _is_session_redirect_error(err: Exception, driver: Any) -> bool:
    """Returns True if the exception was caused by SupplyPro redirecting to the login screen."""
    if isinstance(err, SupplyProSessionExpiredError):
        return True
    if hasattr(driver, "is_session_expired") and callable(driver.is_session_expired):
        try:
            if driver.is_session_expired():
                return True
        except Exception:
            pass
    msg = str(err).lower()
    return "login.asp" in msg or "session redirected to login" in msg or "session expired" in msg


def _is_csr_blocked_error(err: Exception) -> bool:
    """Returns True if the exception indicates a CSR-blocked or unavailable Complete Order action."""
    if isinstance(err, SupplyProCsrBlockedError):
        return True
    msg = str(err).lower()
    return (
        "csr blocked" in msg
        or "action unavailable in supplypro" in msg
        or "could not locate 'complete order' action selector" in msg
    )


def _is_scraped_order_already_complete(scraped_data: dict[str, Any]) -> bool:
    """Returns True if the scraped OrderDetail payload indicates the order is already Complete."""
    if scraped_data.get("isAlreadyComplete") is True:
        return True
    status_str = str(scraped_data.get("orderStatus") or "").strip().lower()
    if status_str in ("complete", "completed"):
        return True
    raw_text = str(scraped_data.get("rawPageText") or "")
    if re.search(r"(?:Order\s*Status|Status)\s*[:\-]?\s*Complete(?:d)?\b", raw_text, flags=re.IGNORECASE):
        return True
    return False


def _is_scraped_order_csr_blocked(scraped_data: dict[str, Any]) -> bool:
    """
    Returns True if the Order Details page shows the order is NOT already Complete
    and the 'Complete Order' action is missing or disabled (CSR blocked).
    """
    if _is_scraped_order_already_complete(scraped_data):
        return False
    if scraped_data.get("isCsrBlocked") is True or scraped_data.get("csrBlocked") is True:
        return True
    if scraped_data.get("isActionAvailable") is False or scraped_data.get("actionAvailable") is False:
        return True
    return False


def _scrape_po_with_session_recovery(
    driver: Any,
    po_number: str,
    is_reinspect: bool = False,
    internal_inspection_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Calls `driver.search_and_scrape_order` for `po_number` with automatic session re-login
    if SupplyPro redirects to `login.asp`.
    """
    def _invoke_scrape() -> dict[str, Any]:
        sig_params = inspect.signature(driver.search_and_scrape_order).parameters
        kwargs: dict[str, Any] = {}
        if "is_reinspect" in sig_params:
            kwargs["is_reinspect"] = is_reinspect
        if "internal_inspection_type" in sig_params:
            kwargs["internal_inspection_type"] = internal_inspection_type
        return driver.search_and_scrape_order(po_number, **kwargs)

    try:
        scraped = _invoke_scrape()
        if hasattr(driver, "is_session_expired") and callable(driver.is_session_expired) and driver.is_session_expired():
            raise SupplyProSessionExpiredError(
                f"SupplyPro redirected to login screen after scraping PO {po_number}."
            )
        return scraped
    except Exception as first_err:
        if not _is_not_found_error(first_err) and _is_session_redirect_error(first_err, driver):
            logger.warning(
                "SupplyPro session expired on PO %s (%s); re-authenticating and retrying once...",
                po_number,
                first_err,
            )
            driver.login()
            progress_logger.info("Re-authenticated into SupplyPro after session timeout")
            return _invoke_scrape()
        raise


def _search_po_with_fallback(
    driver: Any,
    po_item: NormalizedPOLookup,
    is_reinspect: bool = False,
    internal_inspection_type: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """
    Two-Step SupplyPro Search (Rules 2.3 & 4.1):
    Searches SupplyPro using `po_item.primary_po`. If zero results return and
    `po_item.fallback_po` is set, immediately retries with `po_item.fallback_po`.
    Returns `(matched_po, scraped_data)` or raises `SupplyProOrderNotFoundError`.
    """
    try:
        scraped = _scrape_po_with_session_recovery(
            driver,
            po_item.primary_po,
            is_reinspect=is_reinspect,
            internal_inspection_type=internal_inspection_type,
        )
        return po_item.primary_po, scraped
    except Exception as primary_err:
        if _is_not_found_error(primary_err) and po_item.fallback_po:
            logger.info(
                "Primary PO %r not found in SupplyPro; retrying with fallback PO %r...",
                po_item.primary_po,
                po_item.fallback_po,
            )
            scraped = _scrape_po_with_session_recovery(
                driver,
                po_item.fallback_po,
                is_reinspect=is_reinspect,
                internal_inspection_type=internal_inspection_type,
            )
            return po_item.fallback_po, scraped
        raise


def _resolve_marked_orders(
    marked_items: list[dict[str, Any]],
    client: BurgessApiClient,
    parsed_params: SupplyProParameters,
    explicit_extract_type: Optional[str],
    active_posted_date: Optional[str],
    active_user_note: Optional[str],
) -> list[dict[str, Any]]:
    """
    Resolves a list of user-marked items (`items`, `poNumbers`, `invoiceNumbers`)
    into normalized expected order records for single-session SupplyPro processing.
    """
    resolved_orders: list[dict[str, Any]] = []
    seen_records: set[tuple[str, str]] = set()

    for item_desc in marked_items:
        item_po = _get_case_insensitive(item_desc, "poNumber", "po_number", "po", "PONumber")
        item_inv = _get_case_insensitive(item_desc, "invoiceNumber", "invoice_number", "invoice", "InvoiceNumber")
        item_note = _get_case_insensitive(item_desc, "userNote", "user_note") or active_user_note
        item_ext = (
            _get_case_insensitive(item_desc, "extractType", "extract_type")
            or explicit_extract_type
        )
        clean_po = str(item_po).strip() if item_po is not None and str(item_po).strip() else None
        clean_inv = str(item_inv).strip() if item_inv is not None and str(item_inv).strip() else None
        clean_ext = str(item_ext).strip() if item_ext is not None and str(item_ext).strip() else None

        raw_matches: list[dict[str, Any]] = []
        try:
            raw_matches = client.get_pending_supplypro_orders(
                extract_type=clean_ext,
                limit=parsed_params.batch_limit,
                posted_date=active_posted_date,
                invoice_number=clean_inv,
                po_number=clean_po,
            )
            if not raw_matches and clean_ext is not None:
                # Retry without extractType filter so record is found regardless of fee sign
                raw_matches = client.get_pending_supplypro_orders(
                    extract_type=None,
                    limit=parsed_params.batch_limit,
                    posted_date=active_posted_date,
                    invoice_number=clean_inv,
                    po_number=clean_po,
                )
        except Exception as lookup_err:
            logger.warning(
                "Marked-item lookup via get_pending_supplypro_orders failed (invoice=%s, po=%s): %s",
                clean_inv,
                clean_po,
                lookup_err,
            )

        matched_records = [
            normalize_pending_order_record(r)
            for r in raw_matches
            if _get_case_insensitive(r, "poNumber", "PONumber", "po")
        ]

        # If both invoiceNumber and poNumber were specified and multiple rows returned, narrow by invoiceNumber
        if clean_inv and len(matched_records) > 1:
            exact_inv_matches = [
                r for r in matched_records if str(r.get("invoiceNumber") or "").strip() == clean_inv
            ]
            if exact_inv_matches:
                matched_records = exact_inv_matches

        if not matched_records and clean_po:
            # Check if inline expected fields were provided on item_desc, else fallback to DB query
            if _get_case_insensitive(item_desc, "builderName", "BuilderName"):
                fallback_rec = normalize_pending_order_record(item_desc)
            else:
                fallback_rec = fetch_expected_order_from_db(clean_po, fallback_params=parsed_params)
                if clean_inv and not fallback_rec.get("invoiceNumber"):
                    fallback_rec["invoiceNumber"] = clean_inv
            matched_records = [fallback_rec]

        for rec in matched_records:
            if item_note:
                rec["userNote"] = str(item_note).strip()
            if clean_ext:
                rec["extractType"] = clean_ext
            rec_key = (str(rec.get("invoiceNumber") or ""), str(rec.get("poNumber") or ""))
            if rec_key not in seen_records:
                seen_records.add(rec_key)
                resolved_orders.append(rec)

    return resolved_orders


def run_supplypro_sync(
    parameters: dict[str, Any],
    allowed_skills: Optional[list[str]] = None,
    dry_run: bool = False,
    po: Optional[str] = None,
    invoice_number: Optional[str] = None,
    user_note: Optional[str] = None,
    prompt_instructions: Optional[str] = None,
    execution_id: Optional[int] = None,
    posted_date: Optional[str] = None,
    api_client: Optional[BurgessApiClient] = None,
    cost_tracker: Optional[CostTracker] = None,
) -> SkillExecutionResult:
    """
    Entry point for the SupplyPro PortalSync skill.

    Supports:
      - Marked list mode (`items`, `poNumbers`, `invoiceNumbers` in `RunParametersJson` for
        "Reconcile Marked" — resolves all selected records and processes them sequentially in
        a single SupplyPro browser/login session).
      - Targeted single-order mode (when `invoice_number` or `po` is supplied via CLI or `runParameters`,
        querying `GET /api/accounting/supplypro/pending` with `invoiceNumber`/`poNumber` to bypass
        `FlaggedForReview` exclusion and optionally applying `user_note` accountant override context).
      - Daily batch queue mode (fetches pending orders from `GET /api/accounting/supplypro/pending`,
        opens a single Playwright session with automatic mid-batch session recovery, audits each
        order with Gemini 2.5 Flash, and logs `Reconciled`, `FlaggedForReview`, or `NotFound`
        to `POST /api/accounting/supplypro/reconcile` when `dry_run=False`).
    """
    unpacked_params = unpack_skill_parameters(parameters)
    if po:
        unpacked_params["po"] = str(po).strip()
    if invoice_number:
        unpacked_params["invoiceNumber"] = str(invoice_number).strip()
    if user_note:
        unpacked_params["userNote"] = str(user_note).strip()
    if prompt_instructions:
        unpacked_params["promptInstructions"] = str(prompt_instructions).strip()
    if execution_id is not None:
        unpacked_params["executionId"] = int(execution_id)
    if posted_date:
        unpacked_params["postedDate"] = str(posted_date).strip()

    parsed_params = SupplyProParameters.model_validate(unpacked_params)
    if not dry_run and parsed_params.dry_run is True:
        dry_run = True
    active_execution_id = execution_id if execution_id is not None else parsed_params.execution_id
    active_posted_date = posted_date or parsed_params.posted_date
    active_po_number = (str(po).strip() if po else None) or (
        str(parsed_params.po).strip() if parsed_params.po else None
    )
    active_invoice_number = (str(invoice_number).strip() if invoice_number else None) or (
        str(parsed_params.invoice_number).strip() if parsed_params.invoice_number else None
    )
    active_user_note = (str(user_note).strip() if user_note else None) or (
        str(parsed_params.user_note).strip() if parsed_params.user_note else None
    )
    mode_prefix = "[DRY-RUN] " if dry_run else ""

    owns_api_client = api_client is None
    client = api_client or BurgessApiClient()

    try:
        # Build the queue of expected order records to process
        orders_to_process: list[dict[str, Any]] = []
        marked_items = parsed_params.items or extract_marked_items(unpacked_params)
        marked_list_mode = bool(marked_items)
        single_order_mode = bool(not marked_list_mode and (active_po_number or active_invoice_number))

        raw_run_params = _safe_json_dict(
            _get_case_insensitive(
                parameters,
                "runParameters",
                "run_parameters",
                "runParametersJson",
                "RunParametersJson",
                "run_parameters_json",
            )
        )
        explicit_extract_type = _get_case_insensitive(
            raw_run_params, "extractType", "extract_type", "ExtractType"
        ) or _get_case_insensitive(parameters, "extractType", "extract_type", "ExtractType")

        if marked_list_mode:
            orders_to_process = _resolve_marked_orders(
                marked_items=marked_items,
                client=client,
                parsed_params=parsed_params,
                explicit_extract_type=str(explicit_extract_type).strip() if explicit_extract_type else None,
                active_posted_date=active_posted_date,
                active_user_note=active_user_note,
            )
        elif single_order_mode:
            # For single-invoice/PO lookups, leave extractType as None unless explicitly
            # specified in runParameters so dbo.bspGetAccountingExtract finds the record
            # regardless of fee sign (Revenue vs Credit).
            raw_targeted: list[dict[str, Any]] = []
            try:
                raw_targeted = client.get_pending_supplypro_orders(
                    extract_type=str(explicit_extract_type).strip() if explicit_extract_type else None,
                    limit=parsed_params.batch_limit,
                    posted_date=active_posted_date,
                    invoice_number=active_invoice_number,
                    po_number=active_po_number,
                )
            except Exception as api_lookup_err:
                logger.warning(
                    "Targeted get_pending_supplypro_orders lookup failed (invoice=%s, po=%s): %s",
                    active_invoice_number,
                    active_po_number,
                    api_lookup_err,
                )

            orders_to_process = [
                normalize_pending_order_record(item)
                for item in raw_targeted
                if _get_case_insensitive(item, "poNumber", "PONumber", "po")
            ]

            if not orders_to_process and active_po_number:
                orders_to_process = [
                    fetch_expected_order_from_db(active_po_number, fallback_params=parsed_params)
                ]
        elif parsed_params.po_queue:
            for queued_po in parsed_params.po_queue[: parsed_params.batch_limit]:
                clean_po = str(queued_po).strip()
                if clean_po:
                    orders_to_process.append(fetch_expected_order_from_db(clean_po, fallback_params=parsed_params))
        else:
            raw_pending = client.get_pending_supplypro_orders(
                extract_type=parsed_params.extract_type,
                limit=parsed_params.batch_limit,
                posted_date=active_posted_date,
            )
            orders_to_process = [
                normalize_pending_order_record(item)
                for item in raw_pending
                if _get_case_insensitive(item, "poNumber", "PONumber", "po")
            ]

        total_orders = len(orders_to_process)
        progress_logger.info("Loaded %d pending orders", total_orders)

        if not orders_to_process:
            empty_summary = (
                f"{mode_prefix}SupplyPro Reconciliation ({parsed_params.extract_type}): "
                f"0 pending records found (postedDate={active_posted_date or 'today-CT'})."
            )
            logger.info(empty_summary)
            return SkillExecutionResult(
                status="Completed",
                records_processed=0,
                records_exception=0,
                records_failed=0,
                output_summary=empty_summary,
                details=[],
                telemetry=TokenTelemetry(),
            )

        extra_task_mappings = _get_case_insensitive(
            unpacked_params,
            "taskMappings",
            "task_mappings",
            "SupplyProTaskMappings",
            "supplypro_task_mappings",
        )
        task_mappings = load_supplypro_task_mappings(
            extra_mappings=extra_task_mappings if isinstance(extra_task_mappings, list) else None
        )

        driver = SupplyProPortalDriver(params=parsed_params, dry_run=dry_run)
        active_cost_tracker = cost_tracker if cost_tracker is not None else CostTracker(model_name=parsed_params.model_name)
        telemetry = TokenTelemetry()
        details_list: list[dict[str, Any]] = []
        processed_count = 0
        exception_count = 0
        failed_count = 0
        order_summaries: list[str] = []

        try:
            driver.setup_browser()
            driver.login()
            progress_logger.info("Logged into SupplyPro")

            for idx, expected_record in enumerate(orders_to_process, start=1):
                raw_po = str(expected_record.get("poNumber") or "").strip()
                if not raw_po:
                    continue

                invoice_num = (
                    expected_record.get("invoiceNumber")
                    or active_invoice_number
                    or ""
                )
                inspection_id = expected_record.get("inspectionId")
                expected_fee = expected_record.get("fee")
                total_fee_float = float(expected_fee) if expected_fee is not None else 0.0
                is_reinspect = _extract_reinspect_bool(expected_record)
                internal_inspection_type = str(expected_record.get("inspectionType") or "").strip()
                builder_name = str(expected_record.get("builderName") or "").strip()
                item_user_note = expected_record.get("userNote") or active_user_note
                item_extract_type = expected_record.get("extractType") or parsed_params.extract_type

                po_items = normalize_and_split_po(raw_po, total_fee_float)
                if not po_items:
                    continue

                # Check before navigating whether SupplyPro redirected back to login.asp
                if hasattr(driver, "ensure_authenticated") and callable(driver.ensure_authenticated):
                    driver.ensure_authenticated()

                validated_children: list[dict[str, Any]] = []
                group_exception_notes: list[str] = []
                technical_crash_err: Optional[Exception] = None
                last_scraped_data: dict[str, Any] = {}
                last_audit_verdict: Optional[SupplyProAuditResult] = None
                total_scraped_fee: float = 0.0
                any_scraped_fee: bool = False

                # Phase 1: Validate ALL child POs first (Atomic Dual-PO Validation — Rule 3.1)
                for child_po in po_items:
                    searched_desc = (
                        f"{child_po.primary_po}, fallback: {child_po.fallback_po}"
                        if child_po.fallback_po
                        else child_po.primary_po
                    )
                    try:
                        matched_po, scraped_data = _search_po_with_fallback(
                            driver,
                            po_item=child_po,
                            is_reinspect=is_reinspect,
                            internal_inspection_type=internal_inspection_type,
                        )
                        last_scraped_data = scraped_data
                    except Exception as search_err:
                        if _is_not_found_error(search_err):
                            missing_note = f"Exception: Missing PO in SupplyPro (searched: {searched_desc})"
                            logger.warning("PO %s missing in SupplyPro: %s", raw_po, missing_note)
                            group_exception_notes.append(missing_note)
                            break
                        if _is_csr_blocked_error(search_err):
                            csr_note = "Exception: CSR Blocked / Action Unavailable in SupplyPro"
                            logger.warning("PO %s CSR blocked in SupplyPro: %s", raw_po, search_err)
                            group_exception_notes.append(csr_note)
                            break
                        technical_crash_err = search_err
                        break

                    already_complete = _is_scraped_order_already_complete(scraped_data)
                    if not already_complete and _is_scraped_order_csr_blocked(scraped_data):
                        csr_note = "Exception: CSR Blocked / Action Unavailable in SupplyPro"
                        logger.warning("PO %s is CSR blocked / action unavailable on OrderDetail.asp", matched_po)
                        group_exception_notes.append(csr_note)
                        break

                    scraped_amt_dec = _to_cents(
                        scraped_data.get("amount")
                        if scraped_data.get("amount") is not None
                        else scraped_data.get("amountText")
                    )
                    supplypro_fee = float(scraped_amt_dec) if scraped_amt_dec is not None else None
                    if supplypro_fee is not None:
                        total_scraped_fee = round(total_scraped_fee + supplypro_fee, 2)
                        any_scraped_fee = True

                    # Rule 4.2: Deterministic Fee Comparison (full fee for single POs, split fee for dual POs)
                    if expected_fee is not None:
                        if supplypro_fee is None or abs(float(child_po.expected_fee) - supplypro_fee) > 0.01:
                            sp_val = supplypro_fee if supplypro_fee is not None else 0.0
                            fee_note = (
                                f"Exception: Fee Discrepancy "
                                f"(Internal: ${float(child_po.expected_fee):.2f} vs SupplyPro: ${sp_val:.2f})"
                            )
                            logger.warning("Fee discrepancy on PO %s: %s", matched_po, fee_note)
                            group_exception_notes.append(fee_note)
                            break

                    # Rules 1.1–1.5 & 4.3: Reinspection Bit Rules + SQL Task Mapping + Guarded Gemini Fallback
                    supplypro_task_name = str(scraped_data.get("taskDescription") or "").strip()
                    child_expected_rec = dict(expected_record)
                    child_expected_rec["poNumber"] = matched_po
                    child_expected_rec["fee"] = child_po.expected_fee
                    child_expected_rec["reinspect"] = is_reinspect

                    try:
                        task_ok, audit_verdict = validate_supplypro_task(
                            internal_inspection_type=internal_inspection_type,
                            supplypro_task_name=supplypro_task_name,
                            is_reinspect=is_reinspect,
                            builder_name=builder_name,
                            task_mappings=task_mappings,
                            expected_record=child_expected_rec,
                            scraped_data=scraped_data,
                            po_number=matched_po,
                            telemetry=telemetry,
                            cost_tracker=active_cost_tracker,
                            model_name=parsed_params.model_name,
                            prompt_instructions=parsed_params.prompt_instructions,
                            user_note=item_user_note,
                        )
                        last_audit_verdict = audit_verdict
                    except Exception as audit_err:
                        technical_crash_err = audit_err
                        break

                    _print_reconciliation_report(
                        po_number=matched_po,
                        expected=child_expected_rec,
                        scraped=scraped_data,
                        audit_verdict=audit_verdict,
                        telemetry=telemetry,
                        dry_run=dry_run,
                    )

                    if not task_ok:
                        task_note = (
                            f"Exception: Inspection Type Mismatch "
                            f"(Reinspect={int(bool(is_reinspect))}, Internal: '{internal_inspection_type}' vs SupplyPro: '{supplypro_task_name}')"
                        )
                        logger.warning("Inspection type mismatch on PO %s: %s", matched_po, task_note)
                        group_exception_notes.append(task_note)
                        break

                    if not audit_verdict.overallMatch:
                        disc_str = "; ".join(audit_verdict.discrepancies) or audit_verdict.auditNotes
                        exc_note = disc_str if disc_str.startswith("Exception:") else f"Exception: {disc_str}"
                        logger.warning("Audit mismatch on PO %s: %s", matched_po, exc_note)
                        group_exception_notes.append(exc_note)
                        break

                    validated_children.append(
                        {
                            "po_item": child_po,
                            "matched_po": matched_po,
                            "scraped_data": scraped_data,
                            "already_complete": already_complete,
                            "supplypro_fee": supplypro_fee,
                            "audit_verdict": audit_verdict,
                        }
                    )

                # Handle technical/Playwright crash strictly as `Failed`
                if technical_crash_err is not None:
                    if single_order_mode:
                        raise technical_crash_err
                    logger.error("Technical error processing PO %s in batch: %s", raw_po, technical_crash_err)
                    failed_count += 1
                    err_note = f"Technical error: {type(technical_crash_err).__name__}: {technical_crash_err}"
                    if not dry_run:
                        try:
                            client.log_supplypro_reconciliation({
                                "executionId": active_execution_id,
                                "inspectionId": inspection_id,
                                "extractType": item_extract_type,
                                "poNumber": raw_po,
                                "invoiceNumber": str(invoice_num) if invoice_num else None,
                                "status": "Failed",
                                "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                                "scrapedAmount": total_scraped_fee if any_scraped_fee else None,
                                "auditNotes": err_note,
                            })
                        except Exception as log_err:
                            logger.error("Failed to log technical crash for PO %s: %s", raw_po, log_err)

                    details_list.append({
                        "PONumber": raw_po,
                        "InvoiceNumber": str(invoice_num),
                        "BuilderExpected": builder_name,
                        "BuilderScraped": last_scraped_data.get("builder") or "",
                        "InspectionExpected": internal_inspection_type,
                        "TaskScraped": last_scraped_data.get("taskDescription") or "",
                        "FeeExpected": expected_fee,
                        "FeeScraped": total_scraped_fee if any_scraped_fee else None,
                        "Status": "Failed",
                        "ConfidenceScore": 0.0,
                        "Discrepancies": [err_note],
                        "AuditNotes": err_note,
                        "overallMatch": False,
                    })
                    order_summaries.append(f"PO {raw_po}: FAILED ({technical_crash_err})")
                    progress_logger.info(
                        "[%d/%d] PO %s -> Failed (%s)",
                        idx,
                        total_orders,
                        raw_po,
                        err_note,
                    )
                    continue

                # Phase 2: Atomic Completion Gate — only complete if EVERY child PO passed validation
                if not group_exception_notes and len(validated_children) == len(po_items):
                    portal_results: list[str] = []
                    if not dry_run:
                        try:
                            for child_idx, v_child in enumerate(validated_children):
                                if v_child["already_complete"]:
                                    logger.info(
                                        "Order %s is already Complete in SupplyPro — skipping",
                                        v_child["matched_po"],
                                    )
                                    portal_results.append("Already Complete")
                                    continue

                                # For dual-PO splits with multiple open children, navigate back to earlier child POs before completing
                                if len(validated_children) > 1 and child_idx < len(validated_children) - 1:
                                    _scrape_po_with_session_recovery(
                                        driver,
                                        v_child["matched_po"],
                                        is_reinspect=is_reinspect,
                                        internal_inspection_type=internal_inspection_type,
                                    )
                                elif len(validated_children) > 1 and child_idx == len(validated_children) - 1:
                                    # If an earlier child was completed, the page navigated away from the last child
                                    if any(r == "Completed" for r in portal_results):
                                        _scrape_po_with_session_recovery(
                                            driver,
                                            v_child["matched_po"],
                                            is_reinspect=is_reinspect,
                                            internal_inspection_type=internal_inspection_type,
                                        )

                                action_res = driver.complete_order_on_portal(
                                    invoice_number=str(invoice_num or v_child["matched_po"])
                                )
                                portal_results.append(action_res)
                        except Exception as complete_err:
                            if _is_csr_blocked_error(complete_err):
                                group_exception_notes.append(
                                    "Exception: CSR Blocked / Action Unavailable in SupplyPro"
                                )
                            else:
                                if single_order_mode:
                                    raise
                                logger.error(
                                    "Technical error completing PO %s on SupplyPro: %s",
                                    raw_po,
                                    complete_err,
                                )
                                failed_count += 1
                                err_note = f"Technical error: {type(complete_err).__name__}: {complete_err}"
                                if not dry_run:
                                    try:
                                        client.log_supplypro_reconciliation({
                                            "executionId": active_execution_id,
                                            "inspectionId": inspection_id,
                                            "extractType": item_extract_type,
                                            "poNumber": raw_po,
                                            "invoiceNumber": str(invoice_num) if invoice_num else None,
                                            "status": "Failed",
                                            "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                                            "scrapedAmount": total_scraped_fee if any_scraped_fee else None,
                                            "auditNotes": err_note,
                                        })
                                    except Exception as log_err:
                                        logger.error("Failed to log completion crash for PO %s: %s", raw_po, log_err)
                                details_list.append({
                                    "PONumber": raw_po,
                                    "InvoiceNumber": str(invoice_num),
                                    "BuilderExpected": builder_name,
                                    "BuilderScraped": last_scraped_data.get("builder") or "",
                                    "InspectionExpected": internal_inspection_type,
                                    "TaskScraped": last_scraped_data.get("taskDescription") or "",
                                    "FeeExpected": expected_fee,
                                    "FeeScraped": total_scraped_fee if any_scraped_fee else None,
                                    "Status": "Failed",
                                    "ConfidenceScore": 0.0,
                                    "Discrepancies": [err_note],
                                    "AuditNotes": err_note,
                                    "overallMatch": False,
                                })
                                order_summaries.append(f"PO {raw_po}: FAILED ({complete_err})")
                                progress_logger.info(
                                    "[%d/%d] PO %s -> Failed (%s)",
                                    idx,
                                    total_orders,
                                    raw_po,
                                    err_note,
                                )
                                continue
                    else:
                        for v_child in validated_children:
                            portal_results.append(
                                "Already Complete" if v_child["already_complete"] else "DryRun"
                            )

                # Record Exception if any validation or CSR-blocked check triggered
                if group_exception_notes:
                    exception_count += 1
                    reconcile_status = "Exception"
                    row_status = "Exception (Dry-Run)" if dry_run else "Exception"
                    combined_exc_note = " | ".join(group_exception_notes)

                    if not dry_run:
                        try:
                            client.log_supplypro_reconciliation({
                                "executionId": active_execution_id,
                                "inspectionId": inspection_id,
                                "extractType": item_extract_type,
                                "poNumber": raw_po,
                                "invoiceNumber": str(invoice_num) if invoice_num else None,
                                "status": reconcile_status,
                                "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                                "scrapedAmount": total_scraped_fee if any_scraped_fee else None,
                                "auditNotes": combined_exc_note,
                            })
                        except Exception as log_err:
                            logger.error("Failed to log Exception status for PO %s: %s", raw_po, log_err)

                    details_list.append({
                        "PONumber": raw_po,
                        "InvoiceNumber": str(invoice_num),
                        "BuilderExpected": builder_name,
                        "BuilderScraped": last_scraped_data.get("builder") or "",
                        "InspectionExpected": internal_inspection_type,
                        "TaskScraped": last_scraped_data.get("taskDescription") or "",
                        "FeeExpected": expected_fee,
                        "FeeScraped": total_scraped_fee if any_scraped_fee else None,
                        "Status": row_status,
                        "ConfidenceScore": 0.0,
                        "Discrepancies": group_exception_notes,
                        "AuditNotes": combined_exc_note,
                        "overallMatch": False,
                    })
                    order_summaries.append(f"PO {raw_po}: EXCEPTION ({combined_exc_note})")
                    progress_logger.info(
                        "[%d/%d] PO %s -> Exception (%s)",
                        idx,
                        total_orders,
                        raw_po,
                        combined_exc_note,
                    )
                    continue

                # All child POs validated and completed (or skipped as Already Complete)
                processed_count += 1
                reconcile_status = "Reconciled"
                all_already_complete = bool(portal_results) and all(r == "Already Complete" for r in portal_results)
                row_status = (
                    "Already Complete"
                    if all_already_complete
                    else ("Reconciled (Dry-Run)" if dry_run else "Reconciled")
                )
                fee_display = (
                    f"${total_scraped_fee:.2f}"
                    if any_scraped_fee
                    else (f"${float(expected_fee):.2f}" if expected_fee is not None else "N/A")
                )
                final_verdict = last_audit_verdict or SupplyProAuditResult(
                    builderMatch=True,
                    addressMatch=True,
                    inspectionTypeMatch=True,
                    feeMatch=True,
                    overallMatch=True,
                    confidenceScore=1.0,
                    discrepancies=[],
                    auditNotes="All checks passed.",
                )
                final_notes = final_verdict.auditNotes
                if all_already_complete:
                    if "already complete" not in (final_notes or "").lower():
                        final_notes = f"Already Complete | {final_notes}" if final_notes else "Already Complete"
                    order_summaries.append(
                        f"PO {raw_po}: ALREADY COMPLETE (Invoice={invoice_num or 'N/A'}, Confidence={final_verdict.confidenceScore:.2f})"
                    )
                    progress_logger.info(
                        "[%d/%d] PO %s -> Already Complete (%s)",
                        idx,
                        total_orders,
                        raw_po,
                        fee_display,
                    )
                else:
                    order_summaries.append(
                        f"PO {raw_po}: MATCH (Invoice={invoice_num or 'N/A'}, Confidence={final_verdict.confidenceScore:.2f})"
                    )
                    progress_logger.info(
                        "[%d/%d] PO %s -> Reconciled (%s)",
                        idx,
                        total_orders,
                        raw_po,
                        fee_display,
                    )

                if not dry_run:
                    client.log_supplypro_reconciliation({
                        "executionId": active_execution_id,
                        "inspectionId": inspection_id,
                        "extractType": item_extract_type,
                        "poNumber": raw_po,
                        "invoiceNumber": str(invoice_num) if invoice_num else None,
                        "status": reconcile_status,
                        "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                        "scrapedAmount": total_scraped_fee if any_scraped_fee else None,
                        "auditNotes": final_notes,
                    })

                row_detail = {
                    "PONumber": raw_po,
                    "InvoiceNumber": str(invoice_num),
                    "BuilderExpected": builder_name,
                    "BuilderScraped": last_scraped_data.get("builder") or "",
                    "InspectionExpected": internal_inspection_type,
                    "TaskScraped": last_scraped_data.get("taskDescription") or "",
                    "FeeExpected": expected_fee,
                    "FeeScraped": total_scraped_fee if any_scraped_fee else last_scraped_data.get("amount"),
                    "Status": row_status,
                    "ConfidenceScore": final_verdict.confidenceScore,
                    "Discrepancies": final_verdict.discrepancies,
                    "AuditNotes": final_notes,
                    **final_verdict.model_dump(),
                }
                details_list.append(row_detail)

            telemetry_summary = (
                f"Tokens: {telemetry.input_tokens:,} in / {telemetry.output_tokens:,} out "
                f"({telemetry.total_tokens:,} total) | Est. Cost: ${telemetry.estimated_cost_usd:.6f} USD"
            )
            summary = (
                f"{mode_prefix}SupplyPro Reconciliation "
                f"({processed_count} reconciled, {exception_count} exceptions, {failed_count} failed) | "
                f"{' ; '.join(order_summaries)} | {telemetry_summary}"
            )
            logger.info(summary)

            return SkillExecutionResult(
                status="Completed",
                records_processed=processed_count,
                records_exception=exception_count,
                records_failed=failed_count,
                output_summary=summary,
                details=details_list,
                telemetry=telemetry,
            )
        finally:
            driver.teardown_browser()
    finally:
        if owns_api_client:
            client.close()
