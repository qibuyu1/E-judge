from __future__ import annotations

import re
from typing import Any

# Scalar issuer fields from public F10 pages are frequently concatenated with the
# next label.  Keep the cleaning rules in one place so structured resolvers and
# model backfill use the same bank-grade validation.
_LABELS = (
    "公司名称", "证券简称", "统一社会信用代码", "营业执照注册号", "法定代表人", "法人代表",
    "注册地址", "办公地址", "注册资本", "成立日期", "经营状态", "企业状态", "所属行业",
    "经营范围", "主营业务", "控股股东", "实际控制人", "最终控制人", "主要产品", "公司网址",
)
_PLACEHOLDERS = {
    "", "-", "--", "未获取", "待核验", "暂无", "不详", "未知", "无", "不存在", "未发生变化",
    "营业执照注册号", "法定代表人", "法人代表", "注册地址", "经营范围", "实际控制人", "控股股东",
}
_COMPANY_SUFFIX = r"(?:股份有限公司|有限责任公司|集团有限公司|有限公司)"


def _balanced(value: str) -> bool:
    return value.count("(") == value.count(")") and value.count("（") == value.count("）")


def _strip_following_labels(value: str) -> str:
    labels = "|".join(re.escape(item) for item in _LABELS)
    return re.split(rf"\s*(?=(?:{labels})\s*[:：]?)", value, maxsplit=1)[0].strip(" ：:，,；;。|")


def clean_profile_value(field: str, value: Any) -> str:
    text = " ".join(str(value or "").replace("\xa0", " ").split()).strip(" ：:，,；;。|")
    if not text:
        return ""
    text = re.sub(r"^(?:变更为|现为|为|系|即)\s*", "", text)
    text = _strip_following_labels(text)
    if text in _PLACEHOLDERS:
        return ""

    if field == "credit_code":
        match = re.search(r"\b[0-9A-Z]{18}\b", text.upper())
        return match.group(0) if match else ""
    if field == "stock_code":
        match = re.search(r"(?<!\d)\d{6}(?!\d)", text)
        return match.group(0) if match else ""
    if field == "company_name":
        match = re.search(rf"[\u4e00-\u9fffA-Za-z0-9（）()·&\-]{{3,80}}?{_COMPANY_SUFFIX}", text)
        return match.group(0).strip() if match else (text if 4 <= len(text) <= 80 else "")
    if field == "short_name":
        return text if 2 <= len(text) <= 30 and not any(label in text for label in _LABELS) else ""
    if field == "legal_person":
        candidate = re.split(r"[；。，,|\s]", text, maxsplit=1)[0]
        if any(token in candidate for token in ("营业执照", "注册号", "公司", "集团", "未发生", "变更")):
            return ""
        return candidate if re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]{2,20}", candidate) else ""
    if field == "registered_address":
        candidate = re.split(r"(?:法定代表人|法人代表|经营范围|注册资本|营业执照注册号|统一社会信用代码|成立日期)\s*[:：]?", text, maxsplit=1)[0]
        candidate = candidate.strip(" ：:，,；;。|")
        address_markers = ("省", "市", "区", "县", "镇", "乡", "街道", "路", "大道", "号", "工业园", "开发区", "自治州")
        if not (5 <= len(candidate) <= 100) or not any(marker in candidate for marker in address_markers):
            return ""
        return candidate
    if field == "registered_capital":
        match = re.search(r"[0-9][0-9,]*(?:\.\d+)?\s*(?:万亿|亿|万)?\s*(?:元|人民币|美元|港元)", text)
        return match.group(0).replace(" ", "") if match else ""
    if field == "company_status":
        for state in ("存续", "在业", "开业", "正常", "注销", "吊销", "迁出", "撤销", "停业", "清算", "歇业"):
            if state in text:
                return state
        return ""
    if field == "established_date":
        match = re.search(r"(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\d|3[01])日?", text)
        return match.group(0) if match else ""
    if field == "industry":
        candidate = re.split(r"[；。，|]", text, maxsplit=1)[0].strip()
        if not (2 <= len(candidate) <= 36) or any(token in candidate for token in ("经营范围", "一般项目", "许可项目", "批发", "零售")):
            return ""
        return candidate
    if field in {"controlling_shareholder", "actual_controller"}:
        candidate = re.split(r"(?:持有|持股|控制|提供担保|未发生变化|；|。|，)", text, maxsplit=1)[0].strip(" ：:，,；;。|")
        if candidate in _PLACEHOLDERS or not _balanced(candidate) or candidate.startswith((")", "）")):
            return ""
        company = re.search(rf"[\u4e00-\u9fffA-Za-z0-9（）()·&\-]{{3,80}}?{_COMPANY_SUFFIX}", candidate)
        if company:
            return company.group(0).strip()
        if re.fullmatch(r"[\u4e00-\u9fff·]{2,10}", candidate):
            return candidate
        if 3 <= len(candidate) <= 40 and any(token in candidate for token in ("国资委", "委员会", "国有资本", "控股集团", "投资集团")):
            return candidate
        return ""
    if field == "business_scope":
        return text[:1000]
    if field == "website":
        match = re.search(r"(?:https?://|www\.)[^\s|；]+", text)
        return match.group(0) if match else ""
    return text


def profile_value_quality(field: str, value: Any) -> float:
    clean = clean_profile_value(field, value)
    if not clean:
        return 0.0
    score = 1.0
    if field in {"credit_code", "stock_code", "legal_person", "registered_capital", "established_date"}:
        score += 1.0
    if field == "registered_address" and any(token in clean for token in ("省", "市", "区", "县")):
        score += 0.8
    if field in {"controlling_shareholder", "actual_controller", "company_name"} and re.search(_COMPANY_SUFFIX, clean):
        score += 0.8
    return score


def sanitize_profile_object(profile: Any) -> Any:
    fields = (
        "stock_code", "short_name", "company_name", "credit_code", "legal_person", "registered_address",
        "registered_capital", "company_status", "established_date", "industry", "business_scope",
        "controlling_shareholder", "actual_controller", "website",
    )
    for field in fields:
        if hasattr(profile, field):
            setattr(profile, field, clean_profile_value(field, getattr(profile, field)))
    if hasattr(profile, "main_products"):
        products = []
        for item in list(getattr(profile, "main_products") or []):
            value = _strip_following_labels(" ".join(str(item).split())).strip(" ：:，,；;。|")
            if 2 <= len(value) <= 60 and value not in products and not any(label in value for label in _LABELS):
                products.append(value)
        profile.main_products = products[:12]
    return profile
