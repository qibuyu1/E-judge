from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import ROOT, settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockHint:
    code: str
    short_name: str
    exchange: str
    company_name: str = ""
    status: str = ""
    provider: str = "证券主数据"
    source_url: str = ""
    reliability: float = 0.88

    @property
    def display_name(self) -> str:
        return self.company_name or self.short_name


_CACHE_PATH = ROOT / "runtime/cache/stock_master.json"
_CACHE_TTL_SECONDS = 30 * 24 * 3600
_CACHE_LOCK = asyncio.Lock()

# Local cache is populated from live structured providers; no company records are hard-coded.



def _market_prefix(code: str) -> Optional[str]:
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("00", "20", "30")):
        return "sz"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return None


def _exchange_name(prefix: str | None) -> str:
    return {"sh": "上海证券交易所", "sz": "深圳证券交易所", "bj": "北京证券交易所"}.get(prefix or "", "证券交易所")


def _official_profile_url(code: str) -> str:
    prefix = _market_prefix(code)
    if prefix == "sh":
        return f"https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml?COMPANY_CODE={code}&tabActive=1"
    if prefix == "sz":
        return f"https://www.cninfo.com.cn/new/disclosure/stock?stockCode={code}"
    if prefix == "bj":
        return f"https://www.bse.cn/products/neeq_listed_companies/general_information.html?companyCode={code}&xxfcbj=2"
    return "https://www.cninfo.com.cn/new/snapshot/companyListCn"


def _load_cache() -> dict[str, dict[str, Any]]:
    try:
        if not _CACHE_PATH.exists():
            return {}
        payload = json.loads(_CACHE_PATH.read_text("utf-8"))
        if not isinstance(payload, dict):
            return {}
        now = time.time()
        return {
            str(code): item
            for code, item in payload.items()
            if isinstance(item, dict) and now - float(item.get("cached_at", 0)) <= _CACHE_TTL_SECONDS
        }
    except Exception:
        return {}


async def _read_cached(code: str) -> Optional[StockHint]:
    async with _CACHE_LOCK:
        item = _load_cache().get(code)
    if not item:
        return None
    try:
        data = dict(item)
        data.pop("cached_at", None)
        return StockHint(**data)
    except Exception:
        return None


async def _write_cached(hint: StockHint) -> None:
    async with _CACHE_LOCK:
        payload = _load_cache()
        payload[hint.code] = {**asdict(hint), "cached_at": time.time()}
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(_CACHE_PATH)


def _clean_name(value: Any) -> str:
    text = str(value or "").strip().replace("*", "")
    return " ".join(text.split())


async def _resolve_cninfo(code: str, client: httpx.AsyncClient) -> Optional[StockHint]:
    # Official disclosure platform search endpoint used by cninfo's own front-end.
    url = "https://www.cninfo.com.cn/new/information/topSearch/query"
    try:
        response = await client.get(
            url,
            params={"keyWord": code, "maxNum": 10},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.cninfo.com.cn/new/index",
                "Accept": "application/json, text/plain, */*",
            },
        )
        if response.status_code != 200:
            return None
        data = response.json()
        rows = data if isinstance(data, list) else data.get("data") or data.get("results") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_code = str(row.get("code") or row.get("secCode") or row.get("stockCode") or row.get("zwjc") or "").strip()
            if row_code and row_code[-6:] != code:
                continue
            short_name = _clean_name(row.get("shortName") or row.get("secName") or row.get("stockName") or row.get("jc") or row.get("name"))
            company_name = _clean_name(row.get("companyName") or row.get("orgName") or row.get("zwqm") or row.get("fullName"))
            if short_name or company_name:
                return StockHint(
                    code=code,
                    short_name=short_name or company_name,
                    company_name=company_name,
                    exchange=_exchange_name(_market_prefix(code)),
                    provider="巨潮资讯证券主数据",
                    source_url="https://www.cninfo.com.cn/new/snapshot/companyListCn",
                    reliability=0.97,
                )
    except Exception as exc:
        logger.debug("CNINFO resolver failed for %s: %s", code, exc)
    return None


