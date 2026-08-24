from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence

import httpx

from .config import settings
from .query_utils import EntityQuery, parse_entity_query
from .schemas import SearchSource

logger = logging.getLogger(__name__)

NOISE_HOSTS = {
    "4399.com", "7k7k.com", "17173.com", "3dmgame.com", "gamersky.com",
    "zh.wikipedia.org", "wikipedia.org", "support.microsoft.com", "zhihu.com",
    "tieba.baidu.com", "douyin.com", "kuaishou.com", "bilibili.com", "weibo.com",
    "trip.com", "ctrip.com", "mafengwo.cn", "booking.com", "qunar.com",
    "xiaohongshu.com", "pinterest.com", "facebook.com", "instagram.com",
}

HIGH_TRUST_HOST_PARTS = {
    ".gov.cn": 0.98,
    "court.gov.cn": 0.98,
    "creditchina.gov.cn": 0.98,
    "zxgk.court.gov.cn": 0.98,
    "cninfo.com.cn": 0.98,
    "sse.com.cn": 0.98,
    "szse.cn": 0.98,
    "bse.cn": 0.97,
    "neeq.com.cn": 0.96,
    "samr.gov.cn": 0.97,
    "pbc.gov.cn": 0.97,
    "csrc.gov.cn": 0.97,
    "chinatax.gov.cn": 0.96,
}

BUSINESS_DATA_HOST_PARTS = {
    "qcc.com": 0.78,
    "tianyancha.com": 0.76,
    "aiqicha.baidu.com": 0.75,
    "eastmoney.com": 0.76,
    "10jqka.com.cn": 0.74,
    "stockstar.com": 0.70,
    "sina.com.cn": 0.68,
}

ENTERPRISE_TERMS = {
    "企业", "公司", "股份", "有限公司", "统一社会信用代码", "法定代表人",
    "注册资本", "注册地址", "证券代码", "股票代码", "股东", "实际控制人",
    "控股股东", "年报", "年度报告", "对外投资", "子公司", "裁判文书",
    "被执行人", "失信", "行政处罚", "关联交易", "经营范围", "工商",
    "股权质押", "股权冻结", "对外担保", "审计报告", "资金占用",
}

NOISE_TERMS = {
    "小游戏", "游戏攻略", "游戏下载", "旅游攻略", "酒店预订", "机票",
    "卸载应用", "windows help", "软件教程", "壁纸", "小说", "动漫", "电视剧",
}

OFFICIAL_LISTED_DOMAINS = (
    "cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn", "neeq.com.cn", "csrc.gov.cn"
)


class TavilyConfigurationError(RuntimeError):
    code = "TAVILY_NOT_CONFIGURED"


