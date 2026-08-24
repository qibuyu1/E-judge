from __future__ import annotations

import argparse
import asyncio
import json

from web_app.report_service import research_company


def main() -> None:
    parser = argparse.ArgumentParser(description="链网E判命令行尽调")
    parser.add_argument("query", help="企业名称、股票代码或统一社会信用代码")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存并重新检索")
    args = parser.parse_args()
    report = asyncio.run(research_company(args.query, force_refresh=args.refresh))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
