from __future__ import annotations

from web_app.query_utils import parse_entity_query
from web_app.search_service import TavilySearchClient


async def search_company_public_information(query: str):
    """使用Tavily检索企业身份、登记、股东、投资和年报公开信息。"""
    entity = parse_entity_query(query)
    client = TavilySearchClient()
    identity = await client.search_identity(entity)
    return identity.sources
