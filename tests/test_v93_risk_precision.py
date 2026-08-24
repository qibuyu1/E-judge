from web_app.risk_engine import calculate_risk
from web_app.schemas import AnnualFact, CompanyProfile, FieldEvidence, JudicialEvent, Relationship, ResearchExtraction, RiskSignal


def base():
    return ResearchExtraction(
        profile=CompanyProfile(company_name="精度测试企业", credit_code="91300000000000000X", legal_person="某负责人", registered_address="某地", registered_capital="1亿元", company_status="存续", industry="制造业", controlling_shareholder="某股东", actual_controller="某控制人", field_evidence={"company_name": FieldEvidence(source_ids=["S1"], confidence=.95)}),
        identity_verified=True, overall_confidence=.92,
        annual_facts=[AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", confidence=.95)],
    )


def test_many_normal_relations_never_raise_company_risk():
    x=base()
    for i in range(29):
        x.relationships.append(Relationship(source_name="精度测试企业", target_name=f"子公司{i}", relation_type="投资/子公司", description="正常经营范围内投资关系", confidence=.95, risk_level="high"))
    assert calculate_risk(x).risk_level == "低风险"


def test_two_vague_transaction_mentions_do_not_create_high_risk():
    x=base()
    x.risk_signals.extend([
        RiskSignal(category="关联交易", title="关联交易需关注", description="公开公告披露日常关联交易，正常履行", severity=95, confidence=.95),
        RiskSignal(category="信息披露", title="监管关注", description="交易所出具监管关注函，未认定重大违法", severity=92, confidence=.9),
    ])
    result=calculate_risk(x)
    assert result.risk_level != "高风险"
    assert result.risk_score is not None and result.risk_score < 60


def test_duplicate_same_vague_default_family_does_not_force_high():
    x=base()
    for title in ("债务违约", "到期债务未兑付", "逾期债务尚未清偿"):
        x.risk_signals.append(RiskSignal(category="偿债", title=title, description="同一笔到期债务尚未清偿", severity=94, confidence=.95, source_ids=["S2"]))
    result=calculate_risk(x)
    assert result.risk_level != "高风险"
    assert result.risk_score is not None and result.risk_score < 60


def test_negative_roe_is_not_negative_net_assets():
    x=base(); x.annual_facts.append(AnnualFact(year=2025, metric="净资产收益率", value="-6.2%", confidence=.95))
    result=calculate_risk(x)
    assert result.risk_level == "低风险"


def test_negative_profit_growth_is_not_a_loss():
    x=base(); x.annual_facts.append(AnnualFact(year=2025, metric="归母净利润同比增长率", value="-30.3%", confidence=.95))
    result=calculate_risk(x)
    assert result.risk_level == "低风险"


def test_resolved_fund_occupation_is_not_current_high():
    x=base(); x.risk_signals.append(RiskSignal(category="关联交易", title="资金占用已归还", description="历史占用款已全部归还并整改完毕", severity=96, confidence=.95))
    assert calculate_risk(x).risk_level == "低风险"


def test_guarantee_compensation_is_high():
    x=base(); x.relationships.append(Relationship(source_name="精度测试企业", target_name="被担保方", relation_type="对外担保", description="被担保方债务违约，公司已承担担保代偿责任且尚未收回", confidence=.95, risk_level="critical"))
    assert calculate_risk(x).risk_level == "高风险"


def test_active_large_execution_is_high():
    x=base(); x.judicial_events.append(JudicialEvent(event_type="执行", title="被执行", description="仍处于执行状态", amount=2, amount_unit="亿元", severity=88))
    assert calculate_risk(x).risk_level == "高风险"


def test_resolved_litigation_does_not_stay_medium():
    x=base(); x.judicial_events.append(JudicialEvent(event_type="诉讼", title="历史合同纠纷", description="案件已结案并履行完毕", amount=3000, amount_unit="万元", severity=85))
    assert calculate_risk(x).risk_level == "低风险"


def test_state_owned_background_does_not_hide_default():
    x=base(); x.profile.actual_controller="某省国资委"; x.risk_signals.append(RiskSignal(category="偿债", title="债务违约", description="到期债务仍未清偿", severity=92, confidence=.95))
    assert calculate_risk(x).risk_level == "高风险"
