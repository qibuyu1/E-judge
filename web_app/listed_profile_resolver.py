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
from .schemas import SearchSource
from .profile_quality import clean_profile_value, profile_value_quality, sanitize_profile_object

logger = logging.getLogger(__name__)


@dataclass
class ListedCompanyProfile:
    stock_code: str
    short_name: str = ""
    company_name: str = ""
    credit_code: str = ""
    legal_person: str = ""
    registered_address: str = ""
    registered_capital: str = ""
    company_status: str = ""
    established_date: str = ""
    industry: str = ""
    business_scope: str = ""
    controlling_shareholder: str = ""
    actual_controller: str = ""
    main_products: list[str] = field(default_factory=list)
    website: str = ""
    provider: str = "证券结构化资料"
    source_url: str = ""
    reliability: float = 0.92

    def merge(self, other: "ListedCompanyProfile") -> "ListedCompanyProfile":
        if not other:
            return self
        result = ListedCompanyProfile(stock_code=self.stock_code)
        scalar_fields = (
            "short_name", "company_name", "credit_code", "legal_person", "registered_address",
            "registered_capital", "company_status", "established_date", "industry",
            "business_scope", "controlling_shareholder", "actual_controller",
            "website", "provider", "source_url",
        )
        sanitize_profile_object(self)
        sanitize_profile_object(other)
        for field_name in scalar_fields:
            left = clean_profile_value(field_name, getattr(self, field_name))
            right = clean_profile_value(field_name, getattr(other, field_name))
            left_rank = profile_value_quality(field_name, left) + self.reliability
            right_rank = profile_value_quality(field_name, right) + other.reliability
            setattr(result, field_name, right if right and right_rank > left_rank else left)
        result.main_products = list(dict.fromkeys([*self.main_products, *other.main_products]))
        result.reliability = max(self.reliability, other.reliability)
        return sanitize_profile_object(result)

    @property
    def field_count(self) -> int:
        return sum(bool(getattr(self, field_name)) for field_name in (
            "short_name", "company_name", "credit_code", "legal_person", "registered_address",
            "registered_capital", "company_status", "established_date", "industry", "business_scope",
            "controlling_shareholder", "actual_controller",
        ))

    def to_source(self) -> SearchSource:
        sanitize_profile_object(self)
        facts: list[str] = [f"股票代码：{self.stock_code}"]
        mapping = (
            ("证券简称", self.short_name), ("公司全称", self.company_name),
            ("统一社会信用代码", self.credit_code), ("法定代表人", self.legal_person),
            ("注册地址", self.registered_address), ("注册资本", self.registered_capital),
            ("企业状态", self.company_status), ("成立日期", self.established_date),
            ("所属行业", self.industry), ("经营范围", self.business_scope),
            ("控股股东", self.controlling_shareholder), ("实际控制人", self.actual_controller),
            ("官方网站", self.website),
        )
        facts.extend(f"{label}：{value}" for label, value in mapping if value)
        if self.main_products:
            facts.append("主要产品：" + "、".join(self.main_products[:12]))
        text = "；".join(facts) + "。"
        digest = hashlib.sha1(f"{self.stock_code}|{self.provider}|{self.source_url}".encode("utf-8")).hexdigest()[:10]
        return SearchSource(
            id=f"PROFILE-{digest.upper()}",
            title=f"{self.provider}：{self.company_name or self.short_name or self.stock_code}",
            url=self.source_url,
            snippet=text,
            text=text,
            source_type="structured_listed_company_profile",
            provider=self.provider,
            search_query=self.stock_code,
            search_purpose="上市公司结构化档案",
            search_score=1.0,
            reliability=self.reliability,
            fetched=True,
        )


_CACHE_PATH = ROOT / "runtime/cache/listed_profiles.json"
_CACHE_TTL = 24 * 3600
_CACHE_LOCK = asyncio.Lock()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().replace("--", "").replace("None", "")
    return " ".join(text.split())


