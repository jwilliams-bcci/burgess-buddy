"""
Token usage and USD cost tracker for Gemini API calls in Burgess Buddy.
"""

from typing import Any, Optional

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"

# Per-1M-token pricing in USD
MODEL_PRICING_PER_1M: dict[str, dict[str, float]] = {
    "gemini-3.5-flash-lite": {
        "input": 0.30,
        "cached_input": 0.03,
        "output": 2.50,
    },
    "gemini-3.8-flash": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 3.75,
    },
    "gemini-3.6-flash": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 3.75,
    },
    "gemini-4-hydrogen": {
        "input": 1.50,
        "cached_input": 0.075,
        "output": 7.50,
    },
}


class CostTracker:
    """
    Tracks cumulative Gemini token usage (`total_input_tokens`, `total_cached_tokens`,
    `total_output_tokens`) and estimated USD cost (`total_cost`) across an execution run.
    """

    def __init__(self, model_name: Optional[str] = None) -> None:
        cleaned = (model_name or "").strip()
        self.model_name: str = cleaned if cleaned else DEFAULT_GEMINI_MODEL
        self.total_input_tokens: int = 0
        self.total_cached_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cost: float = 0.0

    @classmethod
    def get_pricing_for_model(cls, model_name: Optional[str]) -> dict[str, float]:
        """Returns the pricing rate dictionary for `model_name`, falling back to `gemini-3.5-flash-lite`."""
        key = (model_name or "").strip().lower()
        return MODEL_PRICING_PER_1M.get(key, MODEL_PRICING_PER_1M[DEFAULT_GEMINI_MODEL])

    def record_usage(self, response: Any, model_name: Optional[str] = None) -> float:
        """
        Reads `response.usage_metadata` and accumulates token counts and USD cost:
          - `prompt_token_count` -> `total_input_tokens`
          - `cached_content_token_count` -> `total_cached_tokens`
          - `candidates_token_count + thoughts_token_count` -> `total_output_tokens`
          - Uncached input = `max(0, prompt_tokens - cached_tokens)`

        Returns the USD cost of this individual call.
        """
        usage = getattr(response, "usage_metadata", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage_metadata")
        if usage is None:
            return 0.0

        def _read_int(field_name: str) -> int:
            val = (
                usage.get(field_name, 0)
                if isinstance(usage, dict)
                else getattr(usage, field_name, 0)
            )
            try:
                return int(val or 0)
            except (TypeError, ValueError):
                return 0

        prompt_tokens = _read_int("prompt_token_count")
        cached_tokens = _read_int("cached_content_token_count")
        candidates_tokens = _read_int("candidates_token_count")
        thoughts_tokens = _read_int("thoughts_token_count")

        uncached_input_tokens = max(0, prompt_tokens - cached_tokens)
        output_tokens = candidates_tokens + thoughts_tokens

        rates = self.get_pricing_for_model(model_name or self.model_name)
        call_cost = (
            (uncached_input_tokens * rates["input"])
            + (cached_tokens * rates["cached_input"])
            + (output_tokens * rates["output"])
        ) / 1_000_000.0

        self.total_input_tokens += prompt_tokens
        self.total_cached_tokens += cached_tokens
        self.total_output_tokens += output_tokens
        self.total_cost += call_cost
        return call_cost

    def to_telemetry_dict(self) -> dict[str, Any]:
        """Returns a dictionary representation compatible with Emailer and telemetry summaries."""
        return {
            "model_name": self.model_name,
            "input_tokens": self.total_input_tokens,
            "cached_tokens": self.total_cached_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "estimated_cost_usd": round(self.total_cost, 6),
        }
