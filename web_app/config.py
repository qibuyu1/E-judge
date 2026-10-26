from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int, minimum: int = 0, maximum: int = 1000000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _as_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _model(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in {"deepseek-v4-flash", "deepseek-v4-pro"} else default


def _depth(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in {"ultra-fast", "fast", "basic", "advanced"} else default


def _extract_depth(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in {"basic", "advanced"} else default


@dataclass(frozen=True)
class Settings:
    app_name: str = "链网E判"
    app_version: str = "9.5.7"
    build_id: str = "20260802.5"
    cache_schema_version: str = "45"
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = _as_int("APP_PORT", 8000, 1, 65535)

    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_api_key_backup: str = os.getenv("DEEPSEEK_API_KEY_BACKUP", "").strip()
    # Evidence extraction is deliberately pinned to Flash. Old .env files that set
    # DEEPSEEK_FAST_MODEL=deepseek-v4-pro must not silently turn every extraction
    # lane into an expensive Pro call. Pro is reserved for the compact decision pass.
    fast_model: str = "deepseek-v4-flash"
    review_model: str = _model("DEEPSEEK_REVIEW_MODEL", "deepseek-v4-pro")
    deepseek_timeout_seconds: int = _as_int("DEEPSEEK_TIMEOUT_SECONDS", 45, 15, 120)
    deepseek_max_retries: int = _as_int("DEEPSEEK_MAX_RETRIES", 1, 0, 2)
    deepseek_identity_context_chars: int = _as_int("DEEPSEEK_IDENTITY_CONTEXT_CHARS", 7000, 3000, 15000)
    deepseek_profile_context_chars: int = _as_int("DEEPSEEK_PROFILE_CONTEXT_CHARS", 22000, 8000, 50000)
    deepseek_relation_context_chars: int = _as_int("DEEPSEEK_RELATION_CONTEXT_CHARS", 30000, 10000, 60000)
    deepseek_risk_context_chars: int = _as_int("DEEPSEEK_RISK_CONTEXT_CHARS", 30000, 10000, 60000)
    deepseek_profile_max_tokens: int = _as_int("DEEPSEEK_PROFILE_MAX_TOKENS", 3000, 900, 5000)
    deepseek_relation_max_tokens: int = _as_int("DEEPSEEK_RELATION_MAX_TOKENS", 4500, 1200, 7000)
    deepseek_risk_max_tokens: int = _as_int("DEEPSEEK_RISK_MAX_TOKENS", 4500, 1200, 7000)
    deepseek_decision_max_tokens: int = _as_int("DEEPSEEK_DECISION_MAX_TOKENS", 2600, 1000, 5000)
    deepseek_structured_fallback_to_pro: bool = _as_bool("DEEPSEEK_STRUCTURED_FALLBACK_TO_PRO", True)
    enable_pro_decision_analysis: bool = _as_bool("ENABLE_PRO_DECISION_ANALYSIS", True)

    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "").strip()
    tavily_base_url: str = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
    tavily_timeout_seconds: int = _as_int("TAVILY_TIMEOUT_SECONDS", 15, 5, 40)
    tavily_identity_depth: str = _depth("TAVILY_IDENTITY_DEPTH", "basic")
    tavily_detail_depth: str = _depth("TAVILY_DETAIL_DEPTH", "basic")
    tavily_retry_depth: str = _depth("TAVILY_RETRY_DEPTH", "advanced")
    tavily_country: str = os.getenv("TAVILY_COUNTRY", "").strip().lower()
    tavily_max_results_identity: int = _as_int("TAVILY_MAX_RESULTS_IDENTITY", 7, 4, 12)
    tavily_max_results_detail: int = _as_int("TAVILY_MAX_RESULTS_DETAIL", 8, 5, 15)
    tavily_include_raw_content: bool = False
    tavily_max_chars_per_source: int = _as_int("TAVILY_MAX_CHARS_PER_SOURCE", 6500, 1800, 16000)
    tavily_max_sources: int = _as_int("TAVILY_MAX_SOURCES", 40, 12, 45)
    # No local hard credit ceiling: internal cost controls must never interrupt a
    # valid investigation. Cost is reduced through caching, query de-duplication,
    # focused extraction and evidence-gap follow-ups. Only Tavily's actual account
    # limit can stop a request.
    tavily_followup_rounds: int = _as_int("TAVILY_FOLLOWUP_ROUNDS", 3, 0, 3)
    tavily_min_score: float = _as_float("TAVILY_MIN_SCORE", 0.35, 0.0, 1.0)
    minimum_source_relevance: int = _as_int("MINIMUM_SOURCE_RELEVANCE", 3, 0, 20)
    tavily_extract_enabled: bool = _as_bool("TAVILY_EXTRACT_ENABLED", True)
    tavily_extract_depth: str = _extract_depth("TAVILY_EXTRACT_DEPTH", "basic")
    tavily_extract_max_urls: int = _as_int("TAVILY_EXTRACT_MAX_URLS", 15, 4, 20)
    tavily_extract_chunks_per_source: int = _as_int("TAVILY_EXTRACT_CHUNKS_PER_SOURCE", 3, 1, 5)
    tavily_extract_timeout_seconds: int = _as_int("TAVILY_EXTRACT_TIMEOUT_SECONDS", 18, 8, 45)
    adaptive_followup_enabled: bool = _as_bool("ADAPTIVE_FOLLOWUP_ENABLED", True)

    stock_resolver_enabled: bool = _as_bool("STOCK_RESOLVER_ENABLED", True)
    stock_resolver_timeout_seconds: int = _as_int("STOCK_RESOLVER_TIMEOUT_SECONDS", 7, 3, 15)
    listed_profile_resolver_enabled: bool = _as_bool("LISTED_PROFILE_RESOLVER_ENABLED", True)

    cache_ttl_hours: int = _as_int("CACHE_TTL_HOURS", 168, 1, 720)
    cache_db_path: Path = ROOT / os.getenv("CACHE_DB_PATH", "runtime/cache/research_cache.db")
    task_workers: int = _as_int("TASK_WORKERS", 6, 1, 16)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def api_keys(self) -> List[str]:
        return list(dict.fromkeys(key for key in (self.deepseek_api_key, self.deepseek_api_key_backup) if key))

    @property
    def deepseek_ready(self) -> bool:
        return bool(self.api_keys)

    @property
    def tavily_ready(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def api_ready(self) -> bool:
        return self.deepseek_ready and self.tavily_ready


settings = Settings()
settings.cache_db_path.parent.mkdir(parents=True, exist_ok=True)
