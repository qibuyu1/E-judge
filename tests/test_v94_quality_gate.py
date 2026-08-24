from web_app.profile_quality import clean_profile_value, sanitize_profile_object
from web_app.risk_engine import calculate_risk
from web_app.schemas import AnnualFact, CompanyProfile, FieldEvidence, JudicialEvent, Relationship, ResearchExtraction, RiskSignal


def healthy_listed_issuer():
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name="南京钢铁股份有限公司", short_name="南钢股份", stock_code="600282",
            credit_code="91320000714085405J", legal_person="黄一新", registered_address="江苏省南京市六合区卸甲甸",
            registered_capital="616509.1011万元", company_status="存续", industry="黑色金属冶炼和压延加工业",
            controlling_shareholder="南京南钢产业发展有限公司", actual_controller="中国中信集团有限公司",
            field_evidence={"company_name": FieldEvidence(source_ids=["OFFICIAL"], confidence=.98)},
        ),
        identity_verified=True, overall_confidence=.95,
        annual_facts=[
            AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", source_ids=["AR"], confidence=.98),
            AnnualFact(year=2025, metric="归母净利润", value="28.67亿元", source_ids=["AR"], confidence=.98),
            AnnualFact(year=2025, metric="经营活动现金流", value="正向净流入", source_ids=["AR"], confidence=.95),
        ],
    )


def test_malformed_profile_cells_are_removed_not_displayed_in_wrong_columns():
    p = CompanyProfile(
        legal_person="营业执照注册号",
        registered_address="南京市六合区卸甲甸 法定代表人 黄一新 经营范围 一般危险化学品批发",
        controlling_shareholder="集团）有限公司",
        actual_controller="未发生变化",
    )
    sanitize_profile_object(p)
    assert p.legal_person is None or p.legal_person == ""
    assert p.registered_address == "南京市六合区卸甲甸"
    assert p.controlling_shareholder is None or p.controlling_shareholder == ""
    assert p.actual_controller is None or p.actual_controller == ""


def test_healthy_large_cyclical_listed_issuer_is_not_inverted_to_high():
    x = healthy_listed_issuer()
    x.relationships.extend([
        Relationship(source_name=x.profile.company_name, target_name="全资子公司甲", relation_type="对外担保", description="合并范围内正常经营担保，正常履行，未触发代偿", risk_level="high", confidence=.95, source_ids=["G1"]),
        Relationship(source_name="子公司甲", target_name="子公司乙", relation_type="关联交易", description="日常经营采购，定价公允", risk_level="high", confidence=.9, source_ids=["R1"]),
    ])
    x.risk_signals.extend([
        RiskSignal(category="信息披露", title="历史监管关注已整改", description="此前监管关注事项已整改，未认定重大违法", severity=92, confidence=.9, source_ids=["S1"]),
        RiskSignal(category="司法", title="子公司合同纠纷", description="某控股子公司有限公司历史合同纠纷已结案并履行完毕", severity=90, confidence=.9, source_ids=["S2"]),
    ])
    result = calculate_risk(x)
    assert result.risk_level == "低风险"
    assert result.risk_score is not None and result.risk_score < 40


def test_many_vague_model_high_cards_without_concrete_credit_fact_stay_low():
    x = healthy_listed_issuer()
    for i in range(8):
        x.risk_signals.append(RiskSignal(category="综合风险", title=f"风险事项{i}", description="公开信息建议持续关注经营变化", severity=95, confidence=.95, source_ids=[f"S{i}"]))
    result = calculate_risk(x)
    assert result.risk_level == "低风险"
    assert result.risk_score is not None and result.risk_score < 40


def test_other_subsidiary_relationships_do_not_score_as_issuer_risk():
    x = healthy_listed_issuer()
    for i in range(10):
        x.relationships.append(Relationship(source_name=f"子公司{i}", target_name=f"外部公司{i}", relation_type="重大诉讼", description="关联公司之间的未决诉讼，不涉及上市公司承担责任", risk_level="critical", confidence=.95, source_ids=[f"R{i}"]))
    assert calculate_risk(x).risk_level == "低风险"
