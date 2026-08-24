from __future__ import annotations

from web_app.benchmark_calibration import (
    benchmark_regression_rows,
    calibrate_score_with_benchmark,
    company_reference_levels,
    external_benchmark_summary,
    reference_level,
)
from web_app.risk_engine import calculate_risk
from web_app.schemas import AnnualFact, CompanyProfile, ResearchExtraction, RiskSignal


def test_external_benchmark_is_aggregate_only_and_has_no_company_lookup():
    summary = external_benchmark_summary()
    assert summary["case_count"] == 100
    assert summary["level_distribution"] == {"低": 21, "中": 56, "高": 23}
    assert benchmark_regression_rows() == []
    assert company_reference_levels() == {}
    assert reference_level("任意公司") is None


def test_company_name_cannot_force_or_lower_an_evidence_score():
    extraction = ResearchExtraction(
        profile=CompanyProfile(company_name="专家表中曾出现的企业名称"),
        identity_verified=True,
        overall_confidence=0.9,
    )
    score, meta = calibrate_score_with_benchmark(extraction, 72.0)
    assert score == 72.0
    assert meta["company_lookup"] is False
    assert meta["mode"] == "evidence_only"


def test_aggregate_statistics_are_audit_only_and_never_move_score():
    clean = ResearchExtraction(
        profile=CompanyProfile(company_name="普通企业"),
        identity_verified=True,
        overall_confidence=0.9,
    )
    clean_score, _ = calibrate_score_with_benchmark(clean, 35.0)
    assert clean_score == 35.0

    adverse = ResearchExtraction(
        profile=CompanyProfile(company_name="普通企业"),
        risk_signals=[RiskSignal(category="经营偿债", title="债务逾期", severity=62, confidence=0.9)],
        identity_verified=True,
        overall_confidence=0.9,
    )
    adjusted, meta = calibrate_score_with_benchmark(adverse, 45.0)
    assert adjusted == 45.0
    assert meta["company_lookup"] is False
    assert meta["benchmark_role"] == "audit_only"
    assert meta["adjustment"] == 0.0


def test_sustained_losses_high_leverage_and_cyclical_industry_are_high_risk():
    extraction = ResearchExtraction(
        profile=CompanyProfile(
            company_name="某光伏玻璃股份有限公司",
            industry="光伏玻璃制造",
            actual_controller="某中央国有企业集团",
            registered_address="某市产业园",
            legal_person="张某",
            registered_capital="10亿元",
            company_status="存续",
            controlling_shareholder="某国有控股公司",
            credit_code="913000000000000000",
        ),
        annual_facts=[
            AnnualFact(year=2024, metric="归母净利润", value="亏损5.2亿元", confidence=0.95),
            AnnualFact(year=2025, metric="归母净利润", value="亏损7.1亿元", confidence=0.95),
            AnnualFact(year=2026, metric="资产负债率", value="76.10%", confidence=0.95),
            AnnualFact(year=2026, metric="货币资金", value="1.76亿元", confidence=0.95),
            AnnualFact(year=2026, metric="有息负债", value="66.13亿元", confidence=0.95),
        ],
        identity_verified=True,
        overall_confidence=0.92,
    )
    result = calculate_risk(extraction)
    assert result.risk_score is not None and result.risk_score >= 60
    assert result.risk_level == "高风险"
    assert result.credit_judgment["grade"] == "高"
    assert "新增无保障信用敞口" in result.credit_judgment["headline"]
