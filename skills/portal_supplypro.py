"""
SupplyPro Portal Sync skill driver with real Playwright browser automation,
Gemini 2.5 Flash 4-factor semantic reconciliation auditing, daily batch queue
processing via BurgessApi, and token/cost telemetry.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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

load_dotenv(override=True)

logger = logging.getLogger(__name__)
progress_logger = logging.getLogger("burgess_buddy.progress")


class SupplyProOrderNotFoundError(RuntimeError):
    """Raised when a PO number produces 0 matching orders in SupplyPro Order Search."""


class SupplyProSessionExpiredError(RuntimeError):
    """Raised when SupplyPro redirects an active session back to login.asp."""

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
                for field_name in ("builderName", "BuilderName", "propertyAddress", "PropertyAddress", "inspectionType", "InspectionType", "fee", "Fee", "inspectionId", "InspectionID"):
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

    return merged


class SupplyProParameters(BaseModel):
    """Validated job parameters for the SupplyPro PortalSync template."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    target_portal: str = Field(default="SupplyPro", alias="targetPortal")
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
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def add_usage(self, prompt_tokens: int, candidate_tokens: int, total_tokens: int = 0) -> None:
        self.input_tokens += int(prompt_tokens or 0)
        self.output_tokens += int(candidate_tokens or 0)
        computed_total = int(total_tokens) if total_tokens else (int(prompt_tokens or 0) + int(candidate_tokens or 0))
        self.total_tokens += computed_total
        self.estimated_cost_usd = round(
            (self.input_tokens * GEMINI_25_FLASH_INPUT_COST_PER_1M / 1_000_000)
            + (self.output_tokens * GEMINI_25_FLASH_OUTPUT_COST_PER_1M / 1_000_000),
            6,
        )


class SkillExecutionResult(BaseModel):
    """Standardized return payload from any Burgess Buddy skill execution."""

    status: str = "Completed"
    records_processed: int = 0
    records_failed: int = 0
    output_summary: str = ""
    error_message: Optional[str] = None
    details: list[dict[str, Any]] = Field(default_factory=list)
    telemetry: TokenTelemetry = Field(default_factory=TokenTelemetry)


