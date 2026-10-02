"""AI provider, extraction pipelines, metering.

Owns (single writer, BACKEND_DESIGN.md §5.1): extractions, ai_calls, ai_cost_rollups.
Other modules import only from this package root; only this module imports ``google.genai``.
"""

from __future__ import annotations

from eca.intelligence.config import AIConfig, build_ai_client, load_ai_config
from eca.intelligence.provenance import EvidenceSpan, Provenance, SourceRef, confidence_band
from eca.intelligence.provider.attempts import (
    Action,
    Decision,
    Degraded,
    ErrorKind,
    classify,
    next_background_attempt,
    run_interactive,
)
from eca.intelligence.provider.cassette import CassetteKey, CassetteStore, input_hash
from eca.intelligence.provider.client import AIClient, EmbedResult, GenerateResult
from eca.intelligence.provider.meter import CallRecord, Meter, bucket_floor, rollup_costs
from eca.intelligence.provider.pricing import PriceTable
from eca.intelligence.provider.registry import RoleRegistry, RoleSpec
from eca.intelligence.provider.types import (
    AIError,
    CallStatus,
    CassetteMiss,
    ConfigError,
    EmptyResponse,
    FileRef,
    NoFallback,
    PriceNotFound,
    ProviderError,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    RoleDisabled,
    SafetyBlocked,
    SchemaInvalid,
    UnknownRole,
    Usage,
)
from eca.intelligence.tasks import COST_ROLLUP_TASK, periodic_tasks

__all__ = [
    "COST_ROLLUP_TASK",
    "AIClient",
    "AIConfig",
    "AIError",
    "Action",
    "CallRecord",
    "CallStatus",
    "CassetteKey",
    "CassetteMiss",
    "CassetteStore",
    "ConfigError",
    "Decision",
    "Degraded",
    "EmbedResult",
    "EmptyResponse",
    "ErrorKind",
    "EvidenceSpan",
    "FileRef",
    "GenerateResult",
    "Meter",
    "NoFallback",
    "PriceNotFound",
    "PriceTable",
    "Provenance",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRejected",
    "ProviderTimeout",
    "ProviderUnavailable",
    "RoleDisabled",
    "RoleRegistry",
    "RoleSpec",
    "SafetyBlocked",
    "SchemaInvalid",
    "SourceRef",
    "UnknownRole",
    "Usage",
    "bucket_floor",
    "build_ai_client",
    "classify",
    "confidence_band",
    "input_hash",
    "load_ai_config",
    "next_background_attempt",
    "periodic_tasks",
    "rollup_costs",
    "run_interactive",
]