async def _resolve_eastmoney(code: str, client: httpx.AsyncClient) -> Optional[StockHint]:
    prefix = _market_prefix(code)
    market = {"sh": "1", "sz": "0", "bj": "0"}.get(prefix or "")
    if market is None:
        return None
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    try:
        response = await client.get(
            url,
            params={"secid": f"{market}.{code}", "fields": "f57,f58,f84,f85"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        if response.status_code != 200:
            return None
        data = (response.json() or {}).get("data") or {}
        if str(data.get("f57") or "") != code:
            return None
        name = _clean_name(data.get("f58"))
        if not name:
            return None
        return StockHint(
            code=code,
            short_name=name,
            exchange=_exchange_name(prefix),
            provider="东方财富证券主数据",
            source_url=f"https://quote.eastmoney.com/{prefix}{code}.html" if prefix else "https://quote.eastmoney.com/",
            reliability=0.90,
        )
    except Exception as exc:
        logger.debug("Eastmoney resolver failed for %s: %s", code, exc)
        return None


async def _resolve_eastmoney_profile(code: str, client: httpx.AsyncClient) -> Optional[StockHint]:
    """Resolve the listed issuer's legal name from Eastmoney's structured F10 profile.

    This is a zero-token structured lookup. Field names have changed across F10
    versions, so several known aliases are accepted and the function fails closed.
    """
    prefix = _market_prefix(code)
    if prefix not in {"sh", "sz", "bj"}:
        return None
    market_prefix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[prefix]
    endpoints = [
        (
            "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax",
            {"code": f"{market_prefix}{code}"},
        ),
        (
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            {
                "reportName": "RPT_F10_BASIC_ORGINFO",
                "columns": "ALL",
                "filter": f'(SECUCODE="{code}.{market_prefix}")',
                "pageNumber": 1,
                "pageSize": 1,
                "source": "HSF10",
                "client": "PC",
            },
        ),
    ]
    for url, params in endpoints:
        try:
            response = await client.get(
                url,
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"},
            )
            if response.status_code != 200:
                continue
            payload = response.json() or {}
            rows: list[dict[str, Any]] = []
            if isinstance(payload, dict):
                for key in ("jbzl", "data", "result"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        rows.extend(item for item in value if isinstance(item, dict))
                    elif isinstance(value, dict):
                        nested = value.get("data") or value.get("rows") or value.get("list")
                        if isinstance(nested, list):
                            rows.extend(item for item in nested if isinstance(item, dict))
                if not rows and any(k in payload for k in ("gsmc", "ORG_NAME", "SECURITY_NAME_ABBR")):
                    rows.append(payload)
            for row in rows:
                legal_name = _clean_name(
                    row.get("gsmc") or row.get("ORG_NAME") or row.get("ORG_NAME_CN")
                    or row.get("companyName") or row.get("COMPANY_NAME")
                )
                short_name = _clean_name(
                    row.get("agjc") or row.get("SECURITY_NAME_ABBR") or row.get("SECURITY_SHORT_NAME")
                    or row.get("stockName") or row.get("name")
                )
                row_code = _clean_name(
                    row.get("agdm") or row.get("SECURITY_CODE") or row.get("stockCode") or row.get("code")
                )
                if row_code and row_code[-6:] != code:
                    continue
                if legal_name or short_name:
                    return StockHint(
                        code=code,
                        short_name=short_name or legal_name,
                        company_name=legal_name,
                        exchange=_exchange_name(prefix),
                        provider="东方财富F10证券主数据",
                        source_url=f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/Index?type=web&code={market_prefix}{code}",
                        reliability=0.94 if legal_name else 0.90,
                    )
        except Exception as exc:
            logger.debug("Eastmoney F10 resolver failed for %s: %s", code, exc)
    return None


async def _resolve_tencent(code: str, client: httpx.AsyncClient) -> Optional[StockHint]:
    prefix = _market_prefix(code)
    if not prefix:
        return None
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    try:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        if response.status_code != 200:
            return None
        raw = response.content.decode("gb18030", errors="ignore")
        if "~" not in raw:
            return None
        payload = raw.split('="', 1)[-1].rsplit('"', 1)[0]
        fields = payload.split("~")
        if len(fields) < 3 or str(fields[2]).strip() != code:
            return None
        name = _clean_name(fields[1])
        if not name:
            return None
        return StockHint(
            code=code,
            short_name=name,
            exchange=_exchange_name(prefix),
            provider="腾讯证券主数据",
            source_url=f"https://gu.qq.com/{prefix}{code}",
            reliability=0.88,
        )
    except Exception as exc:
        logger.debug("Tencent resolver failed for %s: %s", code, exc)
        return None


async def _resolve_sina(code: str, client: httpx.AsyncClient) -> Optional[StockHint]:
    prefix = _market_prefix(code)
    if prefix not in {"sh", "sz"}:
        return None
    url = f"https://hq.sinajs.cn/list={prefix}{code}"
    try:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        if response.status_code != 200:
            return None
        raw = response.content.decode("gb18030", errors="ignore")
        if '="' not in raw:
            return None
        body = raw.split('="', 1)[-1].rsplit('"', 1)[0]
        name = _clean_name(body.split(",", 1)[0])
        if not name:
            return None
        return StockHint(
            code=code,
            short_name=name,
            exchange=_exchange_name(prefix),
            provider="新浪证券主数据",
            source_url=f"https://finance.sina.com.cn/realstock/company/{prefix}{code}/nc.shtml",
            reliability=0.84,
        )
    except Exception as exc:
        logger.debug("Sina resolver failed for %s: %s", code, exc)
        return None


def _resolve_baostock_sync(code: str) -> Optional[StockHint]:
    try:
        import baostock as bs  # type: ignore
    except Exception:
        return None
    prefix = _market_prefix(code)
    if prefix not in {"sh", "sz"}:
        return None
    logged = False
    try:
        login = bs.login()
        logged = getattr(login, "error_code", "1") == "0"
        if not logged:
            return None
        rs = bs.query_stock_basic(code=f"{prefix}.{code}")
        if getattr(rs, "error_code", "1") != "0" or not rs.next():
            return None
        row = rs.get_row_data()
        fields = list(getattr(rs, "fields", []) or [])
        data = dict(zip(fields, row))
        name = _clean_name(data.get("code_name") or data.get("name"))
        if not name:
            return None
        return StockHint(
            code=code,
            short_name=name,
            exchange=_exchange_name(prefix),
            provider="BaoStock证券主数据",
            source_url=_official_profile_url(code),
            reliability=0.91,
        )
    except Exception as exc:
        logger.debug("BaoStock resolver failed for %s: %s", code, exc)
        return None
    finally:
        if logged:
            try:
                bs.logout()
            except Exception:
                pass


async def _resolve_baostock(code: str) -> Optional[StockHint]:
    try:
        async with asyncio.timeout(max(2.0, float(settings.stock_resolver_timeout_seconds))):
            return await asyncio.to_thread(_resolve_baostock_sync, code)
    except Exception:
        return None





async def resolve_stock_hint(code: str) -> Optional[StockHint]:
    """Resolve stock-code identity from multiple zero-token structured channels.

    The resolver waits for all fast providers within one short deadline and uses
    consensus instead of accepting the first response. A legal company name is only
    accepted from a provider that explicitly exposes a company-name field; ordinary
    announcement prose is never used here.
    """
    if not settings.stock_resolver_enabled:
        return None
    cached = await _read_cached(code)
    if cached:
        return cached

    timeout = max(3.0, float(settings.stock_resolver_timeout_seconds))
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=8)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        tasks = [
            _resolve_cninfo(code, client),
            _resolve_eastmoney_profile(code, client),
            _resolve_eastmoney(code, client),
            _resolve_tencent(code, client),
            _resolve_sina(code, client),
            _resolve_baostock(code),
        ]
        try:
            async with asyncio.timeout(timeout + 0.5):
                rows = await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            rows = []

    valid = [item for item in rows if isinstance(item, StockHint) and item.code == code and item.short_name]
    if not valid:
        return None

    # Weighted consensus on the market short name. This prevents a transient or
    # malformed provider response from taking over the identity.
    name_scores: dict[str, float] = {}
    for item in valid:
        key = _clean_name(item.short_name)
        name_scores[key] = name_scores.get(key, 0.0) + item.reliability
    consensus_name = max(name_scores, key=name_scores.get)
    agreeing = [item for item in valid if _clean_name(item.short_name) == consensus_name]
    legal_candidates = [item for item in agreeing if item.company_name and item.company_name.endswith(("股份有限公司", "有限责任公司", "集团有限公司", "有限公司"))]
    best = max(legal_candidates or agreeing, key=lambda item: (bool(item.company_name), item.reliability))
    providers = "、".join(dict.fromkeys(item.provider for item in agreeing))
    reliability = min(0.99, max(item.reliability for item in agreeing) + min(0.05, 0.015 * (len(agreeing) - 1)))
    result = StockHint(
        code=code,
        short_name=consensus_name,
        exchange=best.exchange or _exchange_name(_market_prefix(code)),
        company_name=best.company_name,
        status=best.status,
        provider=f"证券主数据共识（{providers}）",
        source_url=best.source_url or _official_profile_url(code),
        reliability=reliability,
    )
    await _write_cached(result)
    return result