def normalize_pending_order_record(raw_order: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizes a pending order record returned by `GET /api/accounting/supplypro/pending`
    into the standard `expected` dictionary structure required by the 4-factor auditor.
    """
    po_num = str(
        _get_case_insensitive(raw_order, "poNumber", "PONumber", "po", "builderOrderNumber", default="")
    ).strip()
    insp_id_raw = _get_case_insensitive(raw_order, "inspectionId", "InspectionID", "id")
    insp_id = int(insp_id_raw) if insp_id_raw is not None and str(insp_id_raw).isdigit() else None

    inv_raw = _get_case_insensitive(raw_order, "invoiceNumber", "InvoiceNumber", "invoice")
    inv_str = str(inv_raw).strip() if inv_raw not in (None, "") else None

    fee_raw = _get_case_insensitive(raw_order, "fee", "Fee", "expectedAmount", "amount")
    fee_dec = _to_cents(fee_raw)
    fee_val = float(fee_dec) if fee_dec is not None else None

    return {
        "poNumber": po_num,
        "inspectionId": insp_id,
        "invoiceNumber": inv_str,
        "builderName": str(
            _get_case_insensitive(raw_order, "builderName", "BuilderName", "builder", default="")
        ).strip(),
        "propertyAddress": str(
            _get_case_insensitive(raw_order, "propertyAddress", "PropertyAddress", "address", default="")
        ).strip(),
        "inspectionType": str(
            _get_case_insensitive(raw_order, "inspectionType", "InspectionType", "task", default="")
        ).strip(),
        "fee": fee_val,
        "dbRecordFound": True,
    }


def fetch_expected_order_from_db(
    po_number: str,
    fallback_params: Optional[SupplyProParameters] = None,
) -> dict[str, Any]:
    """
    Queries `ai.VW_AccountingExtractRevenue` (or `ai.VW_AccountingExtract_Revenue`)
    for the target PO's expected internal record values when running in single `--po` mode.
    """
    defaults: dict[str, Any] = {
        "poNumber": po_number,
        "builderName": (fallback_params.builder_name if fallback_params else None) or "Unknown (No DB Record Found)",
        "propertyAddress": (fallback_params.property_address if fallback_params else None) or "Unknown (No DB Record Found)",
        "inspectionType": (fallback_params.inspection_type if fallback_params else None) or "Unknown (No DB Record Found)",
        "fee": fallback_params.expected_fee if (fallback_params and fallback_params.expected_fee is not None) else None,
        "invoiceNumber": fallback_params.invoice_number if fallback_params else None,
        "inspectionId": None,
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

    view_candidates = [
        "ai.VW_AccountingExtractRevenue",
        "ai.VW_AccountingExtract_Revenue",
    ]

    try:
        engine = create_engine(connection_url)
        with engine.connect() as conn:
            for view_name in view_candidates:
                try:
                    query = text(
                        f"""
                        SELECT TOP 1
                            InspectionID,
                            InvoiceNumber,
                            BuilderName,
                            PONumber,
                            PropertyAddress,
                            InspectionType,
                            Fee
                        FROM {view_name}
                        WHERE PONumber = :exact_po OR PONumber LIKE :like_po
                        ORDER BY
                            CASE WHEN PONumber = :exact_po THEN 0 ELSE 1 END,
                            InspectionDate DESC
                        """
                    )
                    row = conn.execute(
                        query,
                        {"exact_po": po_number, "like_po": f"%{po_number}%"},
                    ).mappings().first()

                    if row:
                        fee_dec = _to_cents(row.get("Fee"))
                        fee_val = float(fee_dec) if fee_dec is not None else defaults["fee"]
                        inv_val = (
                            str(row["InvoiceNumber"]).strip()
                            if row.get("InvoiceNumber") is not None
                            else defaults["invoiceNumber"]
                        )
                        record = {
                            "poNumber": str(row.get("PONumber") or po_number).strip(),
                            "builderName": str(row.get("BuilderName") or "").strip(),
                            "propertyAddress": str(row.get("PropertyAddress") or "").strip(),
                            "inspectionType": str(row.get("InspectionType") or "").strip(),
                            "fee": fee_val,
                            "invoiceNumber": inv_val,
                            "inspectionId": int(row["InspectionID"]) if row.get("InspectionID") is not None else None,
                            "dbRecordFound": True,
                        }
                        logger.info(
                            "Loaded expected record from %s for PO %s: builder=%r, address=%r, type=%r, fee=%s, invoice=%s",
                            view_name,
                            po_number,
                            record["builderName"],
                            record["propertyAddress"],
                            record["inspectionType"],
                            record["fee"],
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

    def search_and_scrape_order(self, po_number: str) -> dict[str, Any]:
        """
        Navigates to Order Search (`Orders/orderlist.asp`), enters the PO number into
        `input#bldr_order_filter`, clicks `input#cmdSearch`, opens `OrderDetail.asp`,
        and scrapes builder, address, taskDescription, and amount fields.
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

        detail_link_selectors = [
            f"a:has-text('{po_number}')",
            "a[href*='OrderDetail.asp']",
            "a[href*='orderdetail.asp']",
            "a:has-text('Details')",
        ]
        detail_link = self._find_visible_locator(target, detail_link_selectors, timeout=12000)
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

        scraped_payload = {
            "poNumber": po_number,
            "pageUrl": self._page.url,
            "builder": extracted_fields.get("builder", ""),
            "address": extracted_fields.get("address", ""),
            "taskDescription": extracted_fields.get("description", ""),
            "amount": numeric_amount if numeric_amount is not None else amount_str,
            "amountText": amount_str,
            "orderStatus": extracted_fields.get("status", ""),
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
            raise RuntimeError("Could not locate 'Complete Order' action selector on Order Details page.")

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


def _build_audit_system_instruction(prompt_instructions: Optional[str] = None) -> str:
    """
    Combines the base 4-factor reconciliation system instruction with optional
    job-specific promptInstructions under 'Additional Job-Specific Business Rules & Aliases'.
    """
    base = AUDIT_SYSTEM_INSTRUCTION.strip()
    extra = (prompt_instructions or "").strip()
    if not extra:
        return base
    return (
        f"{base}\n\n"
        f"Additional Job-Specific Business Rules & Aliases:\n"
        f"{extra}\n"
        f"(Note: The 4-factor JSON output schema and the exact $0.00 fee match rule remain mandatory.)"
    )


def audit_order_with_gemini(
    expected: dict[str, Any],
    scraped: dict[str, Any],
    po_number: Optional[str] = None,
    telemetry: Optional[TokenTelemetry] = None,
    prompt_instructions: Optional[str] = None,
    user_note: Optional[str] = None,
) -> SupplyProAuditResult:
    """
    Performs a 4-factor semantic comparison between the internal Burgess record (`expected`)
    and the scraped SupplyPro order (`scraped`) using Gemini 2.5 Flash, while accumulating
    token usage and cost telemetry.
    """
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

1. EXPECTED (Internal Burgess Database Record - `ai.VW_AccountingExtractRevenue`):
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

    system_instruction = _build_audit_system_instruction(prompt_instructions)

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=SupplyProAuditResult,
            temperature=0.0,
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage is not None and telemetry is not None:
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        total_tokens = int(getattr(usage, "total_token_count", 0) or (prompt_tokens + candidate_tokens))
        telemetry.add_usage(prompt_tokens, candidate_tokens, total_tokens)
        logger.info(
            "Gemini 3.5 Flash usage for PO %s: input=%d, output=%d, total=%d (cumulative cost=$%.6f)",
            po_label,
            prompt_tokens,
            candidate_tokens,
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
                records_failed=0,
                output_summary=empty_summary,
                details=[],
                telemetry=TokenTelemetry(),
            )

        driver = SupplyProPortalDriver(params=parsed_params, dry_run=dry_run)
        telemetry = TokenTelemetry()
        details_list: list[dict[str, Any]] = []
        processed_count = 0
        failed_count = 0
        order_summaries: list[str] = []

        try:
            driver.setup_browser()
            driver.login()
            progress_logger.info("Logged into SupplyPro")

            for idx, expected_record in enumerate(orders_to_process, start=1):
                target_po = str(expected_record.get("poNumber") or "").strip()
                if not target_po:
                    continue

                invoice_num = (
                    expected_record.get("invoiceNumber")
                    or active_invoice_number
                    or ""
                )
                inspection_id = expected_record.get("inspectionId")
                expected_fee = expected_record.get("fee")
                item_user_note = expected_record.get("userNote") or active_user_note
                item_extract_type = expected_record.get("extractType") or parsed_params.extract_type

                # Check before navigating whether SupplyPro redirected back to login.asp
                if hasattr(driver, "ensure_authenticated") and callable(driver.ensure_authenticated):
                    driver.ensure_authenticated()

                try:
                    try:
                        scraped_data = driver.search_and_scrape_order(target_po)
                        if hasattr(driver, "is_session_expired") and callable(driver.is_session_expired) and driver.is_session_expired():
                            raise SupplyProSessionExpiredError(
                                f"SupplyPro redirected to login screen after scraping PO {target_po}."
                            )
                    except Exception as first_scrape_err:
                        if not _is_not_found_error(first_scrape_err) and _is_session_redirect_error(first_scrape_err, driver):
                            logger.warning(
                                "SupplyPro session expired on PO %s (%s); re-authenticating and retrying once...",
                                target_po,
                                first_scrape_err,
                            )
                            driver.login()
                            progress_logger.info("Re-authenticated into SupplyPro after session timeout")
                            scraped_data = driver.search_and_scrape_order(target_po)
                        else:
                            raise

                    audit_kwargs: dict[str, Any] = {
                        "expected": expected_record,
                        "scraped": scraped_data,
                        "po_number": target_po,
                        "telemetry": telemetry,
                        "prompt_instructions": parsed_params.prompt_instructions,
                    }
                    if item_user_note:
                        audit_kwargs["user_note"] = item_user_note

                    audit_verdict = audit_order_with_gemini(**audit_kwargs)
                except Exception as po_err:
                    if _is_not_found_error(po_err):
                        not_found_note = "PO not found in SupplyPro portal"
                        logger.warning("PO %s not found in SupplyPro portal: %s", target_po, po_err)
                        failed_count += 1
                        row_status = "NotFound (Dry-Run)" if dry_run else "NotFound"
                        if not dry_run:
                            try:
                                client.log_supplypro_reconciliation({
                                    "executionId": active_execution_id,
                                    "inspectionId": inspection_id,
                                    "extractType": item_extract_type,
                                    "poNumber": target_po,
                                    "invoiceNumber": str(invoice_num) if invoice_num else None,
                                    "status": "NotFound",
                                    "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                                    "scrapedAmount": None,
                                    "auditNotes": not_found_note,
                                })
                            except Exception as log_err:
                                logger.error("Failed to log NotFound status for PO %s: %s", target_po, log_err)

                        details_list.append({
                            "PONumber": target_po,
                            "InvoiceNumber": str(invoice_num),
                            "BuilderExpected": expected_record.get("builderName") or "",
                            "BuilderScraped": "",
                            "InspectionExpected": expected_record.get("inspectionType") or "",
                            "TaskScraped": "",
                            "FeeExpected": expected_fee,
                            "FeeScraped": None,
                            "Status": row_status,
                            "ConfidenceScore": 0.0,
                            "Discrepancies": [not_found_note],
                            "AuditNotes": not_found_note,
                            "overallMatch": False,
                        })
                        order_summaries.append(f"PO {target_po}: NOT FOUND ({not_found_note})")
                        progress_logger.info(
                            "[%d/%d] PO %s -> NotFound (%s)",
                            idx,
                            total_orders,
                            target_po,
                            not_found_note,
                        )
                        continue

                    if single_order_mode:
                        raise
                    logger.error("Error processing PO %s in batch: %s", target_po, po_err)
                    failed_count += 1
                    err_note = f"Portal scrape/audit error: {type(po_err).__name__}: {po_err}"
                    if not dry_run:
                        try:
                            client.log_supplypro_reconciliation({
                                "executionId": active_execution_id,
                                "inspectionId": inspection_id,
                                "extractType": item_extract_type,
                                "poNumber": target_po,
                                "invoiceNumber": str(invoice_num) if invoice_num else None,
                                "status": "FlaggedForReview",
                                "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                                "scrapedAmount": None,
                                "auditNotes": err_note,
                            })
                        except Exception as log_err:
                            logger.error("Failed to log reconciliation error for PO %s: %s", target_po, log_err)

                    details_list.append({
                        "PONumber": target_po,
                        "InvoiceNumber": str(invoice_num),
                        "BuilderExpected": expected_record.get("builderName") or "",
                        "BuilderScraped": "",
                        "InspectionExpected": expected_record.get("inspectionType") or "",
                        "TaskScraped": "",
                        "FeeExpected": expected_fee,
                        "FeeScraped": None,
                        "Status": "FlaggedForReview",
                        "ConfidenceScore": 0.0,
                        "Discrepancies": [err_note],
                        "AuditNotes": err_note,
                        "overallMatch": False,
                    })
                    order_summaries.append(f"PO {target_po}: ERROR ({po_err})")
                    progress_logger.info(
                        "[%d/%d] PO %s -> FlaggedForReview (%s)",
                        idx,
                        total_orders,
                        target_po,
                        err_note,
                    )
                    continue

                _print_reconciliation_report(
                    po_number=target_po,
                    expected=expected_record,
                    scraped=scraped_data,
                    audit_verdict=audit_verdict,
                    telemetry=telemetry,
                    dry_run=dry_run,
                )

                scraped_amt_dec = _to_cents(scraped_data.get("amount"))
                scraped_amt_float = float(scraped_amt_dec) if scraped_amt_dec is not None else None

                if audit_verdict.overallMatch:
                    reconcile_status = "Reconciled"
                    row_status = "Reconciled (Dry-Run)" if dry_run else "Reconciled"
                    portal_action_result = "DryRun"
                    if not dry_run:
                        portal_action_result = driver.complete_order_on_portal(
                            invoice_number=str(invoice_num or target_po)
                        )
                    processed_count += 1
                    fee_display = (
                        f"${scraped_amt_float:.2f}"
                        if scraped_amt_float is not None
                        else (f"${float(expected_fee):.2f}" if expected_fee is not None else "N/A")
                    )
                    if portal_action_result == "Already Complete":
                        row_status = "Already Complete"
                        if "already complete" not in (audit_verdict.auditNotes or "").lower():
                            audit_verdict.auditNotes = (
                                f"Already Complete | {audit_verdict.auditNotes}"
                                if audit_verdict.auditNotes
                                else "Already Complete"
                            )
                        order_summaries.append(
                            f"PO {target_po}: ALREADY COMPLETE (Invoice={invoice_num or 'N/A'}, Confidence={audit_verdict.confidenceScore:.2f})"
                        )
                        progress_logger.info(
                            "[%d/%d] PO %s -> Already Complete (%s)",
                            idx,
                            total_orders,
                            target_po,
                            fee_display,
                        )
                    else:
                        order_summaries.append(
                            f"PO {target_po}: MATCH (Invoice={invoice_num or 'N/A'}, Confidence={audit_verdict.confidenceScore:.2f})"
                        )
                        progress_logger.info(
                            "[%d/%d] PO %s -> Reconciled (%s)",
                            idx,
                            total_orders,
                            target_po,
                            fee_display,
                        )
                else:
                    reconcile_status = "FlaggedForReview"
                    row_status = "FlaggedForReview (Dry-Run)" if dry_run else "FlaggedForReview"
                    failed_count += 1
                    discrepancies_str = "; ".join(audit_verdict.discrepancies) or "See auditNotes"
                    logger.warning(
                        "Execution gate blocked SupplyPro modification for PO %s: %s",
                        target_po,
                        discrepancies_str,
                    )
                    order_summaries.append(
                        f"PO {target_po}: FLAGGED ({discrepancies_str})"
                    )
                    progress_logger.info(
                        "[%d/%d] PO %s -> FlaggedForReview (%s)",
                        idx,
                        total_orders,
                        target_po,
                        discrepancies_str,
                    )

                if not dry_run:
                    notes_with_discrepancies = audit_verdict.auditNotes
                    if audit_verdict.discrepancies:
                        disc_joined = "; ".join(audit_verdict.discrepancies)
                        notes_with_discrepancies = f"Discrepancies: {disc_joined} | {audit_verdict.auditNotes}"

                    client.log_supplypro_reconciliation({
                        "executionId": active_execution_id,
                        "inspectionId": inspection_id,
                        "extractType": item_extract_type,
                        "poNumber": target_po,
                        "invoiceNumber": str(invoice_num) if invoice_num else None,
                        "status": reconcile_status,
                        "expectedAmount": float(expected_fee) if expected_fee is not None else None,
                        "scrapedAmount": scraped_amt_float,
                        "auditNotes": notes_with_discrepancies,
                    })

                row_detail = {
                    "PONumber": target_po,
                    "InvoiceNumber": str(invoice_num),
                    "BuilderExpected": expected_record.get("builderName") or "",
                    "BuilderScraped": scraped_data.get("builder") or "",
                    "InspectionExpected": expected_record.get("inspectionType") or "",
                    "TaskScraped": scraped_data.get("taskDescription") or "",
                    "FeeExpected": expected_fee,
                    "FeeScraped": scraped_amt_float if scraped_amt_float is not None else scraped_data.get("amount"),
                    "Status": row_status,
                    "ConfidenceScore": audit_verdict.confidenceScore,
                    "Discrepancies": audit_verdict.discrepancies,
                    "AuditNotes": audit_verdict.auditNotes,
                    **audit_verdict.model_dump(),
                }
                details_list.append(row_detail)

            telemetry_summary = (
                f"Tokens: {telemetry.input_tokens:,} in / {telemetry.output_tokens:,} out "
                f"({telemetry.total_tokens:,} total) | Est. Cost: ${telemetry.estimated_cost_usd:.6f} USD"
            )
            summary = (
                f"{mode_prefix}SupplyPro Reconciliation ({processed_count} reconciled, {failed_count} flagged) | "
                f"{' ; '.join(order_summaries)} | {telemetry_summary}"
            )
            logger.info(summary)

            return SkillExecutionResult(
                status="Completed",
                records_processed=processed_count,
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
