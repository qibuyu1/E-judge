from __future__ import annotations

"""Run optional live comparison against an external, user-owned case list.

The case file is intentionally not included in the project.  Live results are an
operational quality check, not a mechanism for overriding evidence-led scores.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_app.report_service import research_company


def normalize_level(value: str) -> str:
    text = value or ""
    if "高风险" in text or text.strip() == "高":
        return "高"
    if "中风险" in text or text.strip() == "中":
        return "中"
    if "低风险" in text or text.strip() == "低":
        return "低"
    return "未知"


def load_external_cases(path: Path) -> list[dict[str, str]]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("外部案例文件必须是 JSON 数组")
    cases: list[dict[str, str]] = []
    for index, row in enumerate(payload, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 行不是对象")
        company = str(row.get("company") or "").strip()
        expected = normalize_level(str(row.get("expected_level") or row.get("level") or ""))
        if not company or expected == "未知":
            raise ValueError(f"第 {index} 行必须包含 company 和低/中/高 expected_level")
        cases.append({"company": company, "expected": expected})
    return cases


async def main_async(cases_path: Path, limit: int, force_refresh: bool) -> int:
    rows = load_external_cases(cases_path)
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if row["company"] in seen:
            continue
        seen.add(row["company"])
        unique.append(row)
    if limit > 0:
        unique = unique[:limit]

    results = []
    for index, row in enumerate(unique, 1):
        company = row["company"]
        expected = row["expected"]
        print(f"[{index}/{len(unique)}] {company} ...", flush=True)
        try:
            report = await research_company(company, force_refresh=force_refresh)
            actual = normalize_level((report.get("summary") or {}).get("risk_level") or "")
            results.append(
                {
                    "company": company,
                    "expected": expected,
                    "actual": actual,
                    "ok": actual == expected,
                    "report_id": report.get("report_id"),
                }
            )
        except Exception as exc:  # operational runner: preserve per-case failures
            results.append(
                {
                    "company": company,
                    "expected": expected,
                    "actual": "ERROR",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    out = Path(__file__).resolve().parents[1] / "runtime" / "benchmark_live_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    correct = sum(1 for item in results if item["ok"])
    print(f"LIVE benchmark: {correct}/{len(results)} aligned")
    print(f"details: {out}")
    print("Note: disagreement requires evidence review; it must not be auto-forced to the external label.")
    return 0 if correct == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="使用真实 Tavily + DeepSeek 对外部案例清单执行在线对照，会消耗 API 额度。")
    parser.add_argument("--cases", type=Path, required=True, help="外部 JSON 案例路径；项目不内置该文件")
    parser.add_argument("--limit", type=int, default=5, help="默认只测 5 家；0 表示全部")
    parser.add_argument("--force-refresh", action="store_true", help="忽略可信缓存并重新联网")
    args = parser.parse_args()
    if not args.cases.is_file():
        parser.error(f"外部案例文件不存在：{args.cases}")
    return asyncio.run(main_async(args.cases, args.limit, args.force_refresh))


if __name__ == "__main__":
    raise SystemExit(main())
