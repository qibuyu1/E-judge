from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

QueryType = Literal["credit_code", "stock_code", "company_name"]

USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "集团",
    "公司",
)


class QueryValidationError(ValueError):
    """Raised when an enterprise query is malformed or clearly a placeholder."""


@dataclass(frozen=True)
class EntityQuery:
    raw: str
    normalized: str
    query_type: QueryType

    @property
    def exact_identifier(self) -> str | None:
        if self.query_type in {"credit_code", "stock_code"}:
            return self.normalized
        return None

    @property
    def company_core(self) -> str:
        value = self.normalized
        for suffix in COMPANY_SUFFIXES:
            if value.endswith(suffix) and len(value) > len(suffix) + 1:
                value = value[: -len(suffix)]
                break
        return value


def normalize_query(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = " ".join(text.strip().split())
    return text.upper() if re.fullmatch(r"[0-9A-Za-z._-]+", text) else text


def is_valid_uscc(code: str) -> bool:
    value = re.sub(r"\s+", "", normalize_query(code))
    if len(value) != 18 or any(char not in USCC_CHARSET for char in value):
        return False
    total = sum(USCC_CHARSET.index(char) * weight for char, weight in zip(value[:17], USCC_WEIGHTS))
    expected = USCC_CHARSET[(31 - total % 31) % 31]
    return value[-1] == expected


def _looks_like_placeholder(value: str) -> bool:
    upper = value.upper()
    if any(token in upper for token in ("AAAAAA", "BBBBBB", "XXXXXX", "123456789012345678")):
        return True
    alnum = re.sub(r"[^0-9A-Z]", "", upper)
    if len(alnum) >= 8 and len(set(alnum[-6:])) == 1:
        return True
    return False


def _extract_stock_code(text: str) -> str | None:
    """Accept common A-share forms: 600282, SH600282, 600282.SH, 股票代码600282."""
    compact = re.sub(r"\s+", "", text.upper())
    compact = re.sub(r"^(股票|证券|股权)?代码[:：]?", "", compact)
    patterns = (
        r"^(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ))?$",
        r"^(\d{6})(?:SH|SZ|BJ)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, compact)
        if match:
            return match.group(1)
    return None


def parse_entity_query(value: str) -> EntityQuery:
    normalized = normalize_query(value)
    if not normalized:
        raise QueryValidationError("请输入企业名称、股票代码或18位统一社会信用代码")
    if _looks_like_placeholder(normalized):
        raise QueryValidationError("输入内容疑似占位符，请填写真实企业名称、股票代码或统一社会信用代码")

    credit_candidate = re.sub(r"[\s-]+", "", normalized.upper())
    if re.fullmatch(r"[0-9A-Z]{18}", credit_candidate):
        if not is_valid_uscc(credit_candidate):
            raise QueryValidationError("统一社会信用代码校验失败，请检查18位代码是否输入正确")
        return EntityQuery(raw=value, normalized=credit_candidate, query_type="credit_code")

    stock_code = _extract_stock_code(normalized)
    if stock_code:
        return EntityQuery(raw=value, normalized=stock_code, query_type="stock_code")

    # A code-like value should fail clearly instead of falling through as a company name.
    if re.fullmatch(r"[0-9A-Za-z._:-]+", normalized):
        raise QueryValidationError(
            "代码格式不正确：支持6位股票代码、SH/SZ/BJ前后缀格式，或18位统一社会信用代码"
        )

    if len(normalized) < 4:
        raise QueryValidationError(
            "企业名称过短，请输入完整企业名称（例如「比亚迪股份有限公司」），或直接输入 6 位股票代码"
        )
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", normalized):
        raise QueryValidationError("请输入有效企业名称、股票代码或统一社会信用代码")
    return EntityQuery(raw=value, normalized=normalized, query_type="company_name")


def display_model_name(model: str | None) -> str:
    value = (model or "").strip().lower()
    mapping = {
        "deepseek-v4-flash": "DeepSeek V4 Flash",
        "deepseek-v4-pro": "DeepSeek V4 Pro",
    }
    return mapping.get(value, "DeepSeek V4")
