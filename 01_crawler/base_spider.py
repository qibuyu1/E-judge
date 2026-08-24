from __future__ import annotations

from dataclasses import dataclass
from typing import List

from web_app.query_utils import parse_entity_query
from web_app.schemas import SearchSource
from web_app.search_service import TavilySearchClient


@dataclass
class CrawlResult:
    query: str
    sources: List[SearchSource]
    credits: int = 0


class PublicInformationSpider:
    """Tavily公开网页实时检索入口，不包含预置企业数据。"""

    def __init__(self) -> None:
        self.client = TavilySearchClient()

    async def crawl(self, query: str) -> CrawlResult:
        entity = parse_entity_query(query)
        run = await self.client.search_identity(entity)
        return CrawlResult(query=entity.normalized, sources=run.sources, credits=run.credits)
