from pathlib import Path

from web_app.deepseek_client import _normalize_payload
from web_app.listed_relations_resolver import _bundle_from_rows
from web_app.risk_engine import calculate_risk
from web_app.schemas import CompanyProfile, FieldEvidence, ResearchExtraction, ResearchExtraction as ExtractionType


def test_deepseek_payload_normalizes_common_json_variants():
    payload = {
        "profile": {
            "company_name": "测试股份有限公司",
            "main_products": "钢材、板材",
            "field_evidence": {"company_name": {"source_ids": "S1", "confidence": "95%"}},
        },
        "relationships": [{
            "source": "股东甲有限公司", "target": "测试股份有限公司", "type": "股东",
            "share_ratio": "57.13%", "source_ids": "S2", "confidence": "0.91", "risk_level": "低风险",
        }],
        "judicial_events": [{"type": "诉讼", "title": "合同纠纷", "severity": "70", "source_ids": "S3"}],
        "annual_facts": [{"year": "2025年", "metric": "营业收入", "value": 100, "source_ids": "S4"}],
        "risk_signals": [{"category": "担保", "title": "对外担保", "severity": "中风险", "source_ids": "S5"}],
        "overall_confidence": "0.88",
        "identity_verified": "true",
    }
    normalized = _normalize_payload(ExtractionType, payload)
    model = ResearchExtraction.model_validate(normalized)
    assert model.profile.main_products == ["钢材", "板材"]
    assert model.profile.field_evidence["company_name"].source_ids == ["S1"]
    assert model.relationships[0].share_ratio == 57.13
    assert model.relationships[0].risk_level == "low"
    assert model.annual_facts[0].year == 2025
    assert model.identity_verified is True


def test_structured_shareholder_rows_create_real_graph_relations():
    rows = [
        {"SECURITY_CODE": "600001", "END_DATE": "2025-12-31", "HOLDER_RANK": i,
         "HOLDER_NAME": f"股东{i}有限公司", "HOLD_RATIO": str(20 - i)}
        for i in range(1, 11)
    ]
    source, relationships = _bundle_from_rows(
        "600001", "测试股份有限公司", "RPT_DMSK_HOLDERS", "前十大股东结构化档案", rows, "2025-12-31"
    )
    assert source is not None
    assert len(relationships) == 10
    assert relationships[0].target_name == "测试股份有限公司"
    extraction = ResearchExtraction(
        profile=CompanyProfile(
            company_name="测试股份有限公司", stock_code="600001",
            field_evidence={"company_name": FieldEvidence(source_ids=[source.id], confidence=.95)},
        ),
        relationships=relationships,
        identity_verified=True,
        overall_confidence=.9,
    )
    risk = calculate_risk(extraction)
    assert risk.related_company_count == 10


def test_frontend_hides_raw_evidence_ids_and_technical_errors():
    root = Path(__file__).resolve().parents[1]
    app = (root / "frontend/report.js").read_text("utf-8")
    html = (root / "frontend/report.html").read_text("utf-8")
    assert "source.id} ·" not in app
    assert "DeepSeekResponseError" not in html
    assert "risk-pyramid-shape" in html
    assert "每一项风险事实均对应" not in html
    assert "横向滑动查看" not in html

from web_app.listed_profile_resolver import ListedCompanyProfile, _fill_profile_from_visible_text
from web_app.schemas import SearchSource
from web_app.search_service import merge_sources


def test_percentage_confidence_is_scaled_to_zero_one():
    payload = {
        "profile": {
            "company_name": "测试股份有限公司",
            "field_evidence": {"company_name": {"source_ids": "S1", "confidence": "95%"}},
        },
        "identity_verified": True,
    }
    normalized = _normalize_payload(ExtractionType, payload)
    assert normalized["profile"]["field_evidence"]["company_name"]["confidence"] == 0.95


