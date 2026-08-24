import asyncio
from types import SimpleNamespace

from web_app import report_service, search_service
from web_app.schemas import (
    CompanyProfile,
    FieldEvidence,
    IdentityResolution,
    NarrativeItem,
    NarrativeResult,
    ResearchExtraction,
    SearchSource,
)
from web_app.search_service import SearchPlan, SearchRunResult, TavilySearchClient


def _source(source_id: str, purpose: str = "企业身份核验") -> SearchSource:
    return SearchSource(
        id=source_id,
        title="南京钢铁股份有限公司公告",
        url=f"https://www.sse.com.cn/{source_id}",
        snippet="证券代码600282，南京钢铁股份有限公司，法定代表人黄一新。",
        text="证券代码600282。公司全称南京钢铁股份有限公司。统一社会信用代码91320100714085405J。法定代表人黄一新。",
        provider="tavily",
        search_query="600282 证券代码 公司全称",
        search_purpose=purpose,
        search_score=0.95,
        reliability=0.97,
        fetched=True,
    )


def test_tavily_response_is_parsed(monkeypatch):
    fake_settings = SimpleNamespace(
        tavily_ready=True,
        tavily_base_url="https://api.tavily.com",
        tavily_api_key="tvly-test",
        tavily_timeout_seconds=10,
        tavily_include_raw_content="markdown",
        tavily_country="china",
        tavily_max_chars_per_source=14000,
        app_version="4.0.0",
    )
    monkeypatch.setattr(search_service, "settings", fake_settings)

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "results": [
                    {
                        "title": "南京钢铁股份有限公司公告",
                        "url": "https://www.sse.com.cn/example",
                        "content": "证券代码600282，公司全称南京钢铁股份有限公司",
                        "raw_content": "证券代码600282。公司全称南京钢铁股份有限公司。",
                        "score": 0.91,
                    }
                ],
                "response_time": "1.2",
                "usage": {"credits": 1},
                "request_id": "req-test",
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(search_service.httpx, "AsyncClient", FakeAsyncClient)
    client = TavilySearchClient()
    plan = SearchPlan("600282 证券代码 公司全称", "企业身份核验", "fast", 5, "finance")
    run = asyncio.run(client._search(plan))
    assert run.credits == 1
    assert run.request_ids == ["req-test"]
    assert len(run.sources) == 1
    assert run.sources[0].provider == "tavily"
    assert run.sources[0].fetched is True
    assert run.sources[0].search_score == 0.91


