
from pathlib import Path

from web_app.report_service import _enrich_profile_fields
from web_app.schemas import CompanyProfile, FieldEvidence, Relationship, ResearchExtraction, SearchSource

ROOT = Path(__file__).resolve().parents[1]


def test_profile_enrichment_reconciles_missing_fields():
    extraction = ResearchExtraction(
        profile=CompanyProfile(company_name="测试股份有限公司", stock_code="600000"),
        relationships=[
            Relationship(
                source_name="测试股份有限公司", target_name="测试控股集团有限公司",
                relation_type="控股股东", source_ids=["R1"], confidence=.96,
            )
        ],
        missing_fields=["控股股东", "统一社会信用代码", "企业状态"],
        identity_verified=True,
    )
    source = SearchSource(
        id="R1", title="公司档案", url="https://example.com",
        text="统一社会信用代码：91310000123456789A；企业状态：存续；控股股东：测试控股集团有限公司。",
        reliability=.95, fetched=True,
    )
    result = _enrich_profile_fields(extraction, [source])
    assert result.profile.controlling_shareholder == "测试控股集团有限公司"
    assert result.profile.credit_code == "91310000123456789A"
    assert result.profile.company_status == "存续"
    assert "控股股东" not in result.missing_fields
    assert "统一社会信用代码" not in result.missing_fields
    assert "企业状态" not in result.missing_fields


def test_frontend_uses_layered_graph_and_no_all_risk_bucket():
    html = (ROOT / "frontend/report.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend/report.js").read_text(encoding="utf-8")
    assert "低风险" in html
    assert "creditJudgmentPanel" in html
    assert "全部事项" not in html
    assert 'data-filter="equity"' in html
    assert 'data-filter="investment"' in html
    assert 'data-filter="trade"' in html
    assert 'data-filter="guarantee"' in html
    assert "layoutLayeredOverview" in js
    assert "report-progress-nav" in html