def _first(row: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        value = row.get(name)
        text = _clean(value)
        if text:
            return text
    return ""


def _rows(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return result
    stack: list[Any] = [payload]
    visited = set()
    while stack:
        value = stack.pop()
        ident = id(value)
        if ident in visited:
            continue
        visited.add(ident)
        if isinstance(value, dict):
            if any(key in value for key in (
                "ORG_NAME", "ORG_NAME_CN", "COMPANY_NAME", "gsmc", "companyName",
                "SECURITY_CODE", "LEGAL_PERSON", "LEGAL_REPRESENTATIVE",
                "REGISTERED_ADDRESS", "REG_ADDRESS", "REG_CAPITAL", "BUSINESS_SCOPE",
                "UNIFIED_SOCIAL_CREDIT_CODE", "SOCIAL_CREDIT_CODE",
            )):
                result.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return result


def _market(code: str) -> tuple[str, str]:
    if code.startswith(("60", "68", "90")):
        return "SH", "1"
    if code.startswith(("00", "20", "30")):
        return "SZ", "0"
    return "BJ", "0"


async def _eastmoney_profile(code: str, client: httpx.AsyncClient) -> Optional[ListedCompanyProfile]:
    prefix, _ = _market(code)
    endpoints = [
        (
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            {
                "reportName": "RPT_F10_BASIC_ORGINFO", "columns": "ALL",
                "filter": f'(SECUCODE="{code}.{prefix}")', "pageNumber": 1,
                "pageSize": 2, "source": "HSF10", "client": "PC",
            },
            f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/Index?type=web&code={prefix}{code}",
        ),
        (
            "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax",
            {"code": f"{prefix}{code}"},
            f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/Index?type=web&code={prefix}{code}",
        ),
    ]
    for endpoint, params, source_url in endpoints:
        try:
            response = await client.get(endpoint, params=params, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            })
            if response.status_code != 200:
                continue
            payload = response.json()
            for row in _rows(payload):
                row_code = _first(row, ("SECURITY_CODE", "agdm", "stockCode", "code"))
                if row_code and row_code[-6:] != code:
                    continue
                profile = ListedCompanyProfile(
                    stock_code=code,
                    short_name=_first(row, ("SECURITY_NAME_ABBR", "SECURITY_SHORT_NAME", "SECU_NAME", "agjc", "stockName", "name")),
                    company_name=_first(row, ("ORG_NAME", "ORG_NAME_CN", "COMPANY_NAME_CN", "gsmc", "companyName", "COMPANY_NAME")),
                    credit_code=_first(row, ("USCC", "SOCIAL_CREDIT_CODE", "UNIFIED_SOCIAL_CREDIT_CODE", "CREDIT_CODE", "tyshxydm")),
                    legal_person=_first(row, ("LEGAL_PERSON", "LEGAL_REPRESENTATIVE", "LEGAL_REP", "LEGAL_PERSON_NAME", "frdb")),
                    registered_address=_first(row, ("REG_ADDRESS", "REGISTERED_ADDRESS", "REGISTERED_ADDR", "REG_ADDR", "zcdz")),
                    registered_capital=_first(row, ("REG_CAPITAL", "REGISTERED_CAPITAL", "REG_CAPITAL_NUM", "REG_CAPITAL_CURRENCY", "zczb")),
                    company_status=_first(row, ("ORG_STATUS", "COMPANY_STATUS", "OPERATE_STATUS", "REG_STATUS", "jyzt")),
                    established_date=_first(row, ("FOUND_DATE", "ESTABLISH_DATE", "REG_DATE", "ESTABLISHED_DATE", "clrq")),
                    industry=_first(row, ("INDUSTRY", "INDUSTRY_NAME", "INDUSTRY_NAME_CSR", "EM2016", "sshy")),
                    business_scope=_first(row, ("BUSINESS_SCOPE", "OPERATE_SCOPE", "OPERATING_SCOPE", "jyfw")),
                    controlling_shareholder=_first(row, ("HOLDING_ORG", "CONTROLLING_SHAREHOLDER", "HOLDER_NAME")),
                    actual_controller=_first(row, ("ACTUAL_CONTROLLER", "FINAL_CONTROLLER", "CONTROL_PERSON")),
                    website=_first(row, ("ORG_WEB", "WEBSITE", "WEB_SITE", "COMPANY_WEBSITE", "gswz")),
                    provider="东方财富F10结构化档案",
                    source_url=source_url,
                    reliability=0.94,
                )
                main_business = _first(row, ("MAIN_BUSINESS", "MAIN_PRODUCTS", "zyyw", "zycp"))
                if main_business:
                    profile.main_products = [item.strip() for item in main_business.replace("；", "、").split("、") if item.strip()][:12]
                sanitize_profile_object(profile)
                if profile.field_count >= 2:
                    return profile
        except Exception as exc:
            logger.debug("Eastmoney profile failed for %s: %s", code, exc)
    return None


def _html_text(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", "；", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def _label_value(page: str, labels: Iterable[str], max_len: int = 500) -> str:
    for label in labels:
        # Handles table cells, definition lists and the common "label:value" form.
        patterns = (
            rf"{re.escape(label)}\s*</[^>]+>\s*<[^>]+>([\s\S]{{1,{max_len * 3}}}?)</",
            rf"{re.escape(label)}\s*[:：]\s*([^<\n]{{1,{max_len}}})",
            rf'>\s*{re.escape(label)}\s*<[/\w\s=\"\'_-]*>\s*([^<]{{1,{max_len}}})<',
        )
        for pattern in patterns:
            match = re.search(pattern, page, flags=re.I)
            if match:
                value = _html_text(match.group(1)).strip(" ：:，,；;")
                if value and value not in {"--", "-"}:
                    return value[:max_len]
    return ""


def _plain_regex(text: str, patterns: Iterable[str], max_len: int = 500) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        value = _clean(match.group(1)).strip(" ：:，,。；;")
        if value and value not in {"--", "-"}:
            return value[:max_len]
    return ""


def _company_party(value: str) -> str:
    if not value:
        return ""
    match = re.search(
        r"([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{3,80}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))",
        value,
    )
    return _clean(match.group(1)) if match else _clean(value).split("(", 1)[0].split("（", 1)[0]


def _fill_profile_from_visible_text(profile: ListedCompanyProfile, page: str) -> ListedCompanyProfile:
    """Fill F10 fields that are embedded in narrative text rather than label cells.

    Public F10 pages often put the social-credit code, registered address, parent and
    actual controller inside the history paragraph.  A label-only parser therefore
    lost exactly the fields users care about even though the page contained them.
    """
    text = _html_text(page)
    title = _html_text(re.search(r"<title[^>]*>([\s\S]*?)</title>", page, flags=re.I).group(1)) if re.search(r"<title[^>]*>([\s\S]*?)</title>", page, flags=re.I) else ""
    if not profile.short_name:
        profile.short_name = _plain_regex(title, (r"^([^（(\-_|]{2,30})\s*[（(]?[0-9]{6}",), 30)
    if not profile.company_name:
        profile.company_name = _plain_regex(text, (
            r"公司名称[:：]\s*([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,80}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))",
            r"([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,80}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))\s*[（(]以下简称(?:公司|本公司)",
        ), 90)
    if not profile.credit_code:
        profile.credit_code = _plain_regex(text, (
            r"统一社会信用代码\s*[:：]?\s*([0-9A-Z]{18})",
            r"统一信用代码\s*[:：]?\s*([0-9A-Z]{18})",
        ), 18)
    if not profile.legal_person:
        profile.legal_person = _plain_regex(text, (r"法人代表[:：]\s*([\u4e00-\u9fff·]{2,20})", r"法定代表人[:：]\s*([\u4e00-\u9fff·]{2,20})"), 24)
    if not profile.registered_address:
        profile.registered_address = _plain_regex(text, (
            r"(?:注册地址|注册地和总部地址(?:均)?为|注册地及总部地址)[:：]?\s*([^；。|]{5,160})",
            r"办公地址[:：]\s*([^；。|]{5,160})",
        ), 180)
    if not profile.registered_capital:
        profile.registered_capital = _plain_regex(text, (
            r"注册资金[:：]\s*([0-9,.]+(?:万|亿)?元)",
            r"注册资本(?:为|[:：])\s*([0-9,.]+(?:万|亿)?元?)",
        ), 60)
    if not profile.established_date:
        profile.established_date = _plain_regex(text, (r"成立日期[:：]\s*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?)",), 30)
    if not profile.industry:
        profile.industry = _plain_regex(text, (r"所属申万行业[:：]\s*([^|；。]{2,80})", r"所属行业[:：]\s*([^|；。]{2,80})"), 80)
    if not profile.business_scope:
        profile.business_scope = _plain_regex(text, (
            r"主要经营活动[:：]?\s*([^。]{4,600})",
            r"经营范围[:：]\s*([^。]{4,900})",
            r"主营业务[:：]\s*([^。]{4,500})",
        ), 1000)
    if not profile.controlling_shareholder:
        profile.controlling_shareholder = _company_party(_plain_regex(text, (
            r"控股股东[:：]\s*([^|。；]{4,180})",
            r"母公司(?:为|是)\s*([^。；]{4,160})",
        ), 180))
    if not profile.actual_controller:
        profile.actual_controller = _company_party(_plain_regex(text, (
            r"实际控制人为\s*([^。；]{4,160})",
            r"最终控制人为\s*([^。；]{4,160})",
            r"实际控制人[:：][\s\S]{0,500}?([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,80}?(?:集团有限公司|股份有限公司|有限责任公司|有限公司))\s*[（(]持有",
        ), 180))
    if not profile.website:
        profile.website = _plain_regex(text, (r"公司网址[:：]\s*(https?://[^\s|]+|www\.[^\s|]+)",), 140)
    if not profile.main_products:
        product_text = _plain_regex(text, (r"产品名称[:：]\s*([^。|]{2,500})", r"主要产品(?:是|包括|[:：])\s*([^。]{2,500})"), 600)
        if product_text:
            product_text = re.split(
                r"\s+(?=(?:公司名称|成立日期|控股股东|实际控制人|法人代表|注册资本|[\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,80}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司)\s*[（(]以下简称))",
                product_text, maxsplit=1,
            )[0].strip()
            profile.main_products = list(dict.fromkeys(
                item.strip() for item in re.split(r"[、,，；;]", product_text)
                if 1 < len(item.strip()) <= 60
            ))[:12]
    return profile


async def _tonghuashun_profile(code: str, client: httpx.AsyncClient) -> Optional[ListedCompanyProfile]:
    urls = [
        f"https://basic.10jqka.com.cn/{code}/company.html",
        f"https://basic.10jqka.com.cn/mobile/{code}/profilen.html",
    ]
    merged: Optional[ListedCompanyProfile] = None
    for url in urls:
        try:
            response = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
                "Referer": f"https://basic.10jqka.com.cn/{code}/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            if response.status_code != 200 or len(response.text) < 500:
                continue
            page = response.text
            profile = ListedCompanyProfile(
                stock_code=code,
                short_name=_label_value(page, ("股票简称", "证券简称", "公司简称"), 40),
                company_name=_label_value(page, ("公司名称", "中文名称", "企业名称"), 80),
                credit_code=_label_value(page, ("统一社会信用代码", "统一信用代码"), 30),
                legal_person=_label_value(page, ("法人代表", "法定代表人"), 30),
                registered_address=_label_value(page, ("注册地址", "公司住所", "办公地址"), 160),
                registered_capital=_label_value(page, ("注册资本", "注册资金"), 60),
                company_status=_label_value(page, ("经营状态", "登记状态", "公司状态", "经营性质"), 30),
                established_date=_label_value(page, ("成立日期", "成立时间"), 30),
                industry=_label_value(page, ("所属申万行业", "所属行业", "行业分类"), 80),
                business_scope=_label_value(page, ("经营范围",), 1000),
                controlling_shareholder=_label_value(page, ("控股股东",), 160),
                actual_controller=_label_value(page, ("实际控制人", "最终控制人"), 160),
                website=_label_value(page, ("公司网址", "官方网站", "公司网站"), 120),
                provider="同花顺上市公司档案",
                source_url=url,
                reliability=0.88,
            )
            # Remove parenthetical holding descriptions from scalar party fields.
            profile.controlling_shareholder = re.split(r"[（(]持有", profile.controlling_shareholder, maxsplit=1)[0].strip()
            profile.actual_controller = re.split(r"[（(]持有", profile.actual_controller, maxsplit=1)[0].strip()
            main = _label_value(page, ("产品名称", "产品类型", "主营业务", "主要产品及业务", "主营产品"), 600)
            if main:
                profile.main_products = list(dict.fromkeys(
                    item.strip() for item in re.split(r"[、,，；;]", main)
                    if 1 < len(item.strip()) <= 60
                ))[:12]
            profile = _fill_profile_from_visible_text(profile, page)
            sanitize_profile_object(profile)
            if profile.field_count >= 3:
                merged = profile if merged is None else merged.merge(profile)
        except Exception as exc:
            logger.debug("10jqka profile failed for %s url=%s: %s", code, url, exc)
    return merged


async def _eastmoney_quote(code: str, client: httpx.AsyncClient) -> Optional[ListedCompanyProfile]:
    _, market = _market(code)
    try:
        response = await client.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": f"{market}.{code}", "fields": "f57,f58"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        if response.status_code != 200:
            return None
        data = (response.json() or {}).get("data") or {}
        if str(data.get("f57") or "") != code:
            return None
        name = _clean(data.get("f58"))
        if not name:
            return None
        return ListedCompanyProfile(
            stock_code=code, short_name=name, provider="东方财富证券主数据",
            source_url="https://quote.eastmoney.com/", reliability=0.89,
        )
    except Exception:
        return None


def _load_cache() -> dict[str, Any]:
    try:
        if not _CACHE_PATH.exists():
            return {}
        payload = json.loads(_CACHE_PATH.read_text("utf-8"))
        if not isinstance(payload, dict):
            return {}
        now = time.time()
        return {k: v for k, v in payload.items() if now - float(v.get("cached_at", 0)) <= _CACHE_TTL}
    except Exception:
        return {}


async def resolve_listed_profile(code: str) -> Optional[ListedCompanyProfile]:
    async with _CACHE_LOCK:
        cached = _load_cache().get(code)
    if cached:
        try:
            data = dict(cached)
            data.pop("cached_at", None)
            cached_profile = ListedCompanyProfile(**data)
            # Partial profiles are re-fetched instead of being frozen for days.
            if cached_profile.field_count >= 8:
                return cached_profile
        except Exception:
            pass

    timeout = max(4, min(12, int(getattr(settings, "stock_resolver_timeout_seconds", 6)) + 2))
    limits = httpx.Limits(max_connections=6, max_keepalive_connections=4)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        results = await asyncio.gather(
            _eastmoney_profile(code, client),
            _tonghuashun_profile(code, client),
            _eastmoney_quote(code, client),
            return_exceptions=True,
        )
    profiles = [item for item in results if isinstance(item, ListedCompanyProfile)]
    if not profiles:
        return None
    merged = sanitize_profile_object(profiles[0])
    for profile in profiles[1:]:
        merged = merged.merge(sanitize_profile_object(profile))
    sanitize_profile_object(merged)
    async with _CACHE_LOCK:
        payload = _load_cache()
        payload[code] = {**merged.__dict__, "cached_at": time.time()}
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(_CACHE_PATH)
    return merged
