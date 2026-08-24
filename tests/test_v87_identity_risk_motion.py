from pathlib import Path

from web_app.evidence_parser import deterministic_identity, explicit_risk_signals, parse_local_extraction, select_profile_sources
from web_app.query_utils import parse_entity_query
from web_app.risk_engine import calculate_risk
from web_app.schemas import FieldEvidence, IdentityResolution, ResearchExtraction, RiskSignal, SearchSource


def source(sid: str, text: str, purpose: str = "法定主体与工商档案", reliability: float = 0.9):
    return SearchSource(
        id=sid,
        title=text[:80],
        url=f"https://example.com/{sid}",
        snippet=text,
        text=text,
        source_type="tavily_search_result",
        provider="tavily",
        search_purpose=purpose,
        search_score=0.9,
        reliability=reliability,
        fetched=True,
    )


def test_full_company_name_is_immutable_identity_anchor():
    entity = parse_entity_query("泰禾集团股份有限公司")
    sources = [
        source("S1", "泰禾集团股份有限公司关于逾期债务的公告。控股子公司福州某某有限公司法定代表人张三。", "债务违约与偿债风险"),
        source("S2", "另一家公司股份有限公司 法定代表人李四 注册资本1000万元。", "司法执行与监管风险"),
    ]
    identity = deterministic_identity(entity, sources)
    assert identity.verified
    assert identity.company_name == "泰禾集团股份有限公司"


def test_company_candidate_rejects_prose_prefix_and_generic_suffix():
    entity = parse_entity_query("泰禾集团股份有限公司")
    identity = deterministic_identity(entity, [source("S1", "因在泰禾集团股份有限公司发生债务逾期，公司被列为失信被执行人。")])
    assert identity.company_name == "泰禾集团股份有限公司"
    assert identity.company_name != "因在泰禾集团股份有限公司"
    assert identity.company_name != "股份有限公司"


def test_profile_sources_exclude_unrelated_relation_page_for_scalar_fields():
    entity = parse_entity_query("目标科技股份有限公司")
    identity = IdentityResolution(company_name="目标科技股份有限公司", source_ids=["S1"], confidence=0.95, verified=True)
    good = source("S1", "目标科技股份有限公司 法定代表人：王明；注册地址：北京市海淀区。")
    bad = source("S2", "目标科技股份有限公司投资了坏数据有限公司。坏数据有限公司 法定代表人：错误人；注册资本：999999万元。", "子公司与对外投资")
    scoped = select_profile_sources(entity, identity, [good, bad])
    extraction = parse_local_extraction(entity, identity, scoped, relation_sources=[good, bad])
    assert extraction.profile.legal_person == "王明"
    assert extraction.profile.legal_person != "错误人"


def test_deterministic_red_flags_force_high_credit_risk():
    entity = parse_entity_query("高风险集团股份有限公司")
    identity = IdentityResolution(company_name="高风险集团股份有限公司", source_ids=["S1"], confidence=0.95, verified=True)
    risk_source = source(
        "S1",
        "高风险集团股份有限公司关于逾期债务、被列失信被执行人的公告。截至报告期公司存在大额逾期债务，并被列为失信被执行人。",
        "债务违约与偿债风险",
        0.95,
    )
    signals = explicit_risk_signals(entity, identity, [risk_source])
    assert any("逾期" in s.title for s in signals)
    assert any("失信" in s.title for s in signals)
    extraction = ResearchExtraction(identity_verified=True, overall_confidence=0.95, risk_signals=signals)
    extraction.profile.company_name = identity.company_name
    extraction.profile.credit_code = "91110101MA0000000X"
    extraction.profile.legal_person = "张三"
    extraction.profile.registered_address = "北京市"
    extraction.profile.registered_capital = "10000万元"
    extraction.profile.company_status = "存续"
    extraction.profile.industry = "综合"
    extraction.profile.controlling_shareholder = "控股集团有限公司"
    extraction.profile.actual_controller = "实际控制人"
    result = calculate_risk(extraction)
    assert result.scorable
    assert result.risk_score is not None and result.risk_score >= 75
    assert result.risk_level == "高风险"


def test_frontend_return_query_motion_and_true_triangle():
    root = Path(__file__).resolve().parents[1]
    report_js = (root / "frontend/report.js").read_text("utf-8")
    query_js = (root / "frontend/query.js").read_text("utf-8")
    css = (root / "frontend/styles.css").read_text("utf-8")
    report_html = (root / "frontend/report.html").read_text("utf-8")
    assert '/?query=1' in report_js and '/?query=1' in report_html
    assert 'chain-return-query' in query_js
    assert 'activateReportMotion();' in report_js
    assert '.tier-critical{width:25%' in css
    assert '.tier-high{width:50%' in css
    assert '.tier-medium{width:75%' in css
    assert '.tier-low{width:100%' in css
    assert 'stroke-dashoffset:1200' in css
    assert 'bar-row.chart-enter' in css


def test_scalar_field_cleaning_rejects_prose_leakage():
    from web_app.evidence_parser import _clean_field_value
    assert _clean_field_value("actual_controller", "无犯罪记录证明") == ""
    assert _clean_field_value("controlling_shareholder", "、 实际") == ""
    assert _clean_field_value("industry", "发展、业务经营等情况分析变化的原因") == ""
    assert _clean_field_value("legal_person", "张三 注册资本：5000万元") == "张三"
