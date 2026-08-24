from __future__ import annotations

import asyncio
from typing import Dict, Iterable

from web_app.query_utils import parse_entity_query
from web_app.search_service import TavilySearchClient


async def crawl_batch(queries: Iterable[str]) -> Dict[str, object]:
    client = TavilySearchClient()
    entities = [parse_entity_query(query) for query in queries if query.strip()]
    results = await asyncio.gather(
        *(client.search_identity(entity) for entity in entities),
        return_exceptions=True,
    )
    return {entity.normalized: result for entity, result in zip(entities, results)}
