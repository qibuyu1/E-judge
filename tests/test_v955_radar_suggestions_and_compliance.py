from pathlib import Path

from web_app.risk_engine import calculate_risk
from web_app.schemas import AnnualFact, CompanyProfile, Relationship, ResearchExtraction, RiskSignal


def complete_extraction() -> ResearchExtraction:
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name="测试股份有限公司",
            short_name="测试公司",
            stock_code="600000",
            credit_code="91310000000000000X",
            legal_person="张三",
            registered_address="上海市",
            registered_capital="10亿元",
            company_status="存续",
            industry="制造业",
            controlling_shareholder="测试集团有限公司",
            actual_controller="某市国资委",
        ),
        relationships=[
            Relationship(
                source_name="测试股份有限公司",
                target_name="测试子公司",
                relation_type="投资",
                confidence=.92,
            )
        ],
        annual_facts=[
            AnnualFact(year=2025, metric="归母净利润", value="5亿元", confidence=.95),
            AnnualFact(year=2025, metric="经营活动现金流", value="净流入3亿元", confidence=.95),
            AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", confidence=.95),
            AnnualFact(year=2025, metric="资产负债率", value="45%", confidence=.95),
        ],
        identity_verified=True,
        overall_confidence=.92,
    )


def test_query_radar_has_rotating_dots_but_no_center_percentage():
    root = Path(__file__).resolve().parents[1]
    html = (root / "frontend/index.html").read_text("utf-8")
    css = (root / "frontend/styles.css").read_text("utf-8")
    js = (root / "frontend/query.js").read_text("utf-8")
    assert 'id="radarProgressValue"' not in html
    assert "queryRadarDotA" in css and "queryRadarDotB" in css
    assert 'id="progressValue"' in html
    assert "radarProgressValue" not in js


def test_report_uses_personalized_suggestions_instead_of_generic_view_cards():
    root = Path(__file__).resolve().parents[1]
    html = (root / "frontend/report.html").read_text("utf-8")
    js = (root / "frontend/report.js").read_text("utf-8")
    assert 'id="judgmentSuggestions"' in html
    assert "针对本企业的建议" in html
    assert "银行授信视角" not in html
    assert "企业合作视角" not in html
    assert "personalized_suggestions" in js


def test_major_tax_violation_is_medium_not_automatically_high_and_has_specific_advice():
    extraction = complete_extraction()
    extraction.risk_signals.append(
        RiskSignal(
            category="合规",
            title="重大税收违法",
            description="公司被认定重大税收违法，尚未完成整改",
            severity=84,
            confidence=.92,
        )
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "中风险"
    assert result.risk_score is not None and 40 <= result.risk_score < 60
    suggestions = result.credit_judgment["personalized_suggestions"]
    assert 1 <= len(suggestions) <= 3
    assert any("重大税收违法" in item for item in suggestions)


def test_resolved_environmental_penalty_does_not_create_medium_or_high_risk():
    extraction = complete_extraction()
    extraction.risk_signals.append(
        RiskSignal(
            category="合规",
            title="环境处罚",
            description="公司曾受到环境处罚，已整改并完成验收",
            severity=82,
            confidence=.92,
        )
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "低风险"
    assert result.risk_score is not None and result.risk_score < 40
