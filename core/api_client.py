"""
Resilient HTTP client for communicating with the C# BurgessApi backend.
Handles job configuration retrieval, execution creation, and completion reporting.
"""

from datetime import datetime
import json
import logging
import os
import time
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv(override=True)

logger = logging.getLogger(__name__)


class BurgessApiError(Exception):
    """Raised when BurgessApi returns an unexpected HTTP status or payload."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _extract_raw_field(data: dict[str, Any], candidate_keys: tuple[str, ...]) -> Any:
    """Finds the first non-None value matching candidate_keys (exact then case-insensitive)."""
    for key in candidate_keys:
        if key in data and data[key] is not None:
            return data[key]
    lower_lookup = {k.lower(): v for k, v in data.items()}
    for key in candidate_keys:
        val = lower_lookup.get(key.lower())
        if val is not None:
            return val
    return None


def _normalize_parameters(raw_params: Any) -> dict[str, Any]:
    """
    Safely deserializes parameters (`ParametersJson` or `RunParametersJson`) from a
    JSON string or dict into a dictionary. Returns `{}` for `None`, `""`, `"{}"`,
    or malformed inputs without raising `TypeError` or `JSONDecodeError`.
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
            return dict(parsed) if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to deserialize parameters JSON (%s): %r", exc, raw_params)
            return {}
    return {}


def _normalize_allowed_skills(raw_skills: Any) -> list[str]:
    """Deserializes allowedSkills from a JSON string or preserves an existing list."""
    if raw_skills is None:
        return []
    if isinstance(raw_skills, list):
        return [
            str(item.get("name") or item.get("skillName") or item.get("code") or item).strip()
            if isinstance(item, dict)
            else str(item).strip()
            for item in raw_skills
            if item
        ]
    if isinstance(raw_skills, str):
        text = raw_skills.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [
                    str(item.get("name") or item.get("skillName") or item.get("code") or item).strip()
                    if isinstance(item, dict)
                    else str(item).strip()
                    for item in parsed
                    if item
                ]
        except json.JSONDecodeError:
            # Fallback to comma-separated string if not valid JSON array
            return [part.strip() for part in text.split(",") if part.strip()]
    return [str(raw_skills).strip()]


