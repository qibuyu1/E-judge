from pathlib import Path

from web_app.risk_engine import calculate_risk
from web_app.schemas import AnnualFact, CompanyProfile, FieldEvidence, ResearchExtraction, RiskSignal


def base_extraction() -> ResearchExtraction:
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name="质量复核股份有限公司", stock_code="600888",
            credit_code="91310000000000000X", legal_person="张三",
            registered_address="上海市", registered_capital="10亿元",
            company_status="存续", industry="制造业",
            controlling_shareholder="质量控股有限公司", actual_controller="某市国资委",
            field_evidence={"company_name": FieldEvidence(source_ids=["ID"], confidence=.98)},
        ),
        identity_verified=True, overall_confidence=.92,
    )


def test_repeated_same_year_financial_extracts_do_not_stack_as_independent_risks():
    extraction = base_extraction()
    extraction.annual_facts.extend([
        AnnualFact(year=2025, metric="归母净利润", value="亏损1.2亿元", confidence=.92, source_ids=["A"]),
        AnnualFact(year=2025, metric="归母净利润", value="亏损1.2亿元", confidence=.91, source_ids=["B"]),
        AnnualFact(year=2025, metric="归母净利润", value="亏损1.2亿元", confidence=.90, source_ids=["C"]),
    ])
    result = calculate_risk(extraction)
    assert result.risk_level != "高风险"
    assert result.risk_score is not None and result.risk_score < 60


def test_sparse_non_direct_signal_cannot_become_high_risk():
    extraction = base_extraction()
    extraction.profile.legal_person = None
    extraction.profile.registered_address = None
    extraction.profile.registered_capital = None
    extraction.profile.controlling_shareholder = None
    extraction.profile.actual_controller = None
    extraction.risk_signals.append(RiskSignal(
        category="经营", title="需关注经营压力",
        description="公开网页提到行业景气承压，具体影响仍待核验",
        severity=88, confidence=.72, source_ids=["S1"],
    ))
    result = calculate_risk(extraction)
    assert result.risk_level in {"低风险", "中风险", "证据不足"}
    assert result.risk_score is None or result.risk_score < 60


def test_loading_panel_is_embedded_in_search_workspace():
    root = Path(__file__).resolve().parents[1]
    html = (root / "frontend/index.html").read_text("utf-8")
    css = (root / "frontend/styles.css").read_text("utf-8")
    query_js = (root / "frontend/query.js").read_text("utf-8")
    assert html.index('id="loadingPanel"') < html.index('</section>\n\n<footer')
    assert "v9.5.4 EMBEDDED RESEARCH WORKSPACE" in css
    assert 'document.body.classList.add("is-researching")' in query_js
    assert 'block: "nearest"' in query_js


def test_conflicting_profit_facts_do_not_trigger_healthy_mitigation():
    extraction = base_extraction()
    extraction.annual_facts.extend([
        AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", confidence=.95, source_ids=["A"]),
        AnnualFact(year=2025, metric="归母净利润", value="5亿元", confidence=.95, source_ids=["A"]),
        AnnualFact(year=2024, metric="归母净利润", value="亏损2亿元", confidence=.95, source_ids=["B"]),
        AnnualFact(year=2025, metric="归母净利润", value="亏损3亿元", confidence=.95, source_ids=["C"]),
        AnnualFact(year=2025, metric="资产负债率", value="78%", confidence=.95, source_ids=["C"]),
    ])
    result = calculate_risk(extraction)
    assert result.risk_level in {"中风险", "高风险"}
    assert result.risk_score is not None and result.risk_score >= 40
