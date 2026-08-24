from pathlib import Path

from web_app.report_service import _company_payload
from web_app.risk_engine import calculate_risk
from web_app.schemas import AnnualFact, CompanyProfile, FieldEvidence, Relationship, ResearchExtraction, RiskSignal


def _issuer() -> ResearchExtraction:
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name="南京钢铁股份有限公司",
            short_name="南钢股份",
            stock_code="600282",
            credit_code="91320000714085405J",
            legal_person="黄一新",
            registered_address="江苏省南京市六合区卸甲甸",
            registered_capital="616509.1011万元",
            company_status="存续",
            industry="黑色金属冶炼和压延加工业",
            controlling_shareholder="南京南钢产业发展有限公司",
            actual_controller="中国中信集团有限公司",
            field_evidence={"company_name": FieldEvidence(source_ids=["AR"], confidence=.98)},
        ),
        identity_verified=True,
        overall_confidence=.95,
        annual_facts=[
            AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", confidence=.98),
            AnnualFact(year=2025, metric="归母净利润", value="28.67亿元", confidence=.98),
            AnnualFact(year=2025, metric="经营活动现金流", value="正向净流入", confidence=.96),
        ],
    )


def test_nangang_like_healthy_evidence_is_low_not_inverted_high():
    x = _issuer()
    for i in range(29):
        x.relationships.append(Relationship(
            source_name=x.profile.company_name,
            target_name=f"子公司{i}",
            relation_type="投资/子公司",
            description="正常经营范围内投资关系",
            confidence=.92,
            risk_level="high",  # deliberately hostile model label; facts must win
        ))
    x.risk_signals.extend([
        RiskSignal(category="监管", title="监管关注", description="未认定重大违法，相关事项已整改", severity=95, confidence=.9),
        RiskSignal(category="关联交易", title="日常关联交易", description="定价公允并正常履行", severity=95, confidence=.9),
        RiskSignal(category="担保", title="合并范围担保", description="为全资子公司提供经营担保，未触发代偿且正常履行", severity=95, confidence=.9),
    ])
    result = calculate_risk(x)
    assert result.risk_level == "低风险"
    assert result.risk_score is not None and result.risk_score < 40
    assert result.abnormal_transaction_count == 0


def test_direct_default_overrides_positive_financial_bundle():
    x = _issuer()
    x.risk_signals.append(RiskSignal(
        category="偿债", title="债务违约", description="到期债务尚未清偿", severity=94, confidence=.95
    ))
    result = calculate_risk(x)
    assert result.risk_level == "高风险"
    assert result.risk_score is not None and result.risk_score >= 60


def test_company_payload_has_final_column_guard():
    x = _issuer()
    x.profile.legal_person = "营业执照注册号"
    x.profile.registered_address = "南京市六合区卸甲甸 法定代表人 黄一新 经营范围 化学品批发"
    x.profile.controlling_shareholder = "集团）有限公司"
    x.profile.actual_controller = "未发生变化"
    company = _company_payload(x)
    assert not company["legal_person"]
    assert company["registered_address"] == "南京市六合区卸甲甸"
    assert not company["controlling_shareholder"]
    assert not company["actual_controller"]


def test_v941_visual_contrast_and_large_four_piece_silhouette():
    root = Path(__file__).resolve().parents[1]
    css = (root / "frontend/styles.css").read_text("utf-8")
    logo = (root / "frontend/assets/logo.svg").read_text("utf-8")
    final = css.split("v9.4.1 VERIFIED VISUAL SYSTEM", 1)[1]
    assert "gap:2px!important" in final
    assert "width:520px!important" in final
    assert "tier-critical" in final and "tier-high" in final and "tier-medium" in final and "tier-low" in final
    assert ".source-row:hover .source-main strong" in final and "color:#431219!important" in final
    assert "filter:brightness(0) invert(1)" not in final
    assert 'stroke="#8b101d"' in logo and 'fill="#fffaf7"' in logo
