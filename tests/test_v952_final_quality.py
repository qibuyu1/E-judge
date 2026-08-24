from pathlib import Path

from web_app.query_utils import parse_entity_query
from web_app.report_service import _enrich_profile_fields, _risk_evidence_support
from web_app.risk_engine import calculate_risk
from web_app.schemas import (
    AnnualFact, CompanyProfile, FieldEvidence, IdentityResolution, JudicialEvent,
    ResearchExtraction, RiskSignal, SearchSource,
)


def issuer(name="精校制造股份有限公司"):
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name=name,
            short_name="精校制造",
            stock_code="600001",
            credit_code="91310000000000000X",
            legal_person="张三",
            registered_address="上海市浦东新区",
            registered_capital="10亿元",
            company_status="存续",
            industry="制造业",
            controlling_shareholder="精校控股集团有限公司",
            actual_controller="某市国资委",
            field_evidence={"company_name": FieldEvidence(source_ids=["ID"], confidence=.98)},
        ),
        identity_verified=True,
        overall_confidence=.94,
        annual_facts=[
            AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", confidence=.96),
            AnnualFact(year=2025, metric="归母净利润", value="8.6亿元", confidence=.96),
            AnnualFact(year=2025, metric="经营活动现金流", value="正向净流入12亿元", confidence=.96),
        ],
    )


def src(sid, text, *, url="https://news.example.com/a", reliability=.9, title="公开资料"):
    return SearchSource(
        id=sid, title=title, url=url, snippet=text, text=text, reliability=reliability,
        search_score=.9, fetched=True, search_purpose="风险核验",
    )


def test_unrelated_company_direct_risk_is_rejected():
    identity = IdentityResolution(
        company_name="精校制造股份有限公司", short_name="精校制造", stock_code="600001",
        verified=True, confidence=.98,
    )
    source = src(
        "S1",
        "精校制造股份有限公司发布年度报告。另据公开信息，其他科技股份有限公司被列为失信被执行人，执行案号（2026）沪01执1号。",
    )
    supported, _ = _risk_evidence_support(
        "其他科技股份有限公司被列为失信被执行人", ["S1"], {"S1": source}, identity,
    )
    assert not supported


def test_one_vague_page_cannot_create_high_risk():
    identity = IdentityResolution(company_name="精校制造股份有限公司", short_name="精校制造", verified=True, confidence=.98)
    source = src("S1", "精校制造股份有限公司存在需要关注的债务风险，具体情况尚未披露。", reliability=.86)
    supported, cap = _risk_evidence_support(
        "精校制造股份有限公司债务违约", ["S1"], {"S1": source}, identity,
    )
    assert (not supported) or (cap is not None and cap <= 44)


def test_concrete_official_default_retains_high_risk_evidence():
    identity = IdentityResolution(company_name="精校制造股份有限公司", short_name="精校制造", verified=True, confidence=.98)
    source = src(
        "S1",
        "精校制造股份有限公司关于到期债务未兑付的公告。2026年6月30日到期的2.3亿元债务尚未清偿，公告编号2026-037。",
        url="https://www.cninfo.com.cn/new/disclosure/detail/1",
        reliability=.96,
        title="精校制造股份有限公司关于到期债务未兑付的公告",
    )
    supported, cap = _risk_evidence_support(
        "精校制造股份有限公司到期债务尚未清偿2.3亿元，公告编号2026-037", ["S1"], {"S1": source}, identity,
    )
    assert supported and cap is None


def test_risk_outputs_are_three_tier_and_not_collapsed_to_68_82():
    clean = calculate_risk(issuer())

    medium_case = issuer()
    medium_case.judicial_events.append(JudicialEvent(
        event_type="诉讼", title="重大合同诉讼",
        description="精校制造股份有限公司涉及合同纠纷，案件正在审理",
        amount=3000, amount_unit="万元", severity=70,
    ))
    medium = calculate_risk(medium_case)

    high_case = issuer()
    high_case.risk_signals.extend([
        RiskSignal(category="偿债", title="债务违约", description="精校制造股份有限公司2.3亿元到期债务尚未清偿，公告编号2026-037", severity=94, confidence=.96),
        RiskSignal(category="司法", title="失信被执行人", description="精校制造股份有限公司被列为失信被执行人，执行案号（2026）沪01执1号", severity=96, confidence=.96),
    ])
    high = calculate_risk(high_case)

    assert [clean.risk_level, medium.risk_level, high.risk_level] == ["低风险", "中风险", "高风险"]
    scores = {clean.risk_score, medium.risk_score, high.risk_score}
    assert len(scores) == 3
    assert clean.risk_score not in {68.0, 82.0}
    assert medium.risk_score not in {68.0, 82.0}


def test_profile_backfill_supports_common_labels_and_stops_at_next_field():
    extraction = issuer("资料完整股份有限公司")
    for field in (
        "legal_person", "registered_address", "registered_capital", "company_status",
        "established_date", "industry", "business_scope",
    ):
        setattr(extraction.profile, field, None)
    source = src(
        "P1",
        "资料完整股份有限公司；法人代表：李明；公司住所：北京市海淀区知春路1号；"
        "注册资金：58000万元；登记状态：存续；成立时间：2001-03-18；"
        "行业：专用设备制造业；主营业务：工业装备的研发、生产和销售；控股股东：资料控股集团有限公司。",
        reliability=.94,
        title="资料完整股份有限公司公司资料",
    )
    source.search_purpose = "法定主体与工商档案"
    result = _enrich_profile_fields(extraction, [source])
    assert result.profile.legal_person == "李明"
    assert result.profile.registered_address == "北京市海淀区知春路1号"
    assert result.profile.registered_capital == "58000万元"
    assert result.profile.company_status == "存续"
    assert "主营业务" not in (result.profile.registered_address or "")


def test_final_frontend_contracts():
    root = Path(__file__).resolve().parents[1]
    css = (root / "frontend/styles.css").read_text("utf-8")
    query_js = (root / "frontend/query.js").read_text("utf-8")
    report_js = (root / "frontend/report.js").read_text("utf-8")
    index = (root / "frontend/index.html").read_text("utf-8")
    final = css.split("v9.5.2 FINAL PROPORTION + READABILITY PASS", 1)[1]
    assert "52px" in final
    assert "width:350px!important" in final
    assert "min-height:500px!important" in final
    assert "#ad4852" in final and "#751823" in final
    assert "chain-last-query" not in query_js
    assert 'autocomplete="new-password"' in index and 'readonly="readonly"' in index
    assert "crypto?.randomUUID" in query_js
    assert "shortRiskLabel" in report_js
    assert "baseRadius = total <= 16 ? 250 : 245" in report_js