class BurgessApiClient:
    """
    HTTP client for the BurgessApi agent job endpoints:
      - GET  /api/agent/jobs/{jobId}
      - POST /api/agent/jobs/{jobId}/run?userId={int}
      - POST /api/agent/jobs/{jobId}/executions/{executionId}/complete

    Uses INTERNAL_PORTAL_API_URL (or BURGESS_API_BASE_URL as an alias) and automatically
    normalizes trailing '/api' paths so endpoints resolve consistently.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        raw_url = (
            base_url
            or os.getenv("INTERNAL_PORTAL_API_URL")
            or os.getenv("BURGESS_API_BASE_URL")
            or "https://portal2backend.burgess-inc.com/BackEnd/api/"
        ).rstrip("/")

        # Normalize base_url so it always ends with '/api'
        if raw_url.endswith("/api"):
            self.api_base_url = raw_url
        else:
            self.api_base_url = f"{raw_url}/api"

        self.base_url = self.api_base_url

        if verify_ssl is None:
            env_verify = os.getenv("BURGESS_API_VERIFY_SSL", "false").strip().lower()
            self.verify_ssl = env_verify in ("true", "1", "yes")
        else:
            self.verify_ssl = verify_ssl

        self.client_id = os.getenv("INTERNAL_API_CLIENT_ID")
        self.client_secret = os.getenv("INTERNAL_API_CLIENT_SECRET")
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.api_base_url,
            verify=self.verify_ssl,
            timeout=self.timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        """Closes the underlying HTTPX client session."""
        self._client.close()

    def __enter__(self) -> "BurgessApiClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _normalize_endpoint(self, endpoint: str) -> str:
        """
        Strips a leading '/api/' prefix when base_url already ends with '/api'
        so callers can pass either '/api/agent/jobs/1' or 'agent/jobs/1'.
        """
        clean = endpoint.lstrip("/")
        if clean.startswith("api/"):
            clean = clean[len("api/") :]
        return f"/{clean}"

    def _get_valid_token(self) -> Optional[str]:
        """Fetches or reuses a Bearer token from /Account/Login when credentials are provided."""
        if not self.client_id or not self.client_secret:
            return None

        now = time.time()
        if self._token and now < (self._token_expires_at - 60):
            return self._token

        response = self._client.post(
            "/Account/Login",
            json={"email": self.client_id, "password": self.client_secret},
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("isSuccess"):
            raise PermissionError(
                f"BurgessApi authentication failed: {data.get('message', 'Unknown error')}"
            )

        self._token = data.get("token")
        exp_str = data.get("expirationTime")
        if exp_str:
            if exp_str.endswith("Z"):
                exp_str = exp_str[:-1] + "+00:00"
            self._token_expires_at = datetime.fromisoformat(exp_str).timestamp()
        else:
            self._token_expires_at = now + 3600

        return self._token

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Executes an HTTP request with automatic retry on transient transport errors."""
        url = self._normalize_endpoint(endpoint)
        logger.debug("BurgessApi %s %s%s (params=%s)", method, self.api_base_url, url, params)

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            response = self._client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers,
            )
            if response.status_code == 401 and self.client_id and self.client_secret:
                logger.debug("Received 401 from BurgessApi; authenticating via /Account/Login...")
                token = self._get_valid_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    response = self._client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_data,
                        headers=headers,
                    )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body_text = exc.response.text
            logger.error(
                "BurgessApi HTTP %s on %s %s: %s",
                exc.response.status_code,
                method,
                url,
                body_text,
            )
            raise BurgessApiError(
                f"BurgessApi returned HTTP {exc.response.status_code} for {method} {url}: {body_text}",
                status_code=exc.response.status_code,
                response_body=body_text,
            ) from exc
        except httpx.RequestError as exc:
            logger.error("BurgessApi connection error on %s %s: %s", method, url, exc)
            raise

        if not response.content:
            return {}

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()

        text = response.text.strip()
        try:
            return response.json()
        except ValueError:
            return text

    def get_job(self, job_id: int) -> dict[str, Any]:
        """
        Fetches job configuration, parameters (JSON), and allowed skills.
        Endpoint: GET /api/agent/jobs/{jobId}
        """
        logger.info("Fetching job configuration for job_id=%s", job_id)
        data = self._request("GET", f"/api/agent/jobs/{job_id}")
        if not isinstance(data, dict):
            raise BurgessApiError(
                f"Expected JSON object from get_job({job_id}), got: {type(data).__name__}"
            )

        logger.debug("Raw job payload from GET /api/agent/jobs/%s: %s", job_id, data)

        raw_params = _extract_raw_field(
            data,
            ("parameters", "parametersJson", "ParametersJson", "parameters_json"),
        )
        data["parameters"] = _normalize_parameters(raw_params)

        raw_skills = _extract_raw_field(
            data,
            ("allowedSkills", "allowedSkillsJson", "AllowedSkillsJson", "allowed_skills_json"),
        )
        data["allowedSkills"] = _normalize_allowed_skills(raw_skills)

        return data

    def trigger_run(self, job_id: int, user_id: int) -> int:
        """
        Creates an execution record for the specified job and returns executionId.
        Endpoint: POST /api/agent/jobs/{jobId}/run?userId={int}
        """
        logger.info("Triggering run for job_id=%s, user_id=%s", job_id, user_id)
        data = self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/run",
            params={"userId": user_id},
        )

        if isinstance(data, int):
            return data
        if isinstance(data, str) and data.isdigit():
            return int(data)
        if isinstance(data, dict):
            exec_id = (
                data.get("executionId")
                or data.get("ExecutionId")
                or data.get("id")
                or data.get("Id")
            )
            if exec_id is not None:
                return int(exec_id)

        raise BurgessApiError(
            f"Unable to extract executionId from trigger_run response: {data!r}"
        )

    def complete_run(
        self,
        job_id: int,
        execution_id: int,
        status: str,
        processed: int = 0,
        failed: int = 0,
        summary: str = "",
        error: Optional[str] = None,
        estimated_cost: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        exceptions: int = 0,
    ) -> dict[str, Any]:
        """
        Reports execution completion metrics and status back to BurgessApi.
        Endpoint: POST /api/agent/jobs/{jobId}/executions/{executionId}/complete
        Body: {
            status, recordsProcessed, recordsFailed, recordsException,
            outputSummary, errorMessage, estimatedCost, inputTokens, outputTokens
        }
        """
        payload = {
            "status": status,
            "recordsProcessed": int(processed),
            "recordsFailed": int(failed),
            "recordsException": int(exceptions or 0),
            "outputSummary": summary or "",
            "errorMessage": error,
            "estimatedCost": round(float(estimated_cost or 0.0), 6),
            "inputTokens": int(input_tokens or 0),
            "outputTokens": int(output_tokens or 0),
        }
        logger.info(
            "Completing execution_id=%s for job_id=%s with status=%s (processed=%s, exceptions=%s, failed=%s, cost=$%.4f, inputTokens=%s, outputTokens=%s)",
            execution_id,
            job_id,
            status,
            processed,
            payload["recordsException"],
            failed,
            payload["estimatedCost"],
            payload["inputTokens"],
            payload["outputTokens"],
        )
        result = self._request(
            "POST",
            f"/api/agent/jobs/{job_id}/executions/{execution_id}/complete",
            json_data=payload,
        )
        return result if isinstance(result, dict) else {"result": result}

    complete_execution = complete_run

    def get_execution(self, execution_id: int) -> dict[str, Any]:
        """
        Fetches an existing job execution record from BurgessApi.
        Endpoint: GET /api/agent/executions/{execution_id}
        """
        logger.info("Fetching execution details for execution_id=%s", execution_id)
        data = self._request("GET", f"/api/agent/executions/{execution_id}")
        if not isinstance(data, dict):
            raise BurgessApiError(
                f"Expected JSON object from get_execution({execution_id}), got {type(data).__name__}"
            )

        run_params: dict[str, Any] = {}
        for key in (
            "runParametersJson",
            "RunParametersJson",
            "run_parameters_json",
            "runParameters",
            "run_parameters",
        ):
            raw_val = _extract_raw_field(data, (key,))
            parsed_val = _normalize_parameters(raw_val)
            if parsed_val:
                run_params = parsed_val
                break
        data["runParameters"] = run_params

        raw_trigger = _extract_raw_field(data, ("triggerType", "TriggerType", "trigger_type"))
        trigger_str = str(raw_trigger).strip() if raw_trigger else "Manual"
        data["triggerType"] = "Scheduled" if trigger_str.lower() == "scheduled" else "Manual"
        return data

    def append_execution_log(self, execution_id: int, log_chunk: str) -> None:
        """
        Streams a buffered console log chunk to BurgessApi for live UI progress viewing.
        Endpoint: POST /api/agent/executions/{execution_id}/logs
        Body: { "logChunk": log_chunk }
        Fails silently on transient HTTP errors so logging never crashes a batch.
        """
        if not log_chunk:
            return
        url = self._normalize_endpoint(f"/api/agent/executions/{execution_id}/logs")
        try:
            headers: dict[str, str] = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            response = self._client.request(
                method="POST",
                url=url,
                json={"logChunk": log_chunk},
                headers=headers,
            )
            if response.status_code == 401 and self.client_id and self.client_secret:
                self._token = None
                token = self._get_valid_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    self._client.request(
                        method="POST",
                        url=url,
                        json={"logChunk": log_chunk},
                        headers=headers,
                    )
        except Exception as exc:
            logger.debug(
                "Ignored transient error in append_execution_log(execution_id=%s): %s",
                execution_id,
                exc,
            )

    def get_pending_supplypro_orders(
        self,
        extract_type: Optional[str] = "Revenue",
        limit: int = 50,
        posted_date: Optional[str] = None,
        invoice_number: Optional[str] = None,
        po_number: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Fetches pending SupplyPro accounting extract records awaiting portal reconciliation.
        Endpoint: GET /api/accounting/supplypro/pending

        Note: For single-invoice/PO lookups where `invoice_number` or `po_number` is passed
        without `extract_type` (`extract_type=None`), `extractType` is omitted from query
        parameters so `dbo.bspGetAccountingExtract` finds the record regardless of fee sign.
        """
        params: dict[str, Any] = {
            "limit": int(limit),
        }
        if extract_type is not None and str(extract_type).strip():
            params["extractType"] = str(extract_type).strip()
        if posted_date is not None and str(posted_date).strip():
            params["postedDate"] = str(posted_date).strip()
        if invoice_number is not None and str(invoice_number).strip():
            params["invoiceNumber"] = str(invoice_number).strip()
        if po_number is not None and str(po_number).strip():
            params["poNumber"] = str(po_number).strip()

        logger.info(
            "Fetching pending SupplyPro orders (extractType=%s, limit=%s, postedDate=%s, invoiceNumber=%s, poNumber=%s)",
            params.get("extractType"),
            params["limit"],
            params.get("postedDate", "today-CT"),
            params.get("invoiceNumber"),
            params.get("poNumber"),
        )
        data = self._request("GET", "/api/accounting/supplypro/pending", params=params)

        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("items", "orders", "data", "results", "pendingOrders", "records"):
                val = _extract_raw_field(data, (key,))
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
        return []

    def log_supplypro_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Logs an individual SupplyPro PO reconciliation audit result to SQL Server via BurgessApi.
        Endpoint: POST /api/accounting/supplypro/reconcile

        Expected camelCase DTO keys:
          - executionId (int | None)
          - inspectionId (int | None)
          - extractType (str, e.g. "Revenue")
          - poNumber (str)
          - invoiceNumber (str | None)
          - status (str: "Reconciled" or "FlaggedForReview")
          - expectedAmount (float | None)
          - scrapedAmount (float | None)
          - auditNotes (str | None)
        """
        dto = {
            "executionId": (
                int(payload["executionId"])
                if payload.get("executionId") is not None
                else None
            ),
            "inspectionId": (
                int(payload["inspectionId"])
                if payload.get("inspectionId") is not None
                else None
            ),
            "extractType": str(payload.get("extractType") or "Revenue"),
            "poNumber": str(payload.get("poNumber") or ""),
            "invoiceNumber": (
                str(payload["invoiceNumber"]).strip()
                if payload.get("invoiceNumber") not in (None, "")
                else None
            ),
            "status": str(payload.get("status") or "Exception"),
            "expectedAmount": (
                float(payload["expectedAmount"])
                if payload.get("expectedAmount") is not None
                else None
            ),
            "scrapedAmount": (
                float(payload["scrapedAmount"])
                if payload.get("scrapedAmount") is not None
                else None
            ),
            "auditNotes": (
                str(payload["auditNotes"])
                if payload.get("auditNotes") is not None
                else None
            ),
        }
        logger.info(
            "Logging SupplyPro reconciliation for PO %s (status=%s, executionId=%s, inspectionId=%s)",
            dto["poNumber"],
            dto["status"],
            dto["executionId"],
            dto["inspectionId"],
        )
        result = self._request(
            "POST",
            "/api/accounting/supplypro/reconcile",
            json_data=dto,
        )
        return result if isinstance(result, dict) else {"result": result}