class TavilySearchError(RuntimeError):
    def __init__(self, message: str, *, code: str = "TAVILY_SEARCH_FAILED", status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class SearchPlan:
    query: str
    purpose: str
    search_depth: str = "basic"
    max_results: int = 8
    topic: str = "general"
    exact_match: bool = False
    include_domains: tuple[str, ...] = ()

    @property
    def estimated_credits(self) -> int:
        return 2 if self.search_depth == "advanced" else 1


@dataclass
class SearchRunResult:
    sources: List[SearchSource] = field(default_factory=list)
    query_count: int = 0
    credits: int = 0
    response_seconds: float = 0.0
    request_ids: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    provider: str = "Tavily Search"

    def merge(self, other: "SearchRunResult") -> "SearchRunResult":
        return SearchRunResult(
            sources=merge_sources(self.sources, other.sources),
            query_count=self.query_count + other.query_count,
            credits=self.credits + other.credits,
            response_seconds=round(self.response_seconds + other.response_seconds, 3),
            request_ids=list(dict.fromkeys([*self.request_ids, *other.request_ids])),
            warnings=list(dict.fromkeys([*self.warnings, *other.warnings])),
            provider=other.provider or self.provider,
        )


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_noise_host(hostname: str) -> bool:
    return any(hostname == host or hostname.endswith("." + host) for host in NOISE_HOSTS)


def _is_public_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname or _is_noise_host(hostname):
        return False
    blocked = (
        hostname == "localhost" or hostname.startswith("127.") or hostname.startswith("10.")
        or hostname.startswith("192.168.") or hostname.startswith("169.254.")
        or hostname.startswith("172.16.") or hostname.startswith("172.17.")
        or hostname.startswith("172.18.") or hostname.startswith("172.19.")
        or hostname.startswith("172.2")
    )
    return not blocked


def _reliability(url: str) -> float:
    hostname = _host(url)
    for fragment, score in HIGH_TRUST_HOST_PARTS.items():
        if fragment in hostname:
            return score
    for fragment, score in BUSINESS_DATA_HOST_PARTS.items():
        if fragment in hostname:
            return score
    if hostname.endswith(".edu.cn"):
        return 0.82
    if hostname.endswith(".com.cn") or hostname.endswith(".cn"):
        return 0.67
    return 0.54


def _clean_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _compact(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", (text or "").lower())


def _source_id(url: str, title: str) -> str:
    digest = hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()[:10]
    return f"S{digest.upper()}"


def _stock_exchange_hint(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "上海证券交易所"
    if code.startswith(("00", "20", "30")):
        return "深圳证券交易所"
    if code.startswith(("4", "8", "9")):
        return "北京证券交易所 全国股转系统"
    return "证券交易所"


def build_identity_plan(
    query: str | EntityQuery,
    *,
    advanced: bool = False,
    name_hints: Sequence[str] = (),
) -> List[SearchPlan]:
    entity = query if isinstance(query, EntityQuery) else parse_entity_query(query)
    q = entity.normalized
    depth = settings.tavily_retry_depth if advanced else settings.tavily_identity_depth
    hints = " ".join(f'"{name}"' for name in name_hints if name)

    if entity.query_type == "credit_code":
        text = f'"{q}" {hints} 中国企业 统一社会信用代码 企业全称 法定代表人'.strip()
        return [
            SearchPlan(
                text,
                "企业身份核验",
                depth,
                settings.tavily_max_results_identity,
                topic="general",
                exact_match=not advanced,
            )
        ]

    if entity.query_type == "stock_code":
        # Two tiny parallel queries are much more robust than one overloaded query.
        # The first is restricted to statutory disclosure domains; the second covers
        # market data pages. Neither uses exact_match, which can over-filter numeric
        # identifiers in Tavily's compact index.
        exchange = _stock_exchange_hint(q)
        official = SearchPlan(
            f'"{q}" {hints} 证券代码 公司简称 公司全称 年度报告 {exchange}'.strip(),
            "证券主体官方核验",
            "basic",
            settings.tavily_max_results_identity,
            topic="general",
            include_domains=OFFICIAL_LISTED_DOMAINS,
        )
        general = SearchPlan(
            f'{q} {hints} 股票代码 上市公司 公司简称 公司全称'.strip(),
            "证券主体市场核验",
            depth,
            settings.tavily_max_results_identity,
            topic="general",
        )
        return [official, general]

    text = f'"{q}" 中国企业 企业全称 统一社会信用代码 法定代表人 注册地址'.strip()
    return [SearchPlan(text, "企业身份核验", depth, settings.tavily_max_results_identity, topic="general")]


def build_search_queries(query: str | EntityQuery) -> List[str]:
    return [plan.query for plan in build_identity_plan(query)]


def build_detail_plans(entity: EntityQuery, company_name: str, aliases: Sequence[str] = ()) -> List[SearchPlan]:
    names = [company_name, *aliases]
    names = [name.strip() for name in names if name and len(name.strip()) >= 2]
    if not names:
        return []
    primary = names[0]
    identifier = entity.exact_identifier or ""
    id_hint = f' "{identifier}"' if identifier else ""
    max_results = settings.tavily_max_results_detail

    # One query, one evidence objective. All plans run concurrently, so broader
    # coverage does not multiply wall-clock time in the way serial crawling does.
    plans = [
        SearchPlan(
            f'"{primary}"{id_hint} 企业全称 统一社会信用代码 法定代表人 注册资本 注册地址 成立日期 登记状态 经营范围',
            "法定主体与工商档案", "basic", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 年度报告 净利润 资产负债率 经营现金流 毛利率 有息负债 货币资金 主营业务 主要产品 审计意见 内部控制',
            "年度报告与经营披露", "advanced" if entity.query_type == "stock_code" else "basic", max_results,
            include_domains=OFFICIAL_LISTED_DOMAINS if entity.query_type == "stock_code" else (),
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 控股股东 实际控制人 前十大股东 持股比例 股权变更 控制链',
            "股东与控制链", "advanced", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 主要子公司 全资子公司 控股子公司 参股公司 对外投资',
            "子公司与对外投资", "advanced", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 关联方 关联交易 销售 采购 资金往来 定价依据',
            "关联交易关系", "advanced", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 对外担保 被担保 反担保 资金占用 股权质押 股权冻结',
            "担保质押与资金占用", "advanced", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 重大诉讼 仲裁 被执行 失信 限高 行政处罚 监管措施',
            "司法执行与监管风险", "advanced", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 逾期债务 有息负债 违约 未兑付 流动性 偿债 破产 重整',
            "债务违约与偿债风险", "advanced", max_results,
        ),
        SearchPlan(
            f'"{primary}"{id_hint} 退市 终止上市 摘牌 非标审计 无法表示意见 保留意见 资金占用 违规担保',
            "退市审计与重大异常", "advanced", max_results,
        ),
    ]
    return plans

def build_followup_plans(
    entity: EntityQuery,
    company_name: str,
    *,
    need_profile: bool,
    need_relationships: bool,
    need_risk: bool,
) -> List[SearchPlan]:
    plans: List[SearchPlan] = []
    identifier = entity.exact_identifier or ""
    hint = f' "{identifier}"' if identifier else ""
    if need_profile:
        plans.extend([
            SearchPlan(
                f'"{company_name}"{hint} 工商登记 统一社会信用代码 法定代表人 注册资本 注册地址 成立日期 登记状态',
                "工商法定字段定向补全", "advanced", 8,
            ),
            SearchPlan(
                f'"{company_name}"{hint} 最新年度报告 公司简介 注册地址 注册资本 法定代表人 主营业务 主要产品',
                "年报企业档案定向补全", "advanced", 8,
            ),
            SearchPlan(
                f'"{company_name}"{hint} 控股股东 实际控制人 最终控制人 股权结构 最新年报',
                "控制权字段定向补全", "advanced", 7,
            ),
        ])
    if need_relationships:
        plans.append(SearchPlan(
            f'"{company_name}"{hint} 最新年报 前十大股东 控股股东 实际控制人 主要子公司 关联方 重大担保',
            "关系网络定向补全", "advanced", 8,
        ))
    if need_risk:
        plans.extend([
            SearchPlan(
                f'"{company_name}"{hint} 最新 失信 被执行 限制高消费 重大诉讼 行政处罚 监管措施 股权冻结',
                "司法执行风险定向补全", "advanced", 8,
            ),
            SearchPlan(
                f'"{company_name}"{hint} 最新 逾期债务 债券违约 未兑付 破产 重整 退市 非标审计 资金占用 违规担保',
                "偿债重大异常定向补全", "advanced", 8,
            ),
        ])
    return plans

def build_expanded_queries(entity: EntityQuery, company_name: str, aliases: Sequence[str] = ()) -> List[str]:
    return [plan.query for plan in build_detail_plans(entity, company_name, aliases)]


def _name_match_score(text: str, entity: EntityQuery, aliases: Sequence[str]) -> int:
    compact = _compact(text)
    score = 0
    if entity.query_type == "company_name":
        full = _compact(entity.normalized)
        core = _compact(entity.company_core)
        if full and full in compact:
            score += 9
        elif len(core) >= 4 and core in compact:
            score += 6
        else:
            score -= 5
    else:
        identifier = _compact(entity.normalized)
        if identifier and identifier in compact:
            score += 10
    for alias in aliases:
        alias_compact = _compact(alias)
        if len(alias_compact) >= 2 and alias_compact in compact:
            score += 6
            break
    return score


def source_relevance_score(
    title: str,
    snippet: str,
    url: str,
    entity: EntityQuery,
    aliases: Sequence[str] = (),
    body: str = "",
    tavily_score: float = 0.0,
) -> int:
    hostname = _host(url)
    if not hostname or _is_noise_host(hostname):
        return -100
    combined = _clean_text(f"{title} {snippet} {body[:5000]}")
    lowered = combined.lower()
    score = _name_match_score(combined, entity, aliases)
    score += int(round((_reliability(url) - 0.5) * 8))
    score += int(round(max(0.0, min(1.0, tavily_score)) * 5))
    score += min(8, sum(1 for term in ENTERPRISE_TERMS if term.lower() in lowered))
    score -= sum(1 for term in NOISE_TERMS if term.lower() in lowered) * 6

    alias_matched = any(
        len(_compact(alias)) >= 2 and _compact(alias) in _compact(combined)
        for alias in aliases
    )
    compact_combined = _compact(combined)
    if entity.query_type == "credit_code" and _compact(entity.normalized) not in compact_combined:
        score -= 1 if alias_matched else 14
    if entity.query_type == "stock_code":
        exact = entity.normalized in compact_combined
        securities_context = any(term in combined for term in ("证券代码", "股票代码", "上市公司", "年度报告"))
        if not exact and not alias_matched and not (securities_context and _reliability(url) >= 0.9):
            score -= 8
    return score


def filter_relevant_sources(
    sources: Sequence[SearchSource],
    entity: EntityQuery,
    aliases: Sequence[str] = (),
) -> List[SearchSource]:
    scored: List[tuple[int, SearchSource]] = []
    for source in sources:
        structured = source.source_type.startswith("structured")
        score = source_relevance_score(
            source.title,
            source.snippet,
            source.url,
            entity,
            aliases,
            source.text,
            source.search_score,
        )
        # Structured securities/profile/shareholder/subsidiary evidence is already
        # entity-bound by a stock code and must not be discarded by generic web
        # relevance thresholds.  This was the root cause of basic fields and dozens
        # of relationships disappearing after they had actually been resolved.
        if not structured:
            if source.search_score < settings.tavily_min_score and score < settings.minimum_source_relevance + 3:
                continue
            if score < settings.minimum_source_relevance:
                continue
        else:
            score = max(score, settings.minimum_source_relevance + 8)
        source.reliability = min(1.0, max(source.reliability, 0.42 + min(score, 16) / 35))
        scored.append((score, source))
    scored.sort(
        key=lambda item: (
            item[1].source_type.startswith("structured"),
            item[0], item[1].search_score, item[1].reliability, len(item[1].text),
        ),
        reverse=True,
    )
    structured_rows = [source for _, source in scored if source.source_type.startswith("structured")]
    web_rows = [source for _, source in scored if not source.source_type.startswith("structured")]
    remaining = max(0, settings.tavily_max_sources - len(structured_rows))
    return [*structured_rows, *web_rows[:remaining]]


def _source_dedup_key(source: SearchSource) -> str:
    normalized_url = source.url.split("#", 1)[0].rstrip("/")
    if source.source_type.startswith("structured"):
        # Several independent datasets are legitimately exposed by the same F10 URL
        # (company profile, top shareholders, subsidiaries).  Deduplicating only by
        # URL silently deleted all but one of them.
        return f"structured::{source.source_type}::{source.id}"
    return f"web::{normalized_url}"


def merge_sources(*groups: Iterable[SearchSource]) -> List[SearchSource]:
    dedup: Dict[str, SearchSource] = {}
    for group in groups:
        for source in group:
            key = _source_dedup_key(source)
            previous = dedup.get(key)
            if previous is None:
                dedup[key] = source
                continue
            previous_quality = (previous.search_score, len(previous.text), previous.reliability)
            source_quality = (source.search_score, len(source.text), source.reliability)
            if source_quality > previous_quality:
                dedup[key] = source
    return list(dedup.values())


class TavilySearchClient:
    def __init__(self) -> None:
        self._search_endpoint = settings.tavily_base_url + "/search"
        self._extract_endpoint = settings.tavily_base_url + "/extract"

    @property
    def ready(self) -> bool:
        return settings.tavily_ready

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.tavily_api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"ChainRiskRadar/{settings.app_version}",
        }

    async def get_usage(self) -> Dict[str, object]:
        """Return Tavily account usage without consuming search credits."""
        if not self.ready:
            raise TavilyConfigurationError("请先在项目根目录 .env 中配置 TAVILY_API_KEY")
        endpoint = settings.tavily_base_url + "/usage"
        try:
            async with httpx.AsyncClient(timeout=min(settings.tavily_timeout_seconds, 12)) as client:
                response = await client.get(endpoint, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise TavilySearchError("查询Tavily账户用量超时", code="TAVILY_USAGE_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise TavilySearchError(f"无法读取Tavily账户用量：{exc}", code="TAVILY_USAGE_NETWORK_ERROR") from exc
        if response.status_code != 200:
            raise TavilySearchError(
                f"Tavily用量接口返回{response.status_code}：{self._error_detail(response)}",
                code="TAVILY_USAGE_ERROR",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TavilySearchError("Tavily用量接口响应无法解析", code="TAVILY_USAGE_INVALID_RESPONSE") from exc

    async def run_plans(
        self,
        plans: Sequence[SearchPlan],
        entity: EntityQuery,
        aliases: Sequence[str] = (),
        *,
        credit_budget: int | None = None,
    ) -> SearchRunResult:
        if not self.ready:
            raise TavilyConfigurationError("请先在项目根目录 .env 中配置 TAVILY_API_KEY")
        if not plans:
            return SearchRunResult()

        # ``credit_budget`` is retained only for backward compatibility with old
        # integrations. It is intentionally NOT a hard gate. A local preset must
        # never stop a valid investigation before Tavily is called. We reduce cost
        # through de-duplicated focused plans, caching and adaptive follow-ups.
        selected: List[SearchPlan] = []
        seen: set[tuple] = set()
        for plan in plans:
            key = (
                _compact(plan.query), plan.purpose, plan.search_depth, plan.topic,
                tuple(sorted(plan.include_domains)), bool(plan.exact_match), plan.max_results,
            )
            if key in seen:
                continue
            seen.add(key)
            selected.append(plan)
        if not selected:
            return SearchRunResult()
        estimated = sum(plan.estimated_credits for plan in selected)
        if credit_budget is not None and estimated > max(0, credit_budget):
            logger.info(
                "忽略本地软预算：计划估算%s credits，旧参数为%s；为保证调查完整性继续执行全部聚焦查询",
                estimated, credit_budget,
            )

        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=settings.tavily_timeout_seconds) as client:
            batches = await asyncio.gather(
                *(self._search(plan, client=client) for plan in selected),
                return_exceptions=True,
            )
        result = SearchRunResult(query_count=len(selected), response_seconds=round(time.perf_counter() - started, 3))
        failed = 0
        for plan, batch in zip(selected, batches):
            if isinstance(batch, Exception):
                failed += 1
                if isinstance(batch, TavilySearchError):
                    result.warnings.append(f"{plan.purpose}：{batch}")
                    logger.warning("Tavily查询失败 [%s]: %s", plan.purpose, batch)
                else:
                    result.warnings.append(f"{plan.purpose}：{type(batch).__name__}")
                    logger.exception("Tavily查询异常 [%s]", plan.purpose)
                continue
            result.credits += batch.credits
            result.request_ids.extend(batch.request_ids)
            result.sources.extend(batch.sources)
            result.warnings.extend(batch.warnings)

        result.sources = filter_relevant_sources(merge_sources(result.sources), entity, aliases)
        result.request_ids = list(dict.fromkeys(result.request_ids))
        result.warnings = list(dict.fromkeys(result.warnings))
        if not result.sources and failed == len(selected):
            raise TavilySearchError("；".join(result.warnings), code="TAVILY_ALL_QUERIES_FAILED")
        return result

    async def search_identity(
        self,
        entity: EntityQuery,
        *,
        advanced: bool = False,
        name_hints: Sequence[str] = (),
    ) -> SearchRunResult:
        plans = build_identity_plan(entity, advanced=advanced, name_hints=name_hints)
        identity_budget = 2 if entity.query_type == "stock_code" else (2 if advanced else 1)
        first = await self.run_plans(plans, entity, name_hints, credit_budget=identity_budget)
        if first.sources or advanced:
            return first

        # Exact identifiers are sometimes absent from Tavily's first compact index.
        # Retry once with a plain general query instead of immediately returning
        # NO_IDENTITY_SOURCES. This only costs one extra credit when the first search
        # actually produced zero relevant sources.
        if entity.query_type == "credit_code":
            fallback_query = f'{entity.normalized} 企业名称 法定代表人 工商信息'
        elif entity.query_type == "stock_code":
            fallback_query = f'{entity.normalized} 证券简称 股票简称 公司名称'
        else:
            fallback_query = f'{entity.normalized} 企业 工商 法定代表人 统一社会信用代码'
        fallback = await self.run_plans(
            [SearchPlan(fallback_query, "企业身份补查", "fast", settings.tavily_max_results_identity, "general")],
            entity,
            name_hints,
            credit_budget=1,
        )
        return first.merge(fallback)

    async def search_details(
        self,
        entity: EntityQuery,
        company_name: str,
        aliases: Sequence[str] = (),
        *,
        credit_budget: int = 5,
    ) -> SearchRunResult:
        plans = build_detail_plans(entity, company_name, aliases)
        return await self.run_plans(plans, entity, [company_name, *aliases], credit_budget=credit_budget)

    async def search_followup(
        self,
        entity: EntityQuery,
        company_name: str,
        aliases: Sequence[str] = (),
        *,
        need_profile: bool = False,
        need_relationships: bool = False,
        need_risk: bool = False,
        credit_budget: int = 2,
    ) -> SearchRunResult:
        plans = build_followup_plans(
            entity,
            company_name,
            need_profile=need_profile,
            need_relationships=need_relationships,
            need_risk=need_risk,
        )
        return await self.run_plans(plans, entity, [company_name, *aliases], credit_budget=credit_budget)

    async def debug_search(self, query: str) -> SearchRunResult:
        entity = parse_entity_query(query)
        return await self.search_identity(entity, advanced=False)

    def _payload(self, plan: SearchPlan, *, minimal: bool = False) -> Dict[str, object]:
        # Minimal mode deliberately uses only long-established parameters. It is the
        # automatic fallback for any Tavily HTTP 400 validation error.
        payload: Dict[str, object] = {
            "query": plan.query[:390],
            "search_depth": "basic" if minimal else plan.search_depth,
            "max_results": min(plan.max_results, 10) if minimal else plan.max_results,
            "topic": "general" if minimal else plan.topic,
            "include_answer": False,
            "include_raw_content": False if minimal else settings.tavily_include_raw_content,
        }
        if minimal:
            return payload
        payload.update(
            {
                "include_images": False,
                "exclude_domains": sorted(NOISE_HOSTS),
                "exact_match": plan.exact_match,
                "include_usage": True,
            }
        )
        if plan.include_domains:
            payload["include_domains"] = list(plan.include_domains)
        if plan.topic == "general" and settings.tavily_country and plan.search_depth in {"basic", "advanced"}:
            payload["country"] = settings.tavily_country
        if plan.search_depth == "advanced":
            payload["chunks_per_source"] = 3
        return payload

    async def _post_search(self, client: httpx.AsyncClient, payload: Dict[str, object]) -> httpx.Response:
        return await client.post(self._search_endpoint, headers=self._headers(), json=payload)

    async def _search(self, plan: SearchPlan, client: httpx.AsyncClient | None = None) -> SearchRunResult:
        payload = self._payload(plan)
        owns_client = client is None
        request_client = client or httpx.AsyncClient(timeout=settings.tavily_timeout_seconds)
        try:
            response = await self._post_search(request_client, payload)
            if response.status_code == 400:
                # Retry once with the smallest stable payload. This fixes occasional
                # parameter-validation failures without exposing HTTP 400 to the UI.
                detail = self._error_detail(response)
                logger.warning("Tavily 400, retrying with minimal payload: %s", detail)
                response = await self._post_search(request_client, self._payload(plan, minimal=True))
        except httpx.TimeoutException as exc:
            raise TavilySearchError(
                f"Tavily搜索超时（{settings.tavily_timeout_seconds}秒）",
                code="TAVILY_TIMEOUT",
            ) from exc
        except httpx.HTTPError as exc:
            raise TavilySearchError(f"无法连接Tavily：{exc}", code="TAVILY_NETWORK_ERROR") from exc
        finally:
            if owns_client and hasattr(request_client, "aclose"):
                await request_client.aclose()

        if response.status_code != 200:
            detail = self._error_detail(response)
            code_map = {
                400: "TAVILY_BAD_REQUEST",
                401: "TAVILY_UNAUTHORIZED",
                429: "TAVILY_RATE_LIMITED",
                432: "TAVILY_PLAN_LIMIT",
                433: "TAVILY_PAYG_LIMIT",
            }
            if response.status_code in {432, 433}:
                detail = "Tavily账户的实际套餐额度或按量付费上限已用尽，请在Tavily控制台补充额度后重试。" + (f" 原始信息：{detail}" if detail else "")
            raise TavilySearchError(
                f"Tavily返回{response.status_code}：{detail}",
                code=code_map.get(response.status_code, "TAVILY_HTTP_ERROR"),
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise TavilySearchError("Tavily返回了无法解析的响应", code="TAVILY_INVALID_RESPONSE") from exc

        rows: List[SearchSource] = []
        for item in data.get("results") or []:
            url = _clean_text(item.get("url"))
            if not url or not _is_public_url(url):
                continue
            title = _clean_text(item.get("title"))
            content = _clean_text(item.get("content"))
            raw_content = item.get("raw_content")
            if isinstance(raw_content, dict):
                raw_content = raw_content.get("content") or raw_content.get("text") or ""
            raw_text = _clean_text(raw_content)
            try:
                search_score = max(0.0, min(1.0, float(item.get("score") or 0)))
            except (TypeError, ValueError):
                search_score = 0.0
            body = (raw_text or content)[: settings.tavily_max_chars_per_source]
            rows.append(
                SearchSource(
                    id=_source_id(url, title),
                    title=title or _host(url),
                    url=url,
                    snippet=content[:1600],
                    text=body,
                    source_type="tavily_raw_content" if raw_text else "tavily_search_result",
                    provider="tavily",
                    search_query=plan.query,
                    search_purpose=plan.purpose,
                    search_score=search_score,
                    reliability=_reliability(url),
                    fetched=bool(raw_text),
                )
            )

        usage = data.get("usage") or {}
        credits = int(usage.get("credits") or plan.estimated_credits)
        request_id = _clean_text(data.get("request_id"))
        warnings: List[str] = []
        if not rows:
            warnings.append(f"{plan.purpose}未返回可用结果")
        return SearchRunResult(
            sources=rows,
            query_count=1,
            credits=credits,
            response_seconds=float(data.get("response_time") or 0),
            request_ids=[request_id] if request_id else [],
            warnings=warnings,
        )

    async def extract_sources(
        self,
        sources: Sequence[SearchSource],
        *,
        query: str,
        max_urls: int | None = None,
    ) -> SearchRunResult:
        if not settings.tavily_extract_enabled or not sources:
            return SearchRunResult(sources=list(sources), provider="Tavily Search")
        ranked = sorted(
            sources,
            key=lambda item: (item.reliability, item.search_score, len(item.snippet)),
            reverse=True,
        )[: (max_urls or settings.tavily_extract_max_urls)]
        urls = [item.url for item in ranked if item.url and not item.source_type.startswith("structured")]
        if not urls:
            return SearchRunResult(sources=list(sources), provider="Tavily Search")

        payload: Dict[str, object] = {
            "urls": urls,
            "query": query[:390],
            "extract_depth": settings.tavily_extract_depth,
            "chunks_per_source": settings.tavily_extract_chunks_per_source,
            "format": "markdown",
            "include_images": False,
            "include_favicon": False,
            "include_usage": True,
            "timeout": settings.tavily_extract_timeout_seconds,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=settings.tavily_extract_timeout_seconds + 5) as client:
                response = await client.post(self._extract_endpoint, headers=self._headers(), json=payload)
        except httpx.TimeoutException:
            return SearchRunResult(
                sources=list(sources),
                warnings=["Tavily Extract超时，已使用搜索摘要继续分析"],
                response_seconds=round(time.perf_counter() - started, 3),
            )
        except httpx.HTTPError as exc:
            return SearchRunResult(
                sources=list(sources),
                warnings=[f"Tavily Extract连接失败：{exc}"],
                response_seconds=round(time.perf_counter() - started, 3),
            )
        if response.status_code != 200:
            return SearchRunResult(
                sources=list(sources),
                warnings=[f"Tavily Extract返回{response.status_code}：{self._error_detail(response)}"],
                response_seconds=round(time.perf_counter() - started, 3),
            )
        try:
            data = response.json()
        except ValueError:
            return SearchRunResult(sources=list(sources), warnings=["Tavily Extract响应无法解析"])

        by_url = {item.url.split("#", 1)[0].rstrip("/"): item.model_copy(deep=True) for item in sources}
        for item in data.get("results") or []:
            url = _clean_text(item.get("url")).split("#", 1)[0].rstrip("/")
            source = by_url.get(url)
            if not source:
                continue
            raw = item.get("raw_content") or item.get("content") or ""
            if isinstance(raw, dict):
                raw = raw.get("content") or raw.get("text") or ""
            text = _clean_text(raw)[: settings.tavily_max_chars_per_source]
            if text:
                source.text = text
                source.fetched = True
                source.source_type = "tavily_extract"
        usage = data.get("usage") or {}
        credits = int(usage.get("credits") or 1)
        request_id = _clean_text(data.get("request_id"))
        return SearchRunResult(
            sources=list(by_url.values()),
            credits=credits,
            response_seconds=round(time.perf_counter() - started, 3),
            request_ids=[request_id] if request_id else [],
            provider="Tavily Search + Extract",
        )


    async def extract_research_sources(
        self,
        sources: Sequence[SearchSource],
        *,
        company_name: str,
    ) -> SearchRunResult:
        """Extract high-value pages in three focused lanes, concurrently.

        Search returns broad ranked evidence quickly. Extract then reads only the best
        URLs for profile/business, ownership/transactions and legal/risk themes. Each
        lane uses a different reranking query, so annual-report tables do not compete
        with court or guarantee evidence for the same context slots.
        """
        if not settings.tavily_extract_enabled or not sources:
            return SearchRunResult(sources=list(sources), provider="Tavily Search")

        lane_specs = {
            "profile": {
                "terms": ("主体", "工商", "年度报告", "经营披露", "档案", "证券"),
                "query": f"{company_name} 企业全称 统一社会信用代码 法定代表人 注册资本 注册地址 成立日期 企业状态 主营业务 主要产品 净利润 资产负债率 经营现金流 毛利率 有息负债 货币资金",
                "max_urls": 3,
            },
            "ownership": {
                "terms": ("股东", "控制", "股权"),
                "query": f"{company_name} 控股股东 实际控制人 前十大股东 持股比例 股权变更 控制链",
                "max_urls": 3,
            },
            "investment": {
                "terms": ("投资", "子公司"),
                "query": f"{company_name} 全资子公司 控股子公司 参股公司 主要子公司 对外投资",
                "max_urls": 3,
            },
            "transactions": {
                "terms": ("关联交易", "担保", "资金占用", "质押"),
                "query": f"{company_name} 关联交易 销售 采购 资金往来 对外担保 被担保 股权质押 资金占用",
                "max_urls": 3,
            },
            "risk": {
                "terms": ("司法", "监管", "合规", "风险", "诉讼", "处罚", "冻结", "审计"),
                "query": f"{company_name} 重大诉讼 仲裁 被执行 失信 行政处罚 监管措施 股权冻结 债务风险 审计异常",
                "max_urls": 3,
            },
        }

        groups: list[tuple[str, list[SearchSource], str, int]] = []
        claimed_urls: set[str] = set()
        for lane, spec in lane_specs.items():
            candidates = [
                source for source in sources
                if any(term in (source.search_purpose or "") for term in spec["terms"])
            ]
            # Structured sources are already complete and never need extraction.
            # Search results with substantial text are also kept as-is. This avoids
            # paying repeatedly to extract the same annual report in several lanes.
            candidates = [
                source for source in candidates
                if not source.source_type.startswith("structured")
                and not source.fetched
                and len(source.text or source.snippet or "") < 2200
            ]
            if len(candidates) < 2:
                extras = sorted(
                    [
                        source for source in sources
                        if not source.source_type.startswith("structured")
                        and not source.fetched
                        and source.url.split("#", 1)[0].rstrip("/") not in claimed_urls
                        and source not in candidates
                    ],
                    key=lambda item: (item.reliability, item.search_score),
                    reverse=True,
                )
                candidates.extend(extras[: 2 - len(candidates)])
            unique_candidates: list[SearchSource] = []
            for source in sorted(
                candidates,
                key=lambda item: (item.reliability, item.search_score, len(item.snippet)),
                reverse=True,
            ):
                key = source.url.split("#", 1)[0].rstrip("/")
                if not key or key in claimed_urls:
                    continue
                claimed_urls.add(key)
                unique_candidates.append(source)
                if len(unique_candidates) >= int(spec["max_urls"]):
                    break
            if unique_candidates:
                groups.append((lane, unique_candidates, str(spec["query"]), int(spec["max_urls"])))

        if not groups:
            return SearchRunResult(sources=list(sources), provider="Tavily Search")

        batches = await asyncio.gather(
            *(self.extract_sources(items, query=query, max_urls=max_urls)
              for _, items, query, max_urls in groups),
            return_exceptions=True,
        )
        merged_sources = list(sources)
        result = SearchRunResult(provider="Tavily Search + Extract")
        for (lane, _, _, _), batch in zip(groups, batches):
            if isinstance(batch, Exception):
                result.warnings.append(f"{lane}正文提取失败：{type(batch).__name__}")
                continue
            result = result.merge(batch)
            merged_sources = merge_sources(merged_sources, batch.sources)
        result.sources = merged_sources
        return result

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            data = response.json()
            detail = data.get("detail")
            if isinstance(detail, dict):
                return _clean_text(detail.get("error") or detail)
            return _clean_text(detail or data)
        except Exception:
            return _clean_text(response.text)[:500] or "未知错误"


public_search_client = TavilySearchClient()
tavily_search_client = public_search_client
