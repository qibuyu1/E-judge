from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from .config import ROOT, settings
from .schemas import Relationship, SearchSource

logger = logging.getLogger(__name__)


@dataclass
class ListedRelationsBundle:
    stock_code: str
    issuer_name: str
    relationships: list[Relationship] = field(default_factory=list)
    sources: list[SearchSource] = field(default_factory=list)
    provider: str = "证券结构化股东资料"


_CACHE_PATH = ROOT / "runtime/cache/listed_relations.json"
_CACHE_TTL = 7 * 24 * 3600
_CACHE_LOCK = asyncio.Lock()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("--", "").replace("None", "").split()).strip()


def _first(row: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        text = _clean(row.get(name))
        if text:
            return text
    return ""


def _float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    result: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    seen: set[int] = set()
    while stack:
        value = stack.pop()
        ident = id(value)
        if ident in seen:
            continue
        seen.add(ident)
        if isinstance(value, dict):
            if any(key in value for key in ("HOLDER_NAME", "HOLDER_RANK", "HOLD_RATIO", "SECURITY_CODE")):
                result.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return result


def _load_cache() -> dict[str, Any]:
    try:
        if not _CACHE_PATH.exists():
            return {}
        payload = json.loads(_CACHE_PATH.read_text("utf-8"))
        now = time.time()
        return {
            key: value for key, value in payload.items()
            if isinstance(value, dict) and now - float(value.get("cached_at", 0)) <= _CACHE_TTL
        }
    except Exception:
        return {}


def _deserialize(data: dict[str, Any]) -> ListedRelationsBundle:
    return ListedRelationsBundle(
        stock_code=data.get("stock_code", ""),
        issuer_name=data.get("issuer_name", ""),
        relationships=[Relationship.model_validate(item) for item in data.get("relationships", [])],
        sources=[SearchSource.model_validate(item) for item in data.get("sources", [])],
        provider=data.get("provider", "证券结构化股东资料"),
    )


def _serialize(bundle: ListedRelationsBundle) -> dict[str, Any]:
    return {
        "stock_code": bundle.stock_code,
        "issuer_name": bundle.issuer_name,
        "relationships": [item.model_dump() for item in bundle.relationships],
        "sources": [item.model_dump() for item in bundle.sources],
        "provider": bundle.provider,
        "cached_at": time.time(),
    }


async def _fetch_report(
    client: httpx.AsyncClient,
    code: str,
    report_name: str,
    purpose: str,
) -> tuple[list[dict[str, Any]], str]:
    endpoint = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "END_DATE,HOLDER_RANK",
        "sortTypes": "-1,1",
        "pageSize": "80",
        "pageNumber": "1",
        "reportName": report_name,
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{code}")',
    }
    response = await client.get(
        endpoint,
        params=params,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    if response.status_code != 200:
        return [], ""
    rows = [row for row in _rows(response.json()) if _first(row, ("SECURITY_CODE",))[-6:] == code]
    if not rows:
        return [], ""
    dates = [_first(row, ("END_DATE", "REPORT_DATE", "NOTICE_DATE")) for row in rows]
    latest = max((date for date in dates if date), default="")
    if latest:
        rows = [row for row in rows if _first(row, ("END_DATE", "REPORT_DATE", "NOTICE_DATE")) == latest]
    return rows, latest


def _bundle_from_rows(
    code: str,
    issuer_name: str,
    report_name: str,
    purpose: str,
    rows: list[dict[str, Any]],
    period: str,
) -> tuple[SearchSource | None, list[Relationship]]:
    if not rows or not issuer_name:
        return None, []
    holder_rows: list[tuple[int, str, Optional[float], str]] = []
    for row in rows:
        holder = _first(row, ("HOLDER_NAME", "SHAREHOLDER_NAME", "ORG_NAME"))
        if not holder or holder == issuer_name:
            continue
        rank_text = _first(row, ("HOLDER_RANK", "RANK"))
        try:
            rank = int(float(rank_text)) if rank_text else 999
        except ValueError:
            rank = 999
        if rank > 15:
            continue
        ratio = _float(_first(row, ("HOLD_RATIO", "HOLDING_RATIO", "FREE_HOLD_RATIO", "TOTAL_SHARES_RATIO")))
        nature = _first(row, ("HOLDER_TYPE", "HOLDER_NATURE", "SHAREHOLDER_TYPE"))
        holder_rows.append((rank, holder, ratio, nature))
    holder_rows.sort(key=lambda item: item[0])
    deduped: list[tuple[int, str, Optional[float], str]] = []
    seen: set[str] = set()
    for item in holder_rows:
        key = "".join(item[1].split()).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 10:
            break
    if not deduped:
        return None, []

    digest = hashlib.sha1(f"{code}|{report_name}|{period}".encode("utf-8")).hexdigest()[:10].upper()
    source_id = f"HOLDER-{digest}"
    source_url = f"https://data.eastmoney.com/gdfx/stock/{code}.html"
    lines = [f"证券代码：{code}", f"公司全称：{issuer_name}", f"报告期：{period or '最新披露'}"]
    relationships: list[Relationship] = []
    relation_type = "前十大流通股东" if "FREE" in report_name else "前十大股东"
    for rank, holder, ratio, nature in deduped:
        ratio_text = f"，持股比例{ratio:g}%" if ratio is not None else ""
        nature_text = f"，股东性质{nature}" if nature else ""
        lines.append(f"第{rank}名：{holder}{ratio_text}{nature_text}")
        relationships.append(Relationship(
            source_name=holder,
            target_name=issuer_name,
            relation_type=relation_type,
            share_ratio=ratio,
            description=f"{period or '最新报告期'}第{rank}名股东{ratio_text}{nature_text}",
            source_ids=[source_id],
            confidence=0.94,
            risk_level="low",
        ))
    text = "；".join(lines) + "。"
    source = SearchSource(
        id=source_id,
        title=f"东方财富结构化股东资料：{issuer_name}（{period or '最新'}）",
        url=source_url,
        snippet=text[:1600],
        text=text,
        source_type="structured_listed_shareholders",
        provider="东方财富结构化股东资料",
        search_query=code,
        search_purpose=purpose,
        search_score=1.0,
        reliability=0.94,
        fetched=True,
    )
    return source, relationships




def _html_rows(page: str) -> str:
    text = re.sub(r"</(?:tr|li|p|div|h[1-6])>", "\n", page, flags=re.I)
    text = re.sub(r"</(?:td|th)>", " | ", text, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _parse_subsidiary_rows(text: str, issuer_name: str, max_rows: int = 36) -> list[tuple[str, str, float]]:
    company_pattern = r"([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,70}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))"
    pattern = re.compile(
        rf"{company_pattern}\s*(?:\||\s)+(子公司|控股子公司|全资子公司|参股公司|联营企业|合营企业)\s*(?:\||\s)+([0-9]+(?:\.[0-9]+)?)%?",
        flags=re.I,
    )
    rows: list[tuple[str, str, float]] = []
    seen: set[str] = set()

    def add(company: str, relation: str, ratio: Optional[float]) -> None:
        company = _clean(company)
        relation = _clean(relation)
        key = "".join(company.split()).lower()
        if not company or company == issuer_name or key in seen or ratio is None:
            return
        if relation == "子公司":
            relation = "全资子公司" if ratio >= 99.9 else "控股子公司" if ratio >= 50 else "参股公司"
        seen.add(key)
        rows.append((company, relation, ratio))

    for match in pattern.finditer(text):
        add(match.group(1), match.group(2), _float(match.group(3)))
        if len(rows) >= max_rows:
            return rows

    # Fallback for table layouts whose cells are separated by visible pipes.
    if len(rows) < 8:
        for line in text.splitlines():
            parts = [part.strip() for part in line.split("|") if part.strip()]
            if len(parts) < 3:
                continue
            company = next((part for part in parts if re.search(company_pattern + r"$", part)), "")
            relation = next((part for part in parts if part in {"子公司", "控股子公司", "全资子公司", "参股公司", "联营企业", "合营企业"}), "")
            ratio_part = next((part for part in parts if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", part)), "")
            add(company, relation, _float(ratio_part))
            if len(rows) >= max_rows:
                break
    return rows


async def _fetch_tonghuashun_subsidiaries(
    client: httpx.AsyncClient,
    code: str,
    issuer_name: str,
) -> tuple[SearchSource | None, list[Relationship]]:
    url = f"https://basic.10jqka.com.cn/{code}/company.html"
    try:
        response = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Referer": f"https://basic.10jqka.com.cn/{code}/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if response.status_code != 200 or len(response.text) < 800:
            return None, []
        text = _html_rows(response.text)
        section_pos = text.find("参股控股公司")
        if section_pos >= 0:
            text = text[section_pos: section_pos + 60000]
        rows = _parse_subsidiary_rows(text, issuer_name, max_rows=36)
        if not rows:
            return None, []
        digest = hashlib.sha1(f"{code}|10jqka-subsidiaries".encode("utf-8")).hexdigest()[:10].upper()
        source_id = f"SUBS-{digest}"
        facts = [f"公司全称：{issuer_name}", f"股票代码：{code}"]
        relationships: list[Relationship] = []
        for company, relation, ratio in rows:
            facts.append(f"{relation}：{company}，参控比例{ratio:g}%")
            relationships.append(Relationship(
                source_name=issuer_name,
                target_name=company,
                relation_type=relation,
                share_ratio=ratio,
                description=f"公开F10资料披露参控比例{ratio:g}%",
                source_ids=[source_id],
                confidence=0.88,
                risk_level="low",
            ))
        body = "；".join(facts) + "。"
        source = SearchSource(
            id=source_id,
            title=f"同花顺参股控股公司：{issuer_name}",
            url=url,
            snippet=body[:1600],
            text=body,
            source_type="structured_listed_subsidiaries",
            provider="同花顺F10参控股公司",
            search_query=code,
            search_purpose="子公司与对外投资结构化档案",
            search_score=1.0,
            reliability=0.88,
            fetched=True,
        )
        return source, relationships
    except Exception as exc:
        logger.debug("10jqka subsidiaries resolver failed code=%s: %s", code, exc)
        return None, []


async def resolve_listed_relations(code: str, issuer_name: str) -> Optional[ListedRelationsBundle]:
    if not code or not issuer_name:
        return None
    cache_key = f"{code}|{issuer_name}"
    async with _CACHE_LOCK:
        cached = _load_cache().get(cache_key)
    if cached:
        try:
            return _deserialize(cached)
        except Exception:
            pass

    timeout = max(5, min(14, int(getattr(settings, "stock_resolver_timeout_seconds", 7)) + 4))
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        results = await asyncio.gather(
            _fetch_report(client, code, "RPT_DMSK_HOLDERS", "前十大股东结构化档案"),
            _fetch_report(client, code, "RPT_F10_EH_FREEHOLDERS", "前十大流通股东结构化档案"),
            _fetch_tonghuashun_subsidiaries(client, code, issuer_name),
            return_exceptions=True,
        )
    bundle = ListedRelationsBundle(stock_code=code, issuer_name=issuer_name)
    for spec, result in zip(
        (("RPT_DMSK_HOLDERS", "前十大股东结构化档案"), ("RPT_F10_EH_FREEHOLDERS", "前十大流通股东结构化档案")),
        results[:2],
    ):
        if isinstance(result, Exception):
            logger.debug("Listed shareholder resolver failed code=%s report=%s: %s", code, spec[0], result)
            continue
        rows, period = result
        source, relationships = _bundle_from_rows(code, issuer_name, spec[0], spec[1], rows, period)
        if source:
            bundle.sources.append(source)
            bundle.relationships.extend(relationships)
    subsidiaries_result = results[2] if len(results) > 2 else None
    if isinstance(subsidiaries_result, tuple):
        source, relationships = subsidiaries_result
        if source:
            bundle.sources.append(source)
            bundle.relationships.extend(relationships)

    # Keep both shareholder and material subsidiary relations. Deduplicate the same
    # source-target pair only within the same semantic relation family.
    dedup: dict[tuple[str, str, str], Relationship] = {}
    for relationship in bundle.relationships:
        family = "股东" if "股东" in relationship.relation_type else "投资"
        key = ("".join(relationship.source_name.split()).lower(), "".join(relationship.target_name.split()).lower(), family)
        current = dedup.get(key)
        if current is None or (relationship.share_ratio or 0) > (current.share_ratio or 0):
            dedup[key] = relationship
    bundle.relationships = list(dedup.values())[:48]
    if not bundle.relationships:
        return None

    async with _CACHE_LOCK:
        payload = _load_cache()
        payload[cache_key] = _serialize(bundle)
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(_CACHE_PATH)
    return bundle
