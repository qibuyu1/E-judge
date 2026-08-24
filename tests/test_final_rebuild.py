from pathlib import Path

from web_app.evidence_parser import deterministic_identity
from web_app.query_utils import parse_entity_query
from web_app.report_service import _build_decision_cards, _stock_hint_source
from web_app.risk_engine import build_graph, calculate_risk
from web_app.schemas import CompanyProfile, FieldEvidence, Relationship, ResearchExtraction, RiskSignal
from web_app.stock_resolver import StockHint
from web_app.search_service import build_identity_plan
from web_app.schemas import SearchSource


def test_stock_code_with_chinese_prefix_is_accepted():
    assert parse_entity_query("股权代码：600282").normalized == "600282"
    assert parse_entity_query("600282.SH").normalized == "600282"


def test_structured_stock_master_verifies_without_tavily_identity():
    entity = parse_entity_query("600282")
    hint = StockHint(
        code="600282",
        short_name="南钢股份",
        company_name="南京钢铁股份有限公司",
        exchange="上海证券交易所",
        provider="本地证券主数据缓存",
        source_url="https://www.sse.com.cn/",
        reliability=0.96,
    )
    identity = deterministic_identity(entity, [_stock_hint_source(hint)], [hint.short_name])
    assert identity.verified is True
    assert identity.company_name == "南京钢铁股份有限公司"
    assert identity.stock_code == "600282"



def test_unknown_relationship_gets_semantic_risk_color_band():
    extraction = ResearchExtraction(
        profile=CompanyProfile(
            company_name="目标企业",
            stock_code="600001",
            field_evidence={"company_name": FieldEvidence(source_ids=["S1"], confidence=.9)},
        ),
        relationships=[
            Relationship(
                source_name="目标企业", target_name="控股股东A", relation_type="控股股东",
                source_ids=["S1"], confidence=.9, risk_level="unknown",
            ),
            Relationship(
                source_name="目标企业", target_name="担保方B", relation_type="对外担保",
                source_ids=["S2"], confidence=.9, risk_level="unknown",
            ),
        ],
        identity_verified=True,
        overall_confidence=.8,
    )
    graph = build_graph(extraction, calculate_risk(extraction))
    by_name = {node.name: node for node in graph.nodes}
    assert by_name["控股股东A"].risk == "low"
    assert by_name["担保方B"].risk == "medium"


def test_decision_cards_pair_risk_impact_and_action():
    extraction = ResearchExtraction(
        profile=CompanyProfile(
            company_name="目标企业",
            field_evidence={"company_name": FieldEvidence(source_ids=["S1"], confidence=.9)},
        ),
        risk_signals=[
            RiskSignal(
                category="股权质押", title="控股股东高比例质押", description="公开公告披露高比例质押。",
                severity=80, source_ids=["S2"], confidence=.9,
            )
        ],
        missing_fields=["website", "controlling_shareholder"],
        identity_verified=True,
        overall_confidence=.8,
    )
    risk = calculate_risk(extraction)
    cards = _build_decision_cards(extraction, risk)
    assert cards[0]["title"] == "控股股东高比例质押"
    assert "控制" in cards[0]["impact"]
    assert "穿透" in cards[0]["action"]
    assert any("官方网站" in card["summary"] for card in cards if card["label"] == "数据缺口")


def test_frontend_uses_custom_dialog_and_new_graph_labels():
    root = Path(__file__).resolve().parents[1]
    app = (root / "frontend/report.js").read_text("utf-8")
    html = (root / "frontend/report.html").read_text("utf-8")
    assert "alert(" not in app
    assert "20260729.3" in app and "20260729.3" in html
    assert "edge-label-wrap" in app
    assert 'id="decisionCards"' in html
    assert 'id="riskPyramid"' in html
    assert '<mark' not in app
    assert '再按缺失项精准补查' not in html
    assert 'TAVILY_CREDIT_BUDGET' not in app


