from __future__ import annotations

import argparse
import asyncio

from web_app.search_service import tavily_search_client


async def main(query: str) -> None:
    run = await tavily_search_client.debug_search(query)
    print(f"提供商: {run.provider}")
    print(f"结果数: {len(run.sources)}")
    print(f"credits: {run.credits}")
    print(f"耗时: {run.response_seconds}秒")
    if run.warnings:
        print("警告:", "；".join(run.warnings))
    for index, source in enumerate(run.sources, start=1):
        print(f"\n[{index}] {source.title}")
        print(source.url)
        print(f"Tavily相关度: {source.search_score:.3f}  来源可靠度: {source.reliability:.2f}")
        print(source.snippet[:300])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="执行一次真实Tavily企业身份搜索（消耗1 credit）")
    parser.add_argument("query", help="企业名称、股票代码或统一社会信用代码")
    args = parser.parse_args()
    asyncio.run(main(args.query))