def test_full_research_pipeline_with_mocked_apis(monkeypatch, tmp_path):
    identity_source = _source("SIDENTITY", "法定主体与工商档案")
    detail_source = _source("SDETAIL", "股权控制与投资关系")
    risk_source = _source("SRISK", "司法监管与合规风险")

    class FakeSearch:
        async def search_identity(self, entity, advanced=False, name_hints=()):
            return SearchRunResult(sources=[identity_source], query_count=1, credits=1, response_seconds=.2)
        async def search_details(self, entity, company_name, aliases=(), credit_budget=10):
            return SearchRunResult(sources=[detail_source, risk_source], query_count=5, credits=8, response_seconds=.8)
        async def extract_research_sources(self, sources, company_name):
            return SearchRunResult(sources=list(sources), credits=2, response_seconds=.3, provider="Tavily Search + Extract")
        async def search_followup(self, *args, **kwargs):
            return SearchRunResult()

    profile_extraction = ResearchExtraction(
        profile=CompanyProfile(
            company_name="南京钢铁股份有限公司", short_name="南钢股份",
            credit_code="91320100714085405J", stock_code="600282", legal_person="黄一新",
            registered_address="南京市六合区卸甲甸", registered_capital="616509.1011万元人民币",
            company_status="存续", established_date="1999-03-18", industry="黑色金属冶炼和压延加工业",
            main_products=["中厚板", "特钢长材"],
            field_evidence={
                "company_name": FieldEvidence(source_ids=["SIDENTITY"], confidence=.98),
                "stock_code": FieldEvidence(source_ids=["SIDENTITY"], confidence=.98),
                "credit_code": FieldEvidence(source_ids=["SIDENTITY"], confidence=.96),
                "legal_person": FieldEvidence(source_ids=["SDETAIL"], confidence=.9),
                "registered_address": FieldEvidence(source_ids=["SDETAIL"], confidence=.9),
                "registered_capital": FieldEvidence(source_ids=["SDETAIL"], confidence=.9),
                "company_status": FieldEvidence(source_ids=["SDETAIL"], confidence=.9),
                "established_date": FieldEvidence(source_ids=["SDETAIL"], confidence=.9),
                "industry": FieldEvidence(source_ids=["SDETAIL"], confidence=.9),
            },
        ), overall_confidence=.92, identity_verified=True,
    )

    class FakeDeepSeek:
        def reset_usage(self): pass
        def usage_snapshot(self): return {"calls": 4, "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        async def resolve_identity(self, query, sources):
            return IdentityResolution(company_name="南京钢铁股份有限公司", short_name="南钢股份", stock_code="600282", credit_code="91320100714085405J", aliases=["南钢股份"], source_ids=["SIDENTITY"], confidence=.98, verified=True)
        async def extract_profile(self, query, sources): return profile_extraction
        async def extract_relationships(self, query, sources): return ResearchExtraction(profile=CompanyProfile(stock_code="600282", field_evidence={"stock_code": FieldEvidence(source_ids=["SIDENTITY"], confidence=.98)}), identity_verified=True, overall_confidence=.85)
        async def extract_risks(self, query, sources): return ResearchExtraction(profile=CompanyProfile(stock_code="600282", field_evidence={"stock_code": FieldEvidence(source_ids=["SIDENTITY"], confidence=.98)}), identity_verified=True, overall_confidence=.8)
        async def analyze_decisions(self, company_name, packet, allowed_source_ids):
            from web_app.schemas import DecisionAnalysis, DecisionCard
            return DecisionAnalysis(cards=[DecisionCard(severity="low", label="公开信息初筛", title="当前未确认重大公开风险", summary="已完成公开来源核验。", impact="仍需结合内部数据判断。", action="继续核验财务、流水和担保材料。", source_ids=["SDETAIL"])])

    fake_settings = SimpleNamespace(
        deepseek_ready=True, tavily_ready=True, stock_resolver_enabled=False,
        listed_profile_resolver_enabled=False, tavily_max_credits_per_research=16,
        adaptive_followup_enabled=False, enable_pro_decision_analysis=True,
        fast_model="deepseek-v4-flash", review_model="deepseek-v4-pro", api_keys=["x"],
    )
    monkeypatch.setattr(report_service, "settings", fake_settings)
    monkeypatch.setattr(report_service, "tavily_search_client", FakeSearch())
    monkeypatch.setattr(report_service, "deepseek_client", FakeDeepSeek())
    monkeypatch.setattr(report_service.research_cache, "set", lambda query, report: None)

    report = asyncio.run(report_service.research_company("600282", force_refresh=True))
    assert report["company"]["name"] == "南京钢铁股份有限公司"
    assert report["company"]["credit_code"] == "91320100714085405J"
    assert report["summary"]["identity_verified"] is True
    assert report["summary"]["source_count"] == 3
    assert report["decision_cards"][0]["action"].startswith("继续核验")

def test_fast_search_does_not_send_country(monkeypatch):
    captured = []
    fake_settings = SimpleNamespace(
        tavily_ready=True,
        tavily_base_url="https://api.tavily.com",
        tavily_api_key="tvly-test",
        tavily_timeout_seconds=10,
        tavily_include_raw_content="markdown",
        tavily_country="china",
        tavily_max_chars_per_source=14000,
        app_version="4.0.1",
    )
    monkeypatch.setattr(search_service, "settings", fake_settings)

    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"results": [], "response_time": "0.2", "usage": {"credits": 1}, "request_id": "r"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs):
            captured.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(search_service.httpx, "AsyncClient", FakeAsyncClient)
    client = TavilySearchClient()
    asyncio.run(client._search(SearchPlan("中国企业 600282", "企业身份核验", "fast", 5, "general")))
    assert "country" not in captured[0]


