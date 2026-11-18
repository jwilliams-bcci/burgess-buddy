"""
CLI Worker Runtime Entry Point for the Burgess Buddy AI Agent Platform.

Execution Lifecycle:
  1. Fetch job configuration from BurgessApi and enforce governance checks
     (isActive == True and approvalStatus == 'Active').
  2. Trigger an execution record on BurgessApi to obtain executionId.
  3. Parse job parameters and allowedSkills (injecting --po and local browser config if provided).
  4. Dynamically resolve and invoke the matching skill driver.
  5. If 'Emailer' is in allowedSkills (or recipientEmail is configured), generate the
     in-memory CSV reconciliation report and deliver/preview via skills/emailer.py.
  6. Guarantee the completion callback is invoked ('Completed' or 'Failed').
"""

import argparse
from datetime import datetime
import importlib
import inspect
import json
import logging
import os
import re
import sys
import threading
from typing import Any, Callable, Optional

from dotenv import load_dotenv

from core.api_client import BurgessApiClient, BurgessApiError
from skills.emailer import send_reconciliation_report
from skills.portal_supplypro import SkillExecutionResult, run_supplypro_sync

load_dotenv(override=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("burgess_buddy.runner")
progress_logger = logging.getLogger("burgess_buddy.progress")


class BufferedExecutionLogHandler(logging.Handler):
    """
    Lightweight buffered log handler that collects clean, timestamped progress
    milestones (`[HH:MM:SS] ...`) without raw HTML or full stack traces, flushes
    buffered lines to `client.append_execution_log(execution_id, chunk)` every
    `flush_interval` seconds via a background daemon thread, and guarantees a
    final flush on shutdown.
    """

    def __init__(
        self,
        api_client: BurgessApiClient,
        execution_id: int,
        flush_interval: float = 2.0,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.api_client = api_client
        self.execution_id = int(execution_id)
        self.flush_interval = float(flush_interval)
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._flush_loop,
            name=f"exec-log-streamer-{self.execution_id}",
            daemon=True,
        )
        self._worker.start()

    @staticmethod
    def _sanitize_milestone(raw_msg: str) -> str:
        """Strips raw HTML tags, collapses multi-line content/stack traces to a clean single line."""
        first_line = raw_msg.strip().splitlines()[0] if raw_msg and raw_msg.strip() else ""
        cleaned = re.sub(r"<[^>]+>", "", first_line).strip()
        if len(cleaned) > 500:
            cleaned = cleaned[:497] + "..."
        return cleaned

    def emit(self, record: logging.LogRecord) -> None:
        try:
            raw_message = record.getMessage()
            clean_msg = self._sanitize_milestone(raw_message)
            if not clean_msg:
                return
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            if clean_msg.startswith("[") and len(clean_msg) >= 10 and clean_msg[9] == "]":
                line = clean_msg
            else:
                line = f"[{ts}] {clean_msg}"
            with self._lock:
                self._buffer.append(line)
        except Exception:
            pass

    def flush_to_api(self) -> None:
        """Flushes any buffered milestone lines to BurgessApi in a single chunk."""
        with self._lock:
            if not self._buffer:
                return
            chunk = "\n".join(self._buffer) + "\n"
            self._buffer.clear()
        try:
            self.api_client.append_execution_log(self.execution_id, chunk)
        except Exception:
            pass

    def _flush_loop(self) -> None:
        while not self._stop_event.wait(timeout=self.flush_interval):
            self.flush_to_api()

    def stop_and_flush(self) -> None:
        """Stops the periodic timer thread and performs a guaranteed final flush."""
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)
        self.flush_to_api()

SkillHandler = Callable[..., SkillExecutionResult]

# Registry mapping (templateType.lower(), targetPortal.lower()) -> skill handler
SKILL_REGISTRY: dict[tuple[str, str], SkillHandler] = {
    ("portalsync", "supplypro"): run_supplypro_sync,
}

# Fallback registry by skill identifier / portal name
NAMED_SKILL_REGISTRY: dict[str, SkillHandler] = {
    "portal_supplypro": run_supplypro_sync,
    "supplypro": run_supplypro_sync,
}


class GovernanceError(PermissionError):
    """Raised when a job fails governance validation prior to execution."""