def test_structured_sources_with_same_url_are_not_deduplicated():
    common = "https://basic.example.com/600282/company.html"
    profile = SearchSource(
        id="PROFILE-1", title="公司档案", url=common, snippet="公司名称：测试股份有限公司",
        text="公司名称：测试股份有限公司", source_type="structured_listed_company_profile",
        provider="结构化档案", search_query="600282", search_purpose="上市公司结构化档案",
        search_score=1.0, reliability=.95, fetched=True,
    )
    subsidiaries = SearchSource(
        id="SUBS-1", title="参控股公司", url=common, snippet="子公司：甲有限公司",
        text="子公司：甲有限公司", source_type="structured_listed_subsidiaries",
        provider="结构化关系", search_query="600282", search_purpose="子公司与对外投资结构化档案",
        search_score=1.0, reliability=.9, fetched=True,
    )
    merged = merge_sources([profile], [subsidiaries])
    assert {item.id for item in merged} == {"PROFILE-1", "SUBS-1"}


def test_f10_narrative_fallback_recovers_core_company_fields():
    page = """
    <title>南钢股份(600282) 公司资料</title>
    <div>公司名称：南京钢铁股份有限公司</div>
    <div>法人代表：黄一新</div>
    <div>控股股东：南京南钢钢铁联合有限公司 (持有股份比例59.10%)</div>
    <div>产品名称：专用板材 、特钢长材 、建筑螺纹</div>
    <p>南京钢铁股份有限公司(以下简称公司或本公司)成立日期：1999-03-18。
    本公司统一社会信用代码91320000714085405J;注册地和总部地址均为:江苏省南京市六合区卸甲甸。
    注册资本为616,509.1011万元。本公司的母公司为南京南钢钢铁联合有限公司，
    本公司的实际控制人为中国中信集团有限公司。主要经营活动：钢铁冶炼、钢材轧制。</p>
    """
    profile = _fill_profile_from_visible_text(ListedCompanyProfile(stock_code="600282"), page)
    assert profile.company_name == "南京钢铁股份有限公司"
    assert profile.credit_code == "91320000714085405J"
    assert profile.legal_person == "黄一新"
    assert profile.registered_address.startswith("江苏省南京市六合区卸甲甸")
    assert "616,509.1011" in profile.registered_capital
    assert profile.established_date == "1999-03-18"
    assert profile.controlling_shareholder == "南京南钢钢铁联合有限公司"
    assert profile.actual_controller == "中国中信集团有限公司"
    assert profile.main_products[:3] == ["专用板材", "特钢长材", "建筑螺纹"]

from web_app.listed_relations_resolver import _parse_subsidiary_rows


def test_f10_subsidiary_table_recovers_many_relationships():
    text = "\n".join(
        f"测试子公司{i}有限公司 | 子公司 | {100 - i:.2f}% | {i}亿元 | 是"
        for i in range(1, 21)
    )
    rows = _parse_subsidiary_rows(text, "测试股份有限公司", max_rows=36)
    assert len(rows) == 20
    assert rows[0][0] == "测试子公司1有限公司"
    assert rows[0][1] == "控股子公司"
    assert rows[0][2] == 99.0

from web_app.evidence_parser import merge_extractions


def test_structured_profile_cannot_be_overwritten_by_model_prose():
    primary = ResearchExtraction(
        profile=CompanyProfile(
            company_name="南京钢铁股份有限公司",
            controlling_shareholder="南京南钢钢铁联合有限公司",
            actual_controller="中国中信集团有限公司",
            field_evidence={
                "company_name": FieldEvidence(source_ids=["PROFILE-ABC"], confidence=.88),
                "controlling_shareholder": FieldEvidence(source_ids=["PROFILE-ABC"], confidence=.88),
                "actual_controller": FieldEvidence(source_ids=["PROFILE-ABC"], confidence=.88),
            },
        ), identity_verified=True,
    )
    secondary = ResearchExtraction(
        profile=CompanyProfile(
            controlling_shareholder="和实际控制人及其关联人提供担保",
            actual_controller="变更为中信集团",
            field_evidence={
                "controlling_shareholder": FieldEvidence(source_ids=["WEB-1"], confidence=.99),
                "actual_controller": FieldEvidence(source_ids=["WEB-1"], confidence=.99),
            },
        ), identity_verified=True,
    )
    merged = merge_extractions(primary, secondary)
    assert merged.profile.controlling_shareholder == "南京南钢钢铁联合有限公司"
    assert merged.profile.actual_controller == "中国中信集团有限公司"
