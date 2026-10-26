from __future__ import annotations

from typing import Any, Dict


def _active(data: Dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        inactive_tokens = ("未发生", "不存在", "已解除", "已结案", "已清偿", "已整改")
        placeholder_tokens = ("待核验", "未获取", "未披露", "未知", "信息不足", "核验中", "待补充")
        if any(token in text for token in inactive_tokens):
            return False
        if text in placeholder_tokens or any(token in text for token in placeholder_tokens):
            return False
        return True
    return bool(value)


def calculate_rule_score(data: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy deterministic scorer aligned with the v9.3 bank-credit policy.

    Counts alone never create high risk.  Active default/失信 receives a floor, while
    resolved events and normal operating guarantees are not treated as adverse.
    """
    score = 0.0
    floor = 0.0
    reasons = []
    if _active(data, "dishonest"):
        floor = max(floor, 72.0); reasons.append("当前失信被执行")
    if _active(data, "debt_default") or _active(data, "overdue_debt"):
        floor = max(floor, 70.0); reasons.append("当前债务逾期或违约")
    if _active(data, "bankruptcy"):
        floor = max(floor, 76.0); reasons.append("破产或重整风险")
    if _active(data, "abnormal_status"):
        score += 10.0; reasons.append("经营异常待核验")
    amount = float(data.get("total_lawsuit_amount", 0) or 0)
    active_lawsuit = _active(data, "active_lawsuit") or int(data.get("lawsuit_count", 0) or 0) > 0
    if active_lawsuit and amount >= 100_000_000:
        floor = max(floor, 62.0); reasons.append("重大未决涉诉")
    elif active_lawsuit and amount >= 10_000_000:
        floor = max(floor, 44.0); reasons.append("较大未决涉诉")
    elif active_lawsuit:
        score += min(10.0, int(data.get("lawsuit_count", 0) or 0) * 2.0); reasons.append("一般司法纠纷")
    freeze_count = int(data.get("active_freeze_count", data.get("freeze_count", 0)) or 0)
    if freeze_count:
        floor = max(floor, 44.0); reasons.append(f"当前股权冻结{freeze_count}条")
    pledge_ratio = float(data.get("pledge_ratio", 0) or 0)
    if pledge_ratio >= 70:
        floor = max(floor, 44.0); reasons.append("高比例股权质押")
    elif pledge_ratio >= 40:
        score += 12.0; reasons.append("股权质押比例偏高")
    if _active(data, "annual_not_public"):
        score += 16.0; reasons.append("年报披露异常")
    if _active(data, "guarantee_default") or _active(data, "guarantee_compensation"):
        floor = max(floor, 62.0); reasons.append("担保代偿或被担保方违约")
    elif _active(data, "normal_guarantee"):
        reasons.append("正常经营担保（不加风险分）")
    score = round(min(100.0, max(score, floor)), 2)
    level = "高风险" if score >= 60 else ("中风险" if score >= 40 else "低风险")
    return {"score": score, "risk_level": level, "reasons": reasons}
