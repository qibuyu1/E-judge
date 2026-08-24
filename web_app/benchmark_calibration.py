from __future__ import annotations

"""Generic calibration guardrails learned from an external expert workbook.

The raw workbook and company labels are intentionally not distributed with the
application.  Production scoring must remain evidence-led and must never obtain a
company's grade from its name.  Only aggregate, non-identifying statistics are kept
here as a weak sanity-check prior.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .schemas import ResearchExtraction

# Aggregate distribution from the external 100-row expert review set.  No company
# names, counterparties or row-level labels are retained in the project.
_EXTERNAL_BENCHMARK_SUMMARY: Dict[str, Any] = {
    "as_of": "2026-07-13",
    "case_count": 100,
    "level_distribution": {"低": 21, "中": 56, "高": 23},
    "type_distribution": {
        "对外担保": {"低": 5, "中": 32, "高": 3},
        "欠债": {"低": 2, "中": 5, "高": 13},
        "法律诉讼": {"低": 4, "中": 11, "高": 5},
        "股权变动": {"低": 10, "中": 8, "高": 2},
    },
}

# Mean anchors are deliberately modest.  They can nudge a borderline score upward
# only when actual adverse evidence exists; they can never lower a score and can
# never create a grade from a company name.
_TYPE_PRIORS = {
    "对外担保": 49.5,
    "欠债": 63.2,
    "法律诉讼": 51.0,
    "股权变动": 41.6,
}

_TYPE_PATTERNS = {
    "对外担保": re.compile(r"担保|保证|或有负债|反担保"),
    "欠债": re.compile(r"欠债|债务|负债|逾期|违约|未兑付|偿债|有息负债|流动性"),
    "法律诉讼": re.compile(r"诉讼|仲裁|执行|失信|限高|法院|裁判|处罚"),
    "股权变动": re.compile(r"股权变动|控股权|实际控制人|股东变更|减持|增持|股份转让|控制权变更"),
}


def external_benchmark_summary() -> Dict[str, Any]:
    return {
        "as_of": _EXTERNAL_BENCHMARK_SUMMARY["as_of"],
        "case_count": _EXTERNAL_BENCHMARK_SUMMARY["case_count"],
        "level_distribution": dict(_EXTERNAL_BENCHMARK_SUMMARY["level_distribution"]),
        "type_distribution": {
            key: dict(value) for key, value in _EXTERNAL_BENCHMARK_SUMMARY["type_distribution"].items()
        },
    }


def load_benchmark() -> Dict[str, Any]:
    """Compatibility API: returns aggregate metadata only, never row-level labels."""
    return external_benchmark_summary()


def company_reference_levels() -> Dict[str, str]:
    """Company-name lookup is intentionally disabled to prevent label leakage."""
    return {}


def reference_level(company_name: str | None) -> Optional[str]:
    return None


def type_reference_stats() -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for risk_type, counts in _EXTERNAL_BENCHMARK_SUMMARY["type_distribution"].items():
        result[risk_type] = {
            "counts": dict(counts),
            "mean_score": _TYPE_PRIORS[risk_type],
            "total": sum(counts.values()),
        }
    return result


def detected_reference_types(extraction: ResearchExtraction) -> List[str]:
    parts: List[str] = []
    for signal in extraction.risk_signals:
        parts.append(f"{signal.category} {signal.title} {signal.description}")
    for event in extraction.judicial_events:
        parts.append(f"{event.event_type} {event.title} {event.description}")
    for relation in extraction.relationships:
        parts.append(f"{relation.relation_type} {relation.description}")
    for fact in extraction.annual_facts:
        parts.append(f"{fact.metric} {fact.value}")
    text = " ".join(parts)
    return [risk_type for risk_type, pattern in _TYPE_PATTERNS.items() if pattern.search(text)]


def benchmark_type_prior(extraction: ResearchExtraction) -> Optional[float]:
    types = detected_reference_types(extraction)
    scores = [_TYPE_PRIORS[item] for item in types if item in _TYPE_PRIORS]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 2)


def _has_material_adverse_evidence(extraction: ResearchExtraction) -> bool:
    if any(float(signal.severity) * float(signal.confidence) >= 45 for signal in extraction.risk_signals):
        return True
    if any(float(event.severity) >= 55 for event in extraction.judicial_events):
        return True
    if any(relation.risk_level in {"high", "critical"} for relation in extraction.relationships):
        return True
    financial_tokens = (
        "亏损", "净资产为负", "资产负债率", "经营现金流为负", "债务逾期",
        "债务违约", "无法表示意见", "有息负债",
    )
    return any(any(token in f"{fact.metric} {fact.value}" for token in financial_tokens) for fact in extraction.annual_facts)


def calibrate_score_with_benchmark(
    extraction: ResearchExtraction,
    raw_score: Optional[float],
    *,
    allow_expert_reference: bool = True,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Return an evidence-led score and attach non-identifying audit metadata.

    The external workbook is not bundled.  Company names and row-level labels are not
    looked up, and aggregate type statistics never alter the production score.
    """
    if raw_score is None:
        return None, {"mode": "none", "company_lookup": False}

    score = float(raw_score)
    prior = benchmark_type_prior(extraction) if allow_expert_reference else None
    # v9.1 deliberately keeps external benchmark statistics in audit-only mode.
    # A type distribution cannot prove the severity of a particular company event,
    # so it must not move a production score across a low/medium/high threshold.
    adjusted = score

    return round(max(0.0, min(100.0, adjusted)), 1), {
        "mode": "evidence_only",
        "company_lookup": False,
        "detected_types": detected_reference_types(extraction),
        "prior_score": prior,
        "raw_score": round(score, 1),
        "adjustment": 0.0,
        "benchmark_as_of": _EXTERNAL_BENCHMARK_SUMMARY["as_of"],
        "benchmark_role": "audit_only",
    }


def benchmark_regression_rows() -> List[Dict[str, str]]:
    """Row-level benchmark data is not bundled; kept only for API compatibility."""
    return []