def _get_field(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Case-tolerant lookup helper supporting camelCase, PascalCase, and snake_case keys."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    lower_map = {k.lower(): v for k, v in data.items()}
    for key in keys:
        if key.lower() in lower_map and lower_map[key.lower()] is not None:
            return lower_map[key.lower()]
    return default


def parse_parameters(raw_params: Any) -> dict[str, Any]:
    """
    Safely normalizes job or run parameters (`ParametersJson` / `RunParametersJson`)
    from a JSON string or dict into a dictionary.
    Returns `{}` for `None`, empty strings `""`, empty JSON objects `"{}"`, or invalid
    inputs without raising `TypeError` or `JSONDecodeError`.
    """
    if raw_params is None:
        return {}
    if isinstance(raw_params, dict):
        return dict(raw_params)
    if isinstance(raw_params, str):
        text = raw_params.strip()
        if not text or text.lower() == "null":
            return {}
        try:
            parsed = json.loads(text)
            if parsed is None:
                return {}
            if isinstance(parsed, dict):
                return dict(parsed)
            return {"value": parsed}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse parameters JSON (%s); falling back to {}: %r", exc, raw_params)
            return {}
    logger.warning("Unsupported parameters type %s; falling back to {}.", type(raw_params).__name__)
    return {}


def _extract_run_parameters(execution_record: Optional[dict[str, Any]]) -> dict[str, Any]:
    """
    Extracts and deserializes `RunParametersJson` / `runParameters` from an execution
    record, safely returning `{}` when `None`, `""`, or `{}`.
    """
    if not isinstance(execution_record, dict):
        return {}
    for key in (
        "runParametersJson",
        "RunParametersJson",
        "run_parameters_json",
        "runParameters",
        "run_parameters",
        "parametersJson",
        "parameters",
    ):
        raw_val = _get_field(execution_record, key)
        parsed = parse_parameters(raw_val)
        if parsed:
            return parsed
    return {}


def _parse_optional_bool(value: Any) -> Optional[bool]:
    """Parses a boolean or truthy/falsy string (`dryRun` / `dry_run`) into an optional bool."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in ("true", "1", "yes", "on"):
            return True
        if norm in ("false", "0", "no", "off"):
            return False
    return None


def parse_allowed_skills(raw_skills: Any) -> list[str]:
    """Normalizes allowedSkills from a JSON array string, CSV string, or list."""
    if not raw_skills:
        return []
    if isinstance(raw_skills, list):
        return [
            str(s.get("name") or s.get("skillName") or s.get("code") or s).strip()
            if isinstance(s, dict)
            else str(s).strip()
            for s in raw_skills
            if s
        ]
    if isinstance(raw_skills, str):
        text = raw_skills.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(s).strip() for s in parsed if s]
            except json.JSONDecodeError:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    return [str(raw_skills).strip()]


def verify_job_governance(job: dict[str, Any], job_id: int) -> None:
    """Verifies that the job is active and approved for execution."""
    is_active = _get_field(job, "isActive", "IsActive", "is_active", default=False)
    if isinstance(is_active, str):
        is_active = is_active.strip().lower() in ("true", "1", "yes")

    approval_status = str(
        _get_field(job, "approvalStatus", "ApprovalStatus", "approval_status", default="")
    ).strip()

    if is_active is not True or approval_status.lower() != "active":
        raise GovernanceError(
            f"Job {job_id} blocked by governance policy: "
            f"isActive={is_active!r} (required True), "
            f"approvalStatus={approval_status!r} (required 'Active')."
        )


def resolve_skill_handler(
    job: dict[str, Any],
    parameters: dict[str, Any],
    allowed_skills: list[str],
) -> SkillHandler:
    """
    Dynamically resolves the skill handler function based on templateType,
    targetPortal, and allowedSkills (case-insensitive).
    """
    # Unpack nested skill parameters (e.g., parameters["SiteLogin"]) if present
    site_params = _get_field(parameters, "SiteLogin", "siteLogin", "site_login")
    if not isinstance(site_params, dict):
        site_params = {}

    template_type = str(
        _get_field(job, "templateType", "TemplateType", "template_type")
        or _get_field(parameters, "templateType", "TemplateType", "template_type")
        or _get_field(site_params, "templateType", "TemplateType", "template_type")
        or ""
    ).strip()

    target_portal = str(
        _get_field(site_params, "targetPortal", "target_portal", "TargetPortal", "portal")
        or _get_field(parameters, "targetPortal", "target_portal", "TargetPortal", "portal")
        or _get_field(job, "targetPortal", "target_portal", "TargetPortal")
        or ""
    ).strip()

    if not target_portal and site_params:
        target_portal = "SupplyPro"

    template_lower = template_type.lower()
    portal_lower = target_portal.lower()
    skills_lower = [s.strip().lower() for s in allowed_skills if s]

    logger.info(
        "Resolving skill handler (templateType=%r, targetPortal=%r, allowedSkills=%s)",
        template_type,
        target_portal,
        allowed_skills,
    )

    # 1. PortalSync routing for SupplyPro or SiteLogin skill
    if template_lower == "portalsync" and (
        portal_lower == "supplypro" or "sitelogin" in skills_lower
    ):
        return run_supplypro_sync

    # 2. Exact match on (templateType.lower(), targetPortal.lower())
    key = (template_lower, portal_lower)
    if key in SKILL_REGISTRY:
        return SKILL_REGISTRY[key]

    # 3. Match by targetPortal alone
    if portal_lower in NAMED_SKILL_REGISTRY:
        return NAMED_SKILL_REGISTRY[portal_lower]

    # 4. Match by allowedSkills entries or dynamic import under skills.<skill_name>
    for skill_lower in skills_lower:
        if skill_lower in ("emailer", "email"):
            continue
        normalized = skill_lower.replace("-", "_").replace(".", "_")
        if normalized in NAMED_SKILL_REGISTRY:
            return NAMED_SKILL_REGISTRY[normalized]

        module_candidates = [normalized, f"portal_{normalized}"]
        for mod_name in module_candidates:
            try:
                module = importlib.import_module(f"skills.{mod_name}")
                for attr_name in ("run", f"run_{normalized}_sync", "execute"):
                    if hasattr(module, attr_name):
                        return getattr(module, attr_name)
            except ModuleNotFoundError:
                continue

    raise ValueError(
        f"No skill handler registered for templateType={template_type!r}, "
        f"targetPortal={target_portal!r}, allowedSkills={allowed_skills!r}."
    )


def _invoke_skill_handler(
    handler: SkillHandler,
    parameters: dict[str, Any],
    allowed_skills: list[str],
    dry_run: bool,
    po: Optional[str] = None,
    invoice_number: Optional[str] = None,
    user_note: Optional[str] = None,
    prompt_instructions: Optional[str] = None,
    execution_id: Optional[int] = None,
    posted_date: Optional[str] = None,
    api_client: Optional[BurgessApiClient] = None,
) -> SkillExecutionResult:
    """
    Invokes the resolved skill handler, passing optional context arguments
    (`po`, `invoice_number`, `user_note`, `prompt_instructions`, `execution_id`,
    `posted_date`, `api_client`) when accepted by the handler's function signature.
    """
    sig = inspect.signature(handler)
    kwargs: dict[str, Any] = {}
    optional_args = {
        "po": po,
        "invoice_number": invoice_number,
        "user_note": user_note,
        "prompt_instructions": prompt_instructions,
        "execution_id": execution_id,
        "posted_date": posted_date,
        "api_client": api_client,
    }
    for param_name, param_val in optional_args.items():
        if param_name in sig.parameters:
            kwargs[param_name] = param_val
    if kwargs:
        return handler(parameters, allowed_skills, dry_run=dry_run, **kwargs)
    return handler(parameters, allowed_skills, dry_run)


def _should_trigger_emailer(parameters: dict[str, Any], allowed_skills: list[str]) -> bool:
    """Returns True when 'Emailer' is in allowedSkills, nested parameters['Emailer'] is present, or recipientEmail is configured."""
    skills_lower = {s.strip().lower() for s in allowed_skills if s}
    if "emailer" in skills_lower or "email" in skills_lower:
        return True
    email_params = _get_field(parameters, "Emailer", "emailer")
    if isinstance(email_params, dict) and email_params:
        return True
    recipient = (
        (
            _get_field(email_params, "recipientEmail", "recipient_email", "email")
            if isinstance(email_params, dict)
            else None
        )
        or _get_field(parameters, "recipientEmail", "recipient_email", "email")
        or os.getenv("REPORT_RECIPIENT_EMAIL", "").strip()
    )
    return bool(recipient)


def _clean_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def execute_job(
    job_id: int,
    user_id: int,
    dry_run: bool = False,
    po: Optional[str] = None,
    posted_date: Optional[str] = None,
    execution_id: Optional[int] = None,
    invoice_number: Optional[str] = None,
    user_note: Optional[str] = None,
    headless: bool = False,
    slow_mo: int = 300,
    api_client: Optional[BurgessApiClient] = None,
    log_flush_interval: float = 2.0,
) -> int:
    """
    Executes the full lifecycle for an agent job and returns an OS exit code (0 on success, 1 on failure).
    """
    owns_client = api_client is None
    client = api_client or BurgessApiClient()
    active_execution_id: Optional[int] = int(execution_id) if execution_id is not None else None
    log_streamer: Optional[BufferedExecutionLogHandler] = None

    try:
        # Step a: Fetch job details and verify governance state
        job = client.get_job(job_id)
        job_name = _get_field(job, "name", "jobName", "title", default=f"Job #{job_id}")
        logger.info("Loaded job %s (%s)", job_id, job_name)

        verify_job_governance(job, job_id)
        logger.info("Governance check passed (isActive=True, approvalStatus='Active').")

        # Step b: Resolve execution context (existing --execution-id or create a new run)
        run_params: dict[str, Any] = {}
        trigger_type = "Manual"
        if active_execution_id is not None:
            execution_record = client.get_execution(active_execution_id)
            run_params = _extract_run_parameters(execution_record)
            raw_trigger = _get_field(execution_record, "triggerType", "TriggerType", "trigger_type")
            if raw_trigger and str(raw_trigger).strip().lower() == "scheduled":
                trigger_type = "Scheduled"
            logger.info(
                "Attached to existing executionId=%s (triggerType=%s, runParameters=%s)",
                active_execution_id,
                trigger_type,
                run_params,
            )
        else:
            active_execution_id = client.trigger_run(job_id=job_id, user_id=user_id)
            logger.info(
                "Execution started with executionId=%s (triggerType=%s)",
                active_execution_id,
                trigger_type,
            )

        # Step c & d: Parse parameters/skills and execute skill handler inside protected block
        try:
            raw_params = _get_field(
                job,
                "parameters",
                "parametersJson",
                "ParametersJson",
                "parameters_json",
                "configJson",
            )
            parameters = parse_parameters(raw_params)
            site_params = _get_field(parameters, "SiteLogin", "siteLogin", "site_login")
            if not isinstance(site_params, dict):
                site_params = {}

            # Resolve runtime filters in priority order: CLI flag -> run_params -> ParametersJson / None
            # Supports both camelCase and snake_case keys (postedDate/posted_date, poNumber/po_number, dryRun/dry_run)
            resolved_posted_date = (
                _clean_optional_str(posted_date)
                or _clean_optional_str(_get_field(run_params, "postedDate", "posted_date", "PostedDate"))
                or _clean_optional_str(_get_field(site_params, "postedDate", "posted_date", "PostedDate"))
                or _clean_optional_str(_get_field(parameters, "postedDate", "posted_date", "PostedDate"))
            )
            resolved_invoice_number = (
                _clean_optional_str(invoice_number)
                or _clean_optional_str(_get_field(run_params, "invoiceNumber", "invoice_number", "invoice", "InvoiceNumber"))
                or _clean_optional_str(_get_field(site_params, "invoiceNumber", "invoice_number", "invoice", "InvoiceNumber"))
                or _clean_optional_str(_get_field(parameters, "invoiceNumber", "invoice_number", "invoice", "InvoiceNumber"))
            )
            resolved_po_number = (
                _clean_optional_str(po)
                or _clean_optional_str(_get_field(run_params, "poNumber", "po_number", "po", "PONumber"))
                or _clean_optional_str(_get_field(site_params, "poNumber", "po_number", "po", "PONumber"))
                or _clean_optional_str(_get_field(parameters, "poNumber", "po_number", "po", "PONumber"))
            )
            resolved_user_note = (
                _clean_optional_str(user_note)
                or _clean_optional_str(_get_field(run_params, "userNote", "user_note", "UserNote"))
                or _clean_optional_str(_get_field(site_params, "userNote", "user_note", "UserNote"))
                or _clean_optional_str(_get_field(parameters, "userNote", "user_note", "UserNote"))
            )

            run_param_dry_run = _parse_optional_bool(_get_field(run_params, "dryRun", "dry_run", "DryRun"))
            site_param_dry_run = _parse_optional_bool(_get_field(site_params, "dryRun", "dry_run", "DryRun"))
            job_param_dry_run = _parse_optional_bool(_get_field(parameters, "dryRun", "dry_run", "DryRun"))
            effective_dry_run = bool(
                dry_run
                if dry_run
                else (
                    run_param_dry_run
                    if run_param_dry_run is not None
                    else (
                        site_param_dry_run
                        if site_param_dry_run is not None
                        else (job_param_dry_run if job_param_dry_run is not None else False)
                    )
                )
            )

            # Attach live buffered log streamer when executionId is active and effective_dry_run=False
            if active_execution_id is not None and not effective_dry_run:
                log_streamer = BufferedExecutionLogHandler(
                    api_client=client,
                    execution_id=active_execution_id,
                    flush_interval=log_flush_interval,
                )
                progress_logger.addHandler(log_streamer)

            # Apply local browser automation settings (headless=False, slow_mo=300) unless overridden
            parameters.setdefault("headless", headless)
            parameters.setdefault("slowMo", slow_mo)
            parameters["executionId"] = active_execution_id
            parameters["triggerType"] = trigger_type
            parameters["dryRun"] = effective_dry_run
            if run_params:
                parameters["runParameters"] = dict(run_params)
                raw_extract_type = _clean_optional_str(
                    _get_field(run_params, "extractType", "extract_type", "ExtractType")
                )
                if raw_extract_type:
                    parameters["extractType"] = raw_extract_type
                for list_key, target_key in (
                    (("items", "markedItems", "marked_items", "selectedItems", "selected_items"), "items"),
                    (("poNumbers", "po_numbers", "PONumbers"), "poNumbers"),
                    (("invoiceNumbers", "invoice_numbers", "InvoiceNumbers"), "invoiceNumbers"),
                ):
                    list_val = _get_field(run_params, *list_key)
                    if list_val:
                        parameters[target_key] = list_val
            if resolved_po_number:
                parameters["po"] = resolved_po_number
                parameters["poNumber"] = resolved_po_number
            if resolved_invoice_number:
                parameters["invoiceNumber"] = resolved_invoice_number
            if resolved_posted_date:
                parameters["postedDate"] = resolved_posted_date
            if resolved_user_note:
                parameters["userNote"] = resolved_user_note

            raw_prompt_instructions = _get_field(
                job,
                "promptInstructions",
                "PromptInstructions",
                "prompt_instructions",
            ) or _get_field(
                parameters,
                "promptInstructions",
                "PromptInstructions",
                "prompt_instructions",
            )
            prompt_instructions = _clean_optional_str(raw_prompt_instructions)
            if prompt_instructions:
                parameters.setdefault("promptInstructions", prompt_instructions)

            raw_skills = _get_field(
                job,
                "allowedSkills",
                "allowedSkillsJson",
                "AllowedSkillsJson",
                "allowed_skills_json",
                "skills",
            )
            allowed_skills = parse_allowed_skills(raw_skills)

            handler = resolve_skill_handler(job, parameters, allowed_skills)
            result = _invoke_skill_handler(
                handler,
                parameters=parameters,
                allowed_skills=allowed_skills,
                dry_run=effective_dry_run,
                po=resolved_po_number,
                invoice_number=resolved_invoice_number,
                user_note=resolved_user_note,
                prompt_instructions=prompt_instructions,
                execution_id=active_execution_id,
                posted_date=resolved_posted_date,
                api_client=client,
            )

            # Step d.2: Trigger Emailer skill when 'Emailer' is in allowedSkills or recipientEmail is set
            if _should_trigger_emailer(parameters, allowed_skills):
                if result.details:
                    telemetry_dict = (
                        result.telemetry.model_dump()
                        if hasattr(result, "telemetry") and result.telemetry is not None
                        else {}
                    )
                    send_reconciliation_report(
                        results=result.details,
                        telemetry=telemetry_dict,
                        parameters=parameters,
                        dry_run=effective_dry_run,
                        job_name=str(job_name),
                    )
                else:
                    logger.info(
                        "Skipping Emailer report delivery because 0 pending orders were processed in this batch."
                    )

            progress_logger.info(
                "Completed execution (processed=%d, failed=%d)",
                result.records_processed,
                result.records_failed,
            )
            if log_streamer is not None:
                log_streamer.flush_to_api()

            # Step e (Success): Report completion to BurgessApi
            client.complete_run(
                job_id=job_id,
                execution_id=active_execution_id,
                status=result.status or "Completed",
                processed=result.records_processed,
                failed=result.records_failed,
                summary=result.output_summary,
                error=result.error_message,
            )
            logger.info(
                "Job %s (executionId=%s) finished with status=%s.",
                job_id,
                active_execution_id,
                result.status,
            )
            return 0

        except Exception as exec_err:
            logger.exception(
                "Runtime exception during job_id=%s execution_id=%s: %s",
                job_id,
                active_execution_id,
                exec_err,
            )
            progress_logger.info("Execution failed: %s: %s", type(exec_err).__name__, exec_err)
            if log_streamer is not None:
                log_streamer.flush_to_api()
            # Step e (Failure): Guarantee completion callback is called with status='Failed'
            try:
                is_single_item = bool(
                    po
                    or invoice_number
                    or _get_field(run_params, "poNumber", "po_number", "po", "invoiceNumber", "invoice_number", "invoice")
                )
                client.complete_run(
                    job_id=job_id,
                    execution_id=active_execution_id,
                    status="Failed",
                    processed=0,
                    failed=1 if is_single_item else 0,
                    summary="Execution terminated due to an unhandled runtime error.",
                    error=f"{type(exec_err).__name__}: {exec_err}",
                )
            except Exception as callback_err:
                logger.error(
                    "Failed to send completion callback for execution_id=%s: %s",
                    active_execution_id,
                    callback_err,
                )
            return 1

    except GovernanceError as gov_err:
        logger.error("Governance check failed: %s", gov_err)
        return 1
    except BurgessApiError as api_err:
        logger.error("BurgessApi communication error: %s", api_err)
        return 1
    except Exception as unexpected_err:
        logger.exception("Unexpected worker error before execution start: %s", unexpected_err)
        return 1
    finally:
        if log_streamer is not None:
            try:
                progress_logger.removeHandler(log_streamer)
                log_streamer.stop_and_flush()
            except Exception:
                pass
        if owns_client:
            client.close()


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser for runner.py."""
    default_user_env = (
        os.getenv("BURGESS_DEFAULT_USER_ID")
        or os.getenv("BURGESS_USER_ID")
        or os.getenv("DEFAULT_USER_ID")
        or os.getenv("USER_ID")
    )
    default_user_id = int(default_user_env) if (default_user_env and default_user_env.isdigit()) else None

    is_prod = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "development").strip().lower() == "production"

    parser = argparse.ArgumentParser(
        description="Burgess Buddy Python Worker Runtime",
    )
    parser.add_argument(
        "--job-id",
        type=int,
        required=True,
        help="ID of the agent job in BurgessApi to execute.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run the skill in dry-run mode (scrapes and audits with Gemini without clicking Update).",
    )
    parser.add_argument(
        "--execution-id",
        type=int,
        default=None,
        help="Optional existing execution ID created by BurgessApi (fetches runParameters instead of calling /run).",
    )
    parser.add_argument(
        "--po",
        type=str,
        default=None,
        help="Target single Purchase Order (PO) number to process instead of reading a queue.",
    )
    parser.add_argument(
        "--invoice",
        type=str,
        default=None,
        help="Target single Burgess InvoiceNumber to reprocess (bypasses FlaggedForReview exclusion).",
    )
    parser.add_argument(
        "--user-note",
        type=str,
        default=None,
        help="Optional accountant override note injected as authoritative context into the Gemini audit prompt.",
    )
    parser.add_argument(
        "--posted-date",
        type=str,
        default=None,
        help="Optional AccountingPostedDate filter (YYYY-MM-DD) when fetching pending batch orders.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=default_user_id,
        required=default_user_id is None,
        help="User ID triggering the job execution (defaults to BURGESS_DEFAULT_USER_ID env var).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=is_prod,
        help="Run Playwright Chromium in headless mode (defaults to False locally).",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0 if is_prod else 300,
        help="Playwright slow_mo delay in milliseconds (defaults to 300ms locally).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return execute_job(
        job_id=args.job_id,
        user_id=args.user_id,
        dry_run=args.dry_run,
        po=args.po,
        posted_date=args.posted_date,
        execution_id=args.execution_id,
        invoice_number=args.invoice,
        user_note=args.user_note,
        headless=args.headless,
        slow_mo=args.slow_mo,
    )


if __name__ == "__main__":
    sys.exit(main())
