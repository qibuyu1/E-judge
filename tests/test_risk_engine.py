from web_app.risk_engine import build_graph, calculate_risk
from web_app.schemas import (
    AnnualFact,
    CompanyProfile,
    FieldEvidence,
    JudicialEvent,
    Relationship,
    ResearchExtraction,
    RiskSignal,
)


def verified_extraction() -> ResearchExtraction:
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name="测试主体",
            credit_code="91300000000000000X",
            legal_person="某负责人",
            registered_address="某地",
            registered_capital="1000万元",
            company_status="存续",
            industry="制造业",
            controlling_shareholder="某股东",
            actual_controller="某控制人",
            field_evidence={"company_name": FieldEvidence(source_ids=["S1"], confidence=0.95)},
        ),
        overall_confidence=0.9,
        identity_verified=True,
    )


def test_unverified_identity_is_not_scored():
    extraction = ResearchExtraction(overall_confidence=0.8, identity_verified=False)
    result = calculate_risk(extraction)
    assert result.scorable is False
    assert result.risk_score is None


def test_verified_clean_evidence_is_deterministic():
    extraction = verified_extraction()
    first = calculate_risk(extraction)
    second = calculate_risk(extraction)
    assert first.model_dump() == second.model_dump()
    assert first.risk_score == 0


def test_risk_signal_increases_corresponding_dimension():
    extraction = verified_extraction()
    extraction.risk_signals.append(
        RiskSignal(
            category="关联交易",
            title="发现循环交易风险",
            description="公开文件明确披露循环交易",
            severity=85,
            source_ids=["S2"],
            confidence=0.95,
        )
    )
    result = calculate_risk(extraction)
    transaction = next(item for item in result.dimensions if item.key == "transaction")
    assert transaction.score > 0
    assert result.risk_score is not None and result.risk_score > 0


def test_graph_does_not_create_unverified_relations():
    extraction = verified_extraction()
    result = calculate_risk(extraction)
    graph = build_graph(extraction, result)
    assert len(graph.nodes) == 1
    assert graph.edges == []


def test_graph_preserves_multihop_relationship_and_risk_node():
    from web_app.schemas import Relationship
    extraction = verified_extraction()
    extraction.relationships.extend([
        Relationship(
            source_name="测试主体",
            target_name="控股公司A",
            relation_type="控股",
            share_ratio=60,
            source_ids=["S1"],
            confidence=0.9,
            risk_level="low",
        ),
        Relationship(
            source_name="控股公司A",
            target_name="子公司B",
            relation_type="投资",
            share_ratio=70,
            source_ids=["S2"],
            confidence=0.9,
            risk_level="medium",
        ),
    ])
    extraction.risk_signals.append(
        RiskSignal(
            category="股权质押",
            title="高比例股权质押",
            severity=80,
            source_ids=["S3"],
            confidence=0.9,
        )
    )
    graph = build_graph(extraction, calculate_risk(extraction))
    names = {n.name: n.id for n in graph.nodes}
    assert "控股公司A" in names and "子公司B" in names
    assert any(e.source == names["控股公司A"] and e.target == names["子公司B"] for e in graph.edges)
    assert any(n.company_type == "风险事件" for n in graph.nodes)


def test_negated_and_resolved_signals_do_not_become_high_risk():
    extraction = verified_extraction()
    extraction.risk_signals.extend(
        [
            RiskSignal(
                category="偿债",
                title="未发生债务违约",
                description="不存在逾期债务",
                severity=98,
                confidence=0.98,
            ),
            RiskSignal(
                category="股权",
                title="股权冻结已解除",
                description="相关冻结已经解除并结案",
                severity=90,
                confidence=0.95,
            ),
        ]
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "低风险"
    assert result.risk_score is not None and result.risk_score < 40


def test_normal_guarantee_does_not_force_high_risk():
    extraction = verified_extraction()
    extraction.relationships.append(
        Relationship(
            source_name="测试主体",
            target_name="全资子公司",
            relation_type="对外担保",
            description="合并范围内正常经营担保，正常履行，未触发代偿责任",
            confidence=0.95,
            risk_level="high",
        )
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "低风险"


def test_material_active_litigation_is_at_least_medium():
    extraction = verified_extraction()
    extraction.judicial_events.append(
        JudicialEvent(
            event_type="民事诉讼",
            title="重大合同纠纷诉讼",
            description="案件正在审理，尚未解决",
            amount=3000,
            amount_unit="万元",
            severity=72,
        )
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "中风险"
    assert result.risk_score is not None and 40 <= result.risk_score < 60


def test_debt_default_plus_execution_is_high_risk():
    extraction = verified_extraction()
    extraction.risk_signals.append(
        RiskSignal(
            category="偿债",
            title="债务违约",
            description="到期债务尚未清偿",
            severity=92,
            confidence=0.95,
        )
    )
    extraction.judicial_events.append(
        JudicialEvent(
            event_type="执行",
            title="被执行并限制高消费",
            description="仍处于执行状态",
            severity=88,
        )
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "高风险"
    assert result.risk_score is not None and result.risk_score >= 60
    assert result.credit_judgment["grade"] == "高"


def test_all_formal_scored_levels_are_three_tier_only():
    scenarios = [verified_extraction()]

    medium = verified_extraction()
    medium.judicial_events.append(
        JudicialEvent(
            event_type="诉讼",
            title="重大诉讼",
            description="尚未解决",
            amount=2000,
            amount_unit="万元",
            severity=70,
        )
    )
    scenarios.append(medium)

    high = verified_extraction()
    high.annual_facts.extend(
        [
            AnnualFact(year=2024, metric="归母净利润", value="亏损3亿元"),
            AnnualFact(year=2025, metric="归母净利润", value="亏损5亿元"),
            AnnualFact(year=2025, metric="资产负债率", value="82%"),
        ]
    )
    scenarios.append(high)

    levels = {calculate_risk(item).risk_level for item in scenarios}
    assert levels <= {"低风险", "中风险", "高风险"}
    assert {"低风险", "中风险", "高风险"} <= levels


def test_single_active_dishonesty_record_is_high_not_diluted_low():
    extraction = verified_extraction()
    extraction.risk_signals.append(
        RiskSignal(category="司法", title="列入失信名单", description="当前仍为失信被执行状态", severity=92, confidence=0.95)
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "高风险"
    assert result.risk_score is not None and result.risk_score >= 60


def test_active_equity_freeze_is_at_least_medium():
    extraction = verified_extraction()
    extraction.risk_signals.append(
        RiskSignal(category="股权", title="股权冻结", description="控股股东股份仍处于冻结状态", severity=76, confidence=0.95)
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "中风险"
    assert result.risk_score is not None and 40 <= result.risk_score < 60


def test_adverse_audit_opinion_from_annual_facts_is_high():
    extraction = verified_extraction()
    extraction.annual_facts.append(
        AnnualFact(year=2025, metric="审计意见", value="会计师出具无法表示意见", confidence=0.98)
    )
    result = calculate_risk(extraction)
    assert result.risk_level == "高风险"
    assert result.risk_score is not None and result.risk_score >= 60