def test_basic_search_can_send_country(monkeypatch):
    captured = []
    fake_settings = SimpleNamespace(
        tavily_ready=True,
        tavily_base_url="https://api.tavily.com",
        tavily_api_key="tvly-test",
        tavily_timeout_seconds=10,
        tavily_include_raw_content="markdown",
        tavily_country="china",
        tavily_max_chars_per_source=14000,
        app_version="4.0.1",
    )
    monkeypatch.setattr(search_service, "settings", fake_settings)

    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"results": [], "response_time": "0.2", "usage": {"credits": 1}, "request_id": "r"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs):
            captured.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(search_service.httpx, "AsyncClient", FakeAsyncClient)
    client = TavilySearchClient()
    asyncio.run(client._search(SearchPlan("中国企业 南京钢铁", "企业详情", "basic", 5, "general")))
    assert captured[0]["country"] == "china"


def test_tavily_400_retries_with_minimal_payload(monkeypatch):
    captured = []
    fake_settings = SimpleNamespace(
        tavily_ready=True,
        tavily_base_url="https://api.tavily.com",
        tavily_api_key="tvly-test",
        tavily_timeout_seconds=10,
        tavily_include_raw_content=False,
        tavily_country="china",
        tavily_max_chars_per_source=7000,
        app_version="4.2.0",
    )
    monkeypatch.setattr(search_service, "settings", fake_settings)

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "bad request" if status_code == 400 else ""
        def json(self):
            if self.status_code == 400:
                return {"detail": "invalid parameter combination"}
            return {"results": [], "response_time": "0.1", "usage": {"credits": 1}, "request_id": "ok"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs):
            captured.append(kwargs["json"])
            return FakeResponse(400 if len(captured) == 1 else 200)

    monkeypatch.setattr(search_service.httpx, "AsyncClient", FakeAsyncClient)
    client = TavilySearchClient()
    run = asyncio.run(client._search(SearchPlan("600282 公司全称", "身份", "fast", 7, "finance")))
    assert run.request_ids == ["ok"]
    assert len(captured) == 2
    assert captured[1]["topic"] == "general"
    assert captured[1]["search_depth"] == "basic"
    assert "country" not in captured[1]


def test_run_plans_never_skips_required_queries_for_local_credit_budget(monkeypatch):
    fake_settings = SimpleNamespace(
        tavily_ready=True,
        tavily_base_url="https://api.tavily.com",
        tavily_api_key="tvly-test",
        tavily_timeout_seconds=10,
        tavily_include_raw_content=False,
        tavily_country="",
        tavily_max_chars_per_source=7000,
        tavily_min_score=0.0,
        minimum_source_relevance=-100,
        tavily_max_sources=30,
        app_version="8.2.0",
    )
    monkeypatch.setattr(search_service, "settings", fake_settings)
    calls = []

    async def fake_search(self, plan, client=None):
        calls.append(plan.purpose)
        return SearchRunResult(
            sources=[SearchSource(
                id=f"S{len(calls)}", title=plan.purpose, url=f"https://example.com/{len(calls)}",
                snippet="目标企业 公司 股东 风险", text="目标企业 公司 股东 风险",
                provider="tavily", search_query=plan.query, search_purpose=plan.purpose,
                search_score=.9, reliability=.8, fetched=True,
            )],
            query_count=1, credits=plan.estimated_credits,
        )

    monkeypatch.setattr(TavilySearchClient, "_search", fake_search)
    client = TavilySearchClient()
    entity = report_service.parse_entity_query("南京钢铁股份有限公司")
    plans = [
        SearchPlan("a", "工商", "basic"),
        SearchPlan("b", "股权", "advanced"),
        SearchPlan("c", "司法", "advanced"),
    ]
    run = asyncio.run(client.run_plans(plans, entity, ["南京钢铁股份有限公司"], credit_budget=0))
    assert calls == ["工商", "股权", "司法"]
    assert run.query_count == 3
    assert run.credits == 5


def test_fast_model_is_pinned_to_flash_even_if_old_env_requests_pro():
    from web_app.config import settings
    assert settings.fast_model == "deepseek-v4-flash"