def test_stock_identity_plan_is_valid_and_uses_general_topic():
    plans = build_identity_plan(parse_entity_query("600282"))
    assert len(plans) == 2
    assert all(plan.topic == "general" for plan in plans)
    assert any(plan.include_domains for plan in plans)


def test_structured_short_name_is_a_formal_stock_identity_and_can_upgrade_to_legal_name():
    entity = parse_entity_query("601398")
    hint = StockHint(
        code="601398", short_name="工商银行", company_name="",
        exchange="上海证券交易所", provider="证券主数据", source_url="https://www.sse.com.cn/", reliability=.9,
    )
    structured = _stock_hint_source(hint)
    initial = deterministic_identity(entity, [structured], [hint.short_name])
    assert initial.verified is True
    assert initial.company_name == "工商银行"

    filing = SearchSource(
        id="S-OFFICIAL", title="中国工商银行股份有限公司年度报告（股票代码601398）",
        url="https://www.sse.com.cn/example",
        snippet="证券代码：601398 公司全称：中国工商银行股份有限公司",
        text="证券代码：601398 公司全称：中国工商银行股份有限公司",
        source_type="web", provider="Tavily", search_query="601398",
        search_purpose="交易所与法定披露", search_score=.95, reliability=.98, fetched=True,
    )
    upgraded = deterministic_identity(entity, [structured, filing], [hint.short_name])
    assert upgraded.verified is True
    assert upgraded.company_name == "中国工商银行股份有限公司"
    assert "S-OFFICIAL" in upgraded.source_ids


def test_stock_resolver_has_no_hardcoded_company_records():
    root = Path(__file__).resolve().parents[1]
    source = (root / "web_app/stock_resolver.py").read_text("utf-8")
    assert "_BOOTSTRAP_MASTER" not in source
    assert "南京钢铁股份有限公司" not in source


def test_task_manager_does_not_abort_research_with_global_wait_for():
    root = Path(__file__).resolve().parents[1]
    source = (root / "web_app/task_manager.py").read_text("utf-8")
    assert "asyncio.wait_for(" not in source
    assert "RESEARCH_DEADLINE_EXCEEDED" not in source


def test_stock_pipeline_has_no_provisional_or_partial_identity_fallback():
    root = Path(__file__).resolve().parents[1]
    resolver = (root / "web_app/stock_resolver.py").read_text("utf-8")
    report = (root / "web_app/report_service.py").read_text("utf-8")
    assert "def provisional_stock_hint" not in resolver
    assert "按部分报告继续" not in report
    assert 'code="IDENTITY_NOT_VERIFIED"' in report
    assert "partial" not in report.lower()

def test_stock_issuer_legal_name_upgrade_does_not_accept_subsidiary():
    from web_app.evidence_parser import deterministic_identity
    from web_app.query_utils import parse_entity_query
    from web_app.schemas import SearchSource

    entity = parse_entity_query("600282")
    sources = [
        SearchSource(
            id="MASTER", title="证券主数据", url="https://quote.example/600282",
            snippet="股票代码：600282；证券简称：南钢股份。", text="股票代码：600282；证券简称：南钢股份。",
            source_type="structured_stock_master", provider="test", search_score=1.0,
            reliability=0.92, fetched=True,
        ),
        SearchSource(
            id="OFFICIAL", title="南京钢铁股份有限公司2025年年度报告 证券代码600282",
            url="https://www.sse.com.cn/report.pdf",
            snippet="公司全称：南京钢铁股份有限公司；证券代码：600282；证券简称：南钢股份。宣布其控股子公司河南南钢合力新材料股份有限公司开展业务。",
            text="公司全称：南京钢铁股份有限公司；证券代码：600282；证券简称：南钢股份。宣布其控股子公司河南南钢合力新材料股份有限公司开展业务。",
            source_type="web_search", provider="test", search_score=.98,
            reliability=.98, fetched=True,
        ),
    ]
    identity = deterministic_identity(entity, sources, ["南钢股份"])
    assert identity.verified is True
    assert identity.company_name == "南京钢铁股份有限公司"
    assert "河南南钢合力新材料股份有限公司" not in identity.company_name
