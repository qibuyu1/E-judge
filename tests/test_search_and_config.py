import pytest

from web_app.query_utils import QueryValidationError, parse_entity_query
from web_app.schemas import AnalyzeRequest
from web_app.search_service import build_search_queries, source_relevance_score


def test_query_builder_uses_user_input():
    entity = parse_entity_query("91310115MA1HADE54A")
    queries = build_search_queries(entity)
    assert queries
    assert all("91310115MA1HADE54A" in query for query in queries)


def test_request_normalizes_whitespace():
    request = AnalyzeRequest(query="  南京钢铁股份有限公司  ")
    assert request.query == "南京钢铁股份有限公司"


def test_valid_credit_code_is_accepted():
    entity = parse_entity_query("91310115MA1HADE54A")
    assert entity.query_type == "credit_code"
    assert entity.normalized == "91310115MA1HADE54A"


def test_invalid_placeholder_credit_code_is_rejected():
    with pytest.raises(QueryValidationError):
        parse_entity_query("91310000MA1KAAAAAA")


def test_wrong_credit_code_checksum_is_rejected():
    with pytest.raises(QueryValidationError):
        parse_entity_query("91310115MA1HADE54B")


def test_stock_code_is_accepted():
    entity = parse_entity_query("600282")
    assert entity.query_type == "stock_code"


def test_irrelevant_game_result_is_dropped():
    entity = parse_entity_query("91310115MA1HADE54A")
    score = source_relevance_score(
        "4399小游戏大全",
        "双人小游戏、网页游戏攻略",
        "https://www.4399.com/",
        entity,
    )
    assert score < 0


def test_exact_credit_code_enterprise_result_is_kept():
    entity = parse_entity_query("91310115MA1HADE54A")
    score = source_relevance_score(
        "某某科技有限公司企业信息",
        "统一社会信用代码91310115MA1HADE54A，法定代表人张某",
        "https://example.com.cn/company",
        entity,
    )
    assert score >= 4


def test_stock_identity_query_uses_general_topic():
    from web_app.search_service import build_identity_plan
    entity = parse_entity_query("600282")
    plan = build_identity_plan(entity)[0]
    assert plan.topic == "general"
    assert "600282" in plan.query
