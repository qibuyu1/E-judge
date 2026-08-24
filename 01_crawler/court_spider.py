from __future__ import annotations

from web_app.query_utils import parse_entity_query
from web_app.search_service import SearchPlan, TavilySearchClient


async def search_judicial_public_information(query: str):
    """使用Tavily检索公开司法、执行、处罚和风险事件。"""
    entity = parse_entity_query(query)
    q = entity.normalized
    plan = SearchPlan(
        query=f'"{q}" 被执行人 失信 裁判文书 行政处罚 股权冻结',
        purpose="司法风险",
        search_depth="basic",
        max_results=10,
    )
    client = TavilySearchClient()
    run = await client.run_plans([plan], entity, credit_budget=1)
    return run.sources
