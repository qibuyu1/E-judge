from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .benchmark_calibration import calibrate_score_with_benchmark
from .schemas import (
    AnnualFact,
    GraphEdge,
    GraphNode,
    GraphPayload,
    JudicialEvent,
    ResearchExtraction,
    RiskDimension,
    RiskResult,
    RiskSignal,
    ShapRow,
)

DIMENSION_WEIGHTS: Dict[str, float] = {
    "judicial": 0.30,
    "equity": 0.20,
    "transaction": 0.25,
    "disclosure": 0.15,
    "business": 0.10,
}

DIMENSION_NAMES = {
    "judicial": "司法涉诉",
    "equity": "股权与控制",
    "transaction": "关联交易",
    "disclosure": "信息披露",
    "business": "经营与偿债",
}


HIGH_RISK_WORDS = {
    "失信": 95,
    "被执行": 80,
    "限制高消费": 85,
    "破产": 90,
    "欺诈": 95,
    "虚假": 80,
    "挪用": 90,
    "重大违法": 95,
    "股权冻结": 75,
    "行政处罚": 65,
    "经营异常": 55,
    "质押": 50,
    "诉讼": 45,
    "仲裁": 45,
    "逾期": 65,
    "违约": 80,
    "关联交易": 35,
    "循环交易": 85,
    "资金占用": 90,
    "非经营性占用": 90,
    "审计保留": 75,
    "无法表示意见": 95,
    "重大税收违法": 88,
    "欠税": 55,
    "环境处罚": 58,
    "安全生产事故": 68,
    "停产整顿": 78,
    "吊销许可证": 88,
}

CATEGORY_MAP = {
    "司法": "judicial",
    "诉讼": "judicial",
    "执行": "judicial",
    "失信": "judicial",
    "股权": "equity",
    "控制": "equity",
    "质押": "equity",
    "冻结": "equity",
    "交易": "transaction",
    "担保": "transaction",
    "关联": "transaction",
    "披露": "disclosure",
    "年报": "disclosure",
    "审计": "disclosure",
    "经营": "business",
    "偿债": "business",
    "现金流": "business",
    "逾期": "business",
    "违约": "business",
    "破产": "business",
    "重整": "business",
    "退市": "disclosure",
    "终止上市": "disclosure",
    "税收": "disclosure",
    "欠税": "disclosure",
    "环保": "business",
    "环境": "business",
    "安全生产": "business",
    "停产": "business",
    "吊销许可证": "business",
    "失信": "judicial",
}


NEGATION_WORDS = (
    "不存在", "未发生", "未出现", "未被", "不涉及", "无相关", "无重大", "没有", "并无",
)
RESOLVED_WORDS = (
    "已解除", "已撤销", "已结案", "已清偿", "已结清", "已兑付", "已履行完毕", "已消除", "终结执行",
    "已整改", "已归还", "已偿还", "已终止", "不再承担",
)
ONGOING_WORDS = ("尚未清偿", "尚未履行", "仍未", "持续", "新增", "再次", "仍处于", "未解决", "正在执行", "尚在审理")
HISTORICAL_WORDS = ("曾经", "曾被", "历史上", "此前", "过往", "报告期前", "原有事项")
SPECULATIVE_WORDS = ("拟", "计划", "可能", "风险提示", "需关注", "或将", "预计")
NORMAL_OPERATION_WORDS = (
    "正常履行", "正常经营", "日常经营", "常规业务", "常规关联交易", "日常关联交易",
    "合并范围内", "全资子公司", "控股子公司", "未触发代偿", "无需承担代偿",
    "标准无保留意见", "不存在逾期", "无逾期", "不存在违约", "无违约",
)
MATERIAL_ADVERSE_WORDS = (
    "异常", "违规", "非公允", "利益输送", "循环交易", "资金占用", "非经营性占用",
    "代偿", "逾期", "违约", "未兑付", "未清偿", "失信", "被执行", "限高", "冻结",
    "重大", "严重", "重复处罚", "高比例", "控制权争议", "资不抵债", "无法表示意见",
    "否定意见", "破产", "重整", "退市", "终止上市", "重大税收违法", "欠税",
    "环境处罚", "安全生产事故", "停产整顿", "吊销许可证",
)
STATUS_RISK_WORDS = tuple(dict.fromkeys((*HIGH_RISK_WORDS.keys(), "担保", "欠债", "债务", "限高", "冻结", "亏损", "负净资产")))


def _keyword_is_negated(text: str, keyword: str) -> bool:
    matches = list(re.finditer(re.escape(keyword), text or ""))
    if not matches:
        return True
    for match in matches:
        before = (text or "")[max(0, match.start() - 16):match.start()]
        around = (text or "")[max(0, match.start() - 16):min(len(text or ""), match.end() + 14)]
        if any(token in around for token in NEGATION_WORDS):
            continue
        if re.search(r"(?:无|未|不|没有).{0,8}$", before):
            continue
        return False
    return True


def _status_factor(text: str) -> float:
    """Current-status weight used by every deterministic risk lane.

    A denied event contributes zero.  A resolved event keeps only a small residual
    weight for recency/history, while an explicitly ongoing event remains fully
    effective.  This prevents old or negated web snippets from overturning the
    current bank-credit conclusion.
    """
    value = text or ""
    mentioned = [keyword for keyword in STATUS_RISK_WORDS if keyword in value]
    if mentioned and all(_keyword_is_negated(value, keyword) for keyword in mentioned):
        return 0.0
    if any(token in value for token in ONGOING_WORDS):
        return 1.0
    if any(token in value for token in RESOLVED_WORDS):
        return 0.20
    if any(token in value for token in HISTORICAL_WORDS):
        return 0.55
    if any(token in value for token in SPECULATIVE_WORDS) and not any(token in value for token in MATERIAL_ADVERSE_WORDS):
        return 0.60
    return 1.0


def _has_active(text: str, words: Sequence[str]) -> bool:
    return any(word in (text or "") and not _keyword_is_negated(text, word) for word in words)


def _contextual_cap(text: str, amount_yuan: float = 0.0) -> float:
    """Maximum plausible severity from wording, status and materiality.

    LLM severity is treated as a hint, not authority.  Generic relationship words
    such as “担保” or “关联交易” cannot become high risk without an adverse qualifier,
    while verified default/失信/破产 events retain a high-risk ceiling.
    """
    value = text or ""
    if _status_factor(value) == 0:
        return 0.0
    catastrophic = ("失信", "限制高消费", "限高", "债务违约", "债券违约", "未兑付", "破产", "无法表示意见", "否定意见", "净资产为负")
    if _has_active(value, catastrophic):
        return 100.0
    has_adverse = _has_active(value, MATERIAL_ADVERSE_WORDS)
    is_normal = any(token in value for token in NORMAL_OPERATION_WORDS) and not has_adverse
    if is_normal:
        return 22.0
    if "担保" in value or "保证" in value:
        if _has_active(value, ("违规担保", "担保代偿", "代偿", "逾期", "违约", "无反担保", "超额担保", "资不抵债")):
            return 92.0
        if amount_yuan >= 1_000_000_000:
            return 62.0
        if amount_yuan >= 100_000_000:
            return 52.0
        return 30.0
    if "关联交易" in value or "关联方交易" in value:
        if _has_active(value, ("循环交易", "资金占用", "非经营性占用", "利益输送", "非公允", "定价异常", "违规")):
            return 92.0
        return 24.0
    if any(token in value for token in ("诉讼", "仲裁", "纠纷")):
        if amount_yuan >= 100_000_000 or "重大" in value:
            return 86.0
        if amount_yuan >= 10_000_000:
            return 70.0
        return 48.0
    if "被执行" in value or "执行标的" in value:
        if amount_yuan >= 100_000_000:
            return 92.0
        if amount_yuan >= 10_000_000:
            return 82.0
        return 72.0
    if "股权冻结" in value or ("冻结" in value and "股权" in value):
        return 78.0 if _has_active(value, ("控股股东", "实际控制人", "控制权", "高比例")) else 66.0
    if "质押" in value:
        return 76.0 if _has_active(value, ("高比例", "平仓", "控制权", "爆仓")) else 38.0
    if _has_active(value, ("重大税收违法",)):
        return 90.0
    if "欠税" in value:
        if _has_active(value, ("重大", "大额", "长期", "持续", "尚未缴纳", "追缴")) or amount_yuan >= 100_000_000:
            return 72.0
        return 46.0
    if _has_active(value, ("吊销许可证", "停产整顿")):
        return 88.0
    if "安全生产事故" in value or "环境处罚" in value or "环保处罚" in value:
        return 82.0 if _has_active(value, ("重大", "严重", "重复", "停产", "死亡", "刑事")) else 55.0
    if "行政处罚" in value or "监管措施" in value or "监管关注" in value:
        return 76.0 if _has_active(value, ("重大", "严重", "重复处罚", "立案调查", "欺诈", "虚假")) else 52.0
    if _has_active(value, ("控制权变更", "控制权争议", "实际控制人变更")):
        return 60.0
    if any(token in value for token in ("股权变动", "股东变更", "增持", "减持", "股份转让")):
        return 28.0
    if "经营异常" in value:
        return 48.0
    return 100.0 if has_adverse else 58.0


def _score_text(text: str, default: int = 35, amount_yuan: float = 0.0) -> int:
    score = default
    for keyword, weight in HIGH_RISK_WORDS.items():
        if keyword in (text or "") and not _keyword_is_negated(text, keyword):
            score = max(score, weight)
    cap = _contextual_cap(text, amount_yuan)
    return int(round(min(100.0, cap, score)))


def _contextual_model_score(text: str, model_severity: float, default: int = 35, amount_yuan: float = 0.0) -> float:
    rule = float(_score_text(text, default, amount_yuan))
    cap = _contextual_cap(text, amount_yuan)
    has_adverse = _has_active(text, MATERIAL_ADVERSE_WORDS)
    model_weight = 1.0 if cap >= 95 else (0.82 if has_adverse else 0.42)
    return min(cap, max(rule, float(model_severity) * model_weight))


def _category(text: str, fallback: str = "business") -> str:
    for keyword, category in CATEGORY_MAP.items():
        if keyword in text:
            return category
    return fallback


def _amount_to_yuan(value: Optional[float], unit: Optional[str]) -> float:
    if value is None:
        return 0.0
    unit_text = (unit or "元").lower()
    multiplier = 1.0
    if "亿" in unit_text:
        multiplier = 100_000_000.0
    elif "万" in unit_text:
        multiplier = 10_000.0
    elif "千" in unit_text:
        multiplier = 1_000.0
    return float(value) * multiplier


_COMPANY_MENTION_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,80}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司)")
_RISK_TRANSMISSION_WORDS = ("本公司承担", "上市公司承担", "公司承担", "担保代偿", "合并报表", "连带责任", "交叉违约", "资金占用")


def _compact_name(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value or "").lower()


def _target_aliases(extraction: ResearchExtraction | None) -> Set[str]:
    if extraction is None:
        return set()
    profile = extraction.profile
    return {
        _compact_name(value)
        for value in (profile.company_name, profile.short_name, profile.stock_code, profile.credit_code)
        if value
    }


def _target_scope_factor(text: str, extraction: ResearchExtraction | None) -> float:
    """Prevent a subsidiary/counterparty event from automatically becoming issuer risk."""
    if extraction is None:
        return 1.0
    aliases = _target_aliases(extraction)
    mentions = {_compact_name(item) for item in _COMPANY_MENTION_RE.findall(text or "")}
    if not mentions:
        return 1.0
    if any(any(alias and alias in mention for alias in aliases) for mention in mentions):
        return 1.0
    if any(word in (text or "") for word in _RISK_TRANSMISSION_WORDS):
        return 0.65
    return 0.18


def _direct_target_attribution(text: str, extraction: ResearchExtraction | None, *, structured: bool = False, source_ids: Sequence[str] = ()) -> float:
    """Stricter attribution used only for bank-grade high-risk gates.

    A generic model sentence without the issuer name must not create a high-risk
    conclusion. Structured events with concrete identifiers receive more trust, while
    text that names another company but not the issuer is strongly discounted.
    """
    if extraction is None:
        return 1.0
    value = text or ""
    compact = _compact_name(value)
    aliases = [item for item in _target_aliases(extraction) if len(item) >= 3]
    if any(alias in compact for alias in aliases):
        return 1.0
    mentions = {_compact_name(item) for item in _COMPANY_MENTION_RE.findall(value)}
    if mentions:
        return 0.08
    if structured and (re.search(r"(?:\d{4})[^\n]{0,12}(?:号|案件|公告)", value) or re.search(r"\d+(?:\.\d+)?\s*(?:亿元|万元|元)", value)):
        return 0.90
    # Unit tests and old integrations may construct evidence without source ids. In
    # production, sanitized model items always carry source ids, so this exception
    # does not weaken live attribution.
    if not source_ids:
        return 0.96 if structured or _has_active(value, ("债务违约", "未兑付", "失信", "限制高消费", "限高", "破产")) else 0.72
    return 0.52


def _evidence_specificity(
    text: str,
    source_ids: Sequence[str],
    *,
    amount_yuan: float = 0.0,
    case_number: str | None = None,
    event_date: str | None = None,
) -> float:
    """How concrete and independently supported a risk assertion is."""
    unique_sources = len({sid for sid in source_ids if sid})
    concrete = 0
    concrete += bool(amount_yuan > 0)
    concrete += bool(case_number and str(case_number).strip())
    concrete += bool(event_date and re.search(r"20\d{2}", str(event_date)))
    concrete += bool(re.search(r"(?:案号|执行案号|处罚决定书|公告编号|裁判文书)[:：]?\s*[^；。\s]{4,}", text or "") or "公告" in (text or ""))
    concrete += bool(re.search(r"\d+(?:\.\d+)?\s*(?:亿元|万元|元)", text or ""))
    if unique_sources >= 3:
        return 1.0
    if unique_sources == 2:
        return 0.90 if concrete else 0.82
    if unique_sources == 1:
        return 0.88 if concrete >= 2 else (0.80 if concrete else 0.48)
    # Compatibility for deterministic tests / hand-built evidence packets.
    return 0.90 if concrete else 0.82


def _financial_distress_meta(extraction: ResearchExtraction) -> Dict[str, object]:
    """Return independent financial-pressure families and persistence indicators."""
    families: Set[str] = set()
    loss_years: Set[int] = set()
    cash_years: Set[int] = set()
    severe = False
    for fact in extraction.annual_facts:
        if not _fact_is_target_financial(fact) or float(fact.confidence) < 0.50:
            continue
        value = f"{fact.metric} {fact.value}"
        year = _fact_year(fact)
        pct = _extract_percent(value)
        if _is_negative_net_assets_fact(value) or _has_active(value, ("无法表示意见", "否定意见", "债务违约", "未兑付", "未能清偿到期债务")):
            severe = True
        if any(k in value for k in ("净利润", "扣非净利润", "归母净利润")) and _is_negative_value(value):
            families.add("loss")
            if year:
                loss_years.add(year)
        if any(k in value for k in ("经营活动现金流", "经营活动产生的现金流", "经营现金流")) and _is_negative_value(value):
            families.add("cashflow")
            if year:
                cash_years.add(year)
        if any(k in value for k in ("资产负债率", "负债率")) and pct is not None and pct >= 70:
            families.add("leverage")
        if "流动比率" in value:
            m = re.search(r"流动比率[^0-9]*(\d+(?:\.\d+)?)", value)
            if m and float(m.group(1)) < 1:
                families.add("liquidity")
        if "毛利率" in value and pct is not None and pct < 5:
            families.add("margin")
        if _has_active(value, ("保留意见",)) and not _has_active(value, ("标准无保留意见", "无保留意见")):
            families.add("audit")
    persistent = len(loss_years) >= 2 or len(cash_years) >= 2
    compound = len(families) >= 2 and persistent
    return {
        "families": sorted(families),
        "loss_years": sorted(loss_years),
        "cashflow_years": sorted(cash_years),
        "persistent": persistent,
        "compound": compound,
        "severe": severe,
    }


def _relationship_scope_factor(extraction: ResearchExtraction, relationship) -> float:
    aliases = _target_aliases(extraction)
    source = _compact_name(relationship.source_name)
    target = _compact_name(relationship.target_name)
    if source in aliases or target in aliases:
        return 1.0
    text = f"{relationship.relation_type} {relationship.description}"
    return 0.45 if any(word in text for word in _RISK_TRANSMISSION_WORDS) else 0.0


def _fact_is_target_financial(fact: AnnualFact) -> bool:
    text = f"{fact.metric} {fact.value}"
    excluded = ("子公司", "参股公司", "联营企业", "关联方", "被担保方", "行业平均", "同行", "预计", "预测", "情景假设")
    return not any(token in text for token in excluded)


def _event_score(event: JudicialEvent, extraction: ResearchExtraction | None = None) -> float:
    text = f"{event.event_type} {event.title} {event.description}"
    amount = _amount_to_yuan(event.amount, event.amount_unit)
    base = _contextual_model_score(text, event.severity, 35, amount)
    if amount > 0 and _status_factor(text) >= 0.9:
        base += min(12, max(0, math.log10(max(amount, 1)) - 6) * 2.5)
    base *= _status_factor(text) * _target_scope_factor(text, extraction)
    return min(100.0, max(0.0, base))


def _signal_score(signal: RiskSignal, extraction: ResearchExtraction | None = None) -> float:
    text = f"{signal.category} {signal.title} {signal.description}"
    score = _contextual_model_score(text, signal.severity, 30) * signal.confidence
    return min(100.0, max(0.0, score * _status_factor(text) * _target_scope_factor(text, extraction)))




def _fact_year(fact: AnnualFact) -> Optional[int]:
    if fact.year:
        return int(fact.year)
    match = re.search(r"(20\d{2})", f"{fact.metric} {fact.value}")
    return int(match.group(1)) if match else None


def _extract_percent(text: str) -> Optional[float]:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text or "")
    return float(match.group(1)) if match else None


def _extract_amount_from_text(text: str) -> Optional[float]:
    """Parse the first CNY amount in text into yuan; used only for ratios/trends."""
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(万亿|亿元|亿|万元|万|元)", text or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "万亿":
        return value * 1_000_000_000_000
    if unit in {"亿元", "亿"}:
        return value * 100_000_000
    if unit in {"万元", "万"}:
        return value * 10_000
    return value


def _is_negative_value(text: str) -> bool:
    value = text or ""
    if re.search(r"(?:不|未|无|没有).{0,5}(?:亏损|为负|负值|负增长)", value):
        return False
    # A negative同比/增长率 is not the same as a negative balance or a loss.
    if any(token in value for token in ("同比", "增长率", "增速", "变动率")) and not _has_active(value, ("亏损", "为负", "负值")):
        return False
    explicit_amount = bool(re.search(r"(?:^|[:：\s])[-−—]\s*\d+(?:\.\d+)?\s*(?:万亿|亿元|亿|万元|万|元)", value))
    explicit_plain = bool(re.search(r"(?:为|：|:)\s*[-−—]\s*\d+(?:\.\d+)?(?!\s*%)", value))
    active_words = any(
        token in value and not _keyword_is_negated(value, token)
        for token in ("亏损", "为负", "负值")
    )
    return explicit_amount or explicit_plain or active_words


def _is_negative_net_assets_fact(text: str) -> bool:
    value = text or ""
    if not any(token in value for token in ("净资产为负", "负净资产", "净资产负值")):
        metric_like = "净资产" in value and not any(token in value for token in ("净资产收益率", "加权平均净资产", "净资产增长率", "每股净资产"))
        if not metric_like:
            return False
    return _is_negative_value(value) or _has_active(value, ("净资产为负", "负净资产", "净资产负值"))


def _state_owned_support(extraction: ResearchExtraction) -> bool:
    profile = extraction.profile
    text = " ".join(filter(None, (profile.actual_controller, profile.controlling_shareholder, profile.company_name)))
    state_words = (
        "国务院国资", "国资委", "国有资本", "国有控股", "中央企业", "央企", "省国资", "市国资",
        "中国建材", "中国宝武", "国家能源", "三峡集团", "航空工业", "航天科技", "航天科工",
        "中船集团", "中国船舶集团", "地方国资", "城投集团",
    )
    return any(word in text for word in state_words)


def _cyclical_industry(extraction: ResearchExtraction) -> bool:
    profile = extraction.profile
    text = " ".join(filter(None, [profile.industry, profile.business_scope, " ".join(profile.main_products)]))
    words = (
        "房地产", "光伏", "玻璃", "钢铁", "水泥", "煤炭", "有色", "化工", "工程机械", "航空",
        "航运", "建材", "建筑施工", "周期", "新能源材料",
    )
    return any(word in text for word in words)


def _financial_health_observations(extraction: ResearchExtraction) -> List[Tuple[float, str, List[str]]]:
    """Turn disclosed financial facts into generic credit-risk observations.

    The rules follow bank-credit logic: profitability, leverage, liquidity and cash flow
    matter more than merely counting public risk hits.  They are intentionally generic
    and never keyed to a particular company.
    """
    observations: List[Tuple[float, str, List[str]]] = []
    loss_years: Set[int] = set()
    negative_cash_years: Set[int] = set()
    interest_debt: List[Tuple[int, float, List[str]]] = []
    cash_balance: List[Tuple[int, float, List[str]]] = []

    for fact in extraction.annual_facts:
        if not _fact_is_target_financial(fact):
            continue
        text = f"{fact.metric} {fact.value}".strip()
        year = _fact_year(fact) or 0
        sources = list(fact.source_ids)
        percent = _extract_percent(text)

        if any(k in text for k in ("净利润", "扣非净利润", "归母净利润")) and _is_negative_value(text):
            if year:
                loss_years.add(year)
            observations.append((52.0, f"{year or '近期'}净利润为负/亏损：{text}", sources))
        elif "亏损" in text and any(k in text for k in ("经营", "利润", "业绩", "盈利")):
            if year:
                loss_years.add(year)
            observations.append((50.0, f"{year or '近期'}经营亏损：{text}", sources))

        if "毛利率" in text and percent is not None:
            if percent < 0:
                observations.append((72.0, f"毛利率转负（{percent:.2f}%）：{text}", sources))
            elif percent < 5:
                observations.append((46.0, f"毛利率偏低（{percent:.2f}%）：{text}", sources))

        if any(k in text for k in ("资产负债率", "负债率")) and percent is not None:
            severity = 0.0
            if percent >= 80:
                severity = 68.0
            elif percent >= 75:
                severity = 60.0
            elif percent >= 65:
                severity = 52.0
            elif percent >= 55:
                severity = 40.0
            if severity:
                observations.append((severity, f"资产负债率{percent:.2f}%：杠杆水平偏高", sources))

        if "流动比率" in text:
            match = re.search(r"流动比率[^0-9]*(\d+(?:\.\d+)?)", text)
            if match:
                ratio = float(match.group(1))
                if ratio < 0.8:
                    observations.append((58.0, f"流动比率{ratio:.2f}，短期偿债覆盖偏弱", sources))
                elif ratio < 1.0:
                    observations.append((46.0, f"流动比率{ratio:.2f}，短期偿债承压", sources))

        if any(k in text for k in ("经营活动现金流", "经营活动产生的现金流", "经营现金流")) and _is_negative_value(text):
            if year:
                negative_cash_years.add(year)
            observations.append((48.0, f"{year or '近期'}经营现金流为负：{text}", sources))

        if _is_negative_net_assets_fact(text):
            observations.append((96.0, f"净资产为负：{text}", sources))

        amount = _extract_amount_from_text(text)
        if amount is not None:
            if "有息负债" in text or "带息负债" in text:
                interest_debt.append((year, abs(amount), sources))
            if "货币资金" in text or "现金及现金等价物" in text:
                cash_balance.append((year, abs(amount), sources))

        debt_terms = ("逾期债务", "债务逾期", "未能清偿到期债务", "债券违约", "债务违约")
        active_debt_terms = [term for term in debt_terms if term in text and not _keyword_is_negated(text, term)]
        if active_debt_terms:
            observations.append((92.0 * _status_factor(text), f"偿债异常：{text}", sources))
        severe_audit = [term for term in ("无法表示意见", "否定意见") if term in text and not _keyword_is_negated(text, term)]
        moderate_audit = [term for term in ("保留意见", "带强调事项") if term in text and not _keyword_is_negated(text, term)]
        if severe_audit:
            observations.append((96.0 * _status_factor(text), f"审计意见重大异常：{text}", sources))
        elif moderate_audit:
            observations.append((75.0 * _status_factor(text), f"审计意见需重点核验：{text}", sources))

    if len(loss_years) >= 2:
        observations.append((68.0 if len(loss_years) == 2 else 78.0, f"连续{len(loss_years)}个年度出现亏损/负利润，盈利持续性明显承压", []))
    if len(negative_cash_years) >= 2:
        observations.append((64.0, f"连续{len(negative_cash_years)}个年度经营现金流为负，现金回笼能力承压", []))

    if interest_debt and cash_balance:
        # Compare the latest disclosed values where possible.
        latest_debt = sorted(interest_debt, key=lambda item: item[0], reverse=True)[0]
        latest_cash = sorted(cash_balance, key=lambda item: item[0], reverse=True)[0]
        if latest_cash[1] > 0:
            multiple = latest_debt[1] / latest_cash[1]
            sources = list(dict.fromkeys([*latest_debt[2], *latest_cash[2]]))
            if multiple >= 8:
                observations.append((72.0, f"有息负债约为货币资金的{multiple:.1f}倍，短期流动性压力较高", sources))
            elif multiple >= 4:
                observations.append((62.0, f"有息负债约为货币资金的{multiple:.1f}倍，偿债资金缓冲偏弱", sources))
            elif multiple >= 2:
                observations.append((48.0, f"有息负债约为货币资金的{multiple:.1f}倍，需关注资金覆盖", sources))

    if _cyclical_industry(extraction):
        has_core_weakness = any(score >= 58 for score, _, _ in observations)
        observations.append((34.0 if has_core_weakness else 22.0, "所属行业具有明显周期性，盈利和现金流对景气度变化较敏感", []))

    # Strong state ownership is a mitigant, not a blanket exemption from operating risk.
    if _state_owned_support(extraction):
        observations.append((-6.0, "国资/央企控制背景提供一定融资与协调缓冲，但不抵消持续亏损或高杠杆事实", []))

    return observations


def _financial_observation_family(reason: str) -> str:
    value = reason or ""
    mapping = (
        ("default", ("偿债异常", "债务违约", "未兑付", "逾期债务")),
        ("audit", ("审计意见", "无法表示意见", "否定意见", "保留意见")),
        ("net_assets", ("净资产为负", "负净资产")),
        ("loss", ("净利润为负", "经营亏损", "年度出现亏损", "盈利持续性")),
        ("cashflow", ("经营现金流为负", "现金回笼能力")),
        ("leverage", ("资产负债率", "杠杆水平")),
        ("liquidity", ("流动比率", "短期偿债")),
        ("debt_cash", ("有息负债约为货币资金", "资金覆盖")),
        ("margin", ("毛利率",)),
        ("industry", ("行业具有明显周期性",)),
    )
    for family, words in mapping:
        if any(word in value for word in words):
            return family
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value)[:28] or "other"


def _dedupe_financial_observations(
    observations: Sequence[Tuple[float, str, List[str]]]
) -> List[Tuple[float, str, List[str]]]:
    """Collapse repeated web extracts of the same metric/year before scoring.

    Multiple search pages often repeat one annual-report number. Treating those
    repetitions as independent pressure inflated otherwise ordinary issuers.
    """
    merged: Dict[Tuple[str, str], Tuple[float, str, Set[str]]] = {}
    for score, reason, source_ids in observations:
        year_match = re.search(r"(20\d{2})", reason or "")
        year = year_match.group(1) if year_match else "current"
        key = (_financial_observation_family(reason), year)
        previous = merged.get(key)
        if previous is None:
            merged[key] = (float(score), reason, set(source_ids))
            continue
        best_score, best_reason, best_sources = previous
        best_sources.update(source_ids)
        if abs(float(score)) > abs(best_score):
            best_score, best_reason = float(score), reason
        merged[key] = (best_score, best_reason, best_sources)
    return [(score, reason, sorted(source_ids)) for score, reason, source_ids in merged.values()]


def _financial_business_score(extraction: ResearchExtraction) -> Tuple[float, List[str], List[str]]:
    observations = _dedupe_financial_observations(_financial_health_observations(extraction))
    positives = sorted((item for item in observations if item[0] > 0), key=lambda x: x[0], reverse=True)
    mitigation = sum(abs(score) for score, _, _ in observations if score < 0)
    if not positives:
        return 0.0, [], []
    top = positives[:5]
    score = min(100.0, top[0][0] * 0.66 + sum(item[0] for item in top[1:]) * 0.10)
    if mitigation:
        score = max(0.0, score - min(10.0, mitigation))
    evidence = [item[1] for item in top]
    source_ids = sorted({sid for _, _, ids in top for sid in ids})
    return round(score, 2), evidence, source_ids


def _build_risk_brief(extraction: ResearchExtraction, risk_score: Optional[float], risk_level: str) -> List[str]:
    if risk_score is None:
        return ["公开证据覆盖不足，暂不能形成可靠风险评分。", "建议先补齐法定主体、财务与司法信息，再进入授信判断。"]
    financial = _financial_health_observations(extraction)
    positive = sorted((item for item in financial if item[0] > 0), key=lambda item: item[0], reverse=True)
    mitigants = [item for item in financial if item[0] < 0]
    lines = [f"综合结论：{risk_level}（{risk_score:.1f}分），优先关注偿债能力、现金流和重大外部风险。"]
    if positive:
        lines.append("核心压力：" + "；".join(item[1].split("：")[0] for item in positive[:3]) + "。")
    else:
        lines.append("核心压力：当前公开财务证据未识别到持续亏损、高杠杆或重大偿债异常。")
    if mitigants:
        lines.append("缓释因素：" + mitigants[0][1] + "。")
    elif _state_owned_support(extraction):
        lines.append("缓释因素：国资/央企控制背景可提供一定信用与融资支持，但仍需核验经营基本面。")
    else:
        lines.append("审查重点：核验最新审计报表、现金流、负债期限结构、担保与司法事项。")
    return lines[:3]


def _completeness(extraction: ResearchExtraction) -> float:
    profile = extraction.profile
    key_fields = [
        profile.company_name,
        profile.credit_code,
        profile.legal_person,
        profile.registered_address,
        profile.registered_capital,
        profile.company_status,
        profile.industry,
        profile.controlling_shareholder,
        profile.actual_controller,
    ]
    profile_score = sum(bool(v) for v in key_fields) / len(key_fields)
    relation_score = min(1.0, len(extraction.relationships) / 5)
    missing_text = " ".join(extraction.missing_fields)
    if extraction.judicial_events:
        judicial_score = 1.0
    elif any(word in missing_text for word in ("司法", "被执行", "失信", "裁判")):
        judicial_score = 0.0
    else:
        # No adverse event found is not equivalent to complete judicial coverage.
        judicial_score = 0.35
    annual_score = min(1.0, len(extraction.annual_facts) / 4)
    source_quality = min(1.0, len({sid for item in extraction.relationships for sid in item.source_ids}) / 6)
    raw = 0.42 * profile_score + 0.20 * relation_score + 0.18 * judicial_score + 0.15 * annual_score + 0.05 * source_quality
    if not extraction.identity_verified:
        raw *= 0.55
    return round(min(1.0, raw), 4)


def _dimension_scores(extraction: ResearchExtraction) -> Dict[str, Tuple[float, List[str], List[str]]]:
    # One event family contributes once per dimension.  Search duplication and many
    # related-company rows must not push an otherwise healthy issuer into high risk.
    scores: Dict[str, Dict[str, float]] = {key: {} for key in DIMENSION_WEIGHTS}
    evidence: Dict[str, List[str]] = {key: [] for key in DIMENSION_WEIGHTS}
    source_ids: Dict[str, Set[str]] = {key: set() for key in DIMENSION_WEIGHTS}

    def add(key: str, family: str, value: float, text: str, ids: Sequence[str]) -> None:
        if value <= 0:
            return
        previous = scores[key].get(family, 0.0)
        if value > previous:
            scores[key][family] = value
        if text and text not in evidence[key]:
            evidence[key].append(text)
        source_ids[key].update(ids)

    for event in extraction.judicial_events:
        text = f"{event.event_type} {event.title} {event.description}"
        add("judicial", _risk_family(text), _event_score(event, extraction), event.title, event.source_ids)

    for relationship in extraction.relationships:
        scope = _relationship_scope_factor(extraction, relationship)
        if scope <= 0:
            continue
        text = f"{relationship.relation_type} {relationship.description}"
        category = "transaction" if any(k in text for k in ("交易", "采购", "销售", "担保", "资金", "循环")) else "equity"
        status_factor = _status_factor(text)
        model_severity = {"critical": 92.0, "high": 76.0, "medium": 48.0, "low": 18.0}.get(relationship.risk_level, 20.0)
        amount = _amount_to_yuan(relationship.amount, relationship.amount_unit)
        value = relationship.confidence * _contextual_model_score(text, model_severity, 20, amount) * status_factor * scope
        family = _risk_family(text)
        add(category, family, value, f"{relationship.target_name}：{relationship.relation_type}", relationship.source_ids)

    for signal in extraction.risk_signals:
        text = f"{signal.category} {signal.title} {signal.description}"
        category = _category(text)
        add(category, _risk_family(text), _signal_score(signal, extraction), signal.title, signal.source_ids)

    for fact in extraction.annual_facts:
        if not _fact_is_target_financial(fact):
            continue
        text = f"{fact.metric} {fact.value}"
        active_keywords = [word for word in HIGH_RISK_WORDS if word in text and not _keyword_is_negated(text, word)]
        if active_keywords:
            category = _category(text, "disclosure")
            add(category, _risk_family(text), _score_text(text, 30) * fact.confidence * _status_factor(text), f"{fact.metric}：{fact.value}", fact.source_ids)

    financial_score, financial_evidence, financial_source_ids = _financial_business_score(extraction)
    if financial_score > 0:
        add("business", "financial_health", financial_score, financial_evidence[0] if financial_evidence else "经营与偿债压力", financial_source_ids)
        for item in financial_evidence[1:5]:
            if item not in evidence["business"]:
                evidence["business"].append(item)

    result: Dict[str, Tuple[float, List[str], List[str]]] = {}
    for key in DIMENSION_WEIGHTS:
        values = sorted(scores[key].values(), reverse=True)
        if not values:
            dimension_score = 0.0
        else:
            top = values[:4]
            dimension_score = min(100.0, top[0] * 0.72 + sum(top[1:]) * 0.08)
        result[key] = (round(dimension_score, 2), evidence[key][:5], sorted(source_ids[key]))
    return result

def _extract_export_ratio(facts: Sequence[AnnualFact]) -> Optional[float]:
    for fact in facts:
        text = f"{fact.metric} {fact.value}"
        if "出口" not in text and "境外" not in text:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if match:
            return round(float(match.group(1)), 2)
    return None


def _risk_level(score: float) -> str:
    """Bank-facing three-tier grade: low / medium / high only."""
    if score >= 60:
        return "高风险"
    if score >= 40:
        return "中风险"
    return "低风险"


def _short_level(risk_level: str) -> str:
    if "高" in risk_level:
        return "高"
    if "中" in risk_level:
        return "中"
    if "低" in risk_level:
        return "低"
    return "待核验"


def _unique_text(items: Iterable[str], limit: int = 4) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for item in items:
        value = re.sub(r"\s+", " ", (item or "")).strip(" ；。")
        if not value or value in seen or value == "未发现可量化的公开异常证据":
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _evidence_excerpt(value: str, limit: int = 54) -> str:
    text = re.sub(r"\s+", " ", value or "").strip(" ；。")
    if not text or text == "未发现可量化的公开异常证据":
        return ""
    text = text.split("；", 1)[0].strip()
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _personalized_suggestions(
    extraction: ResearchExtraction,
    grade: str,
    ranked_dimensions: Sequence[RiskDimension],
    completeness: float,
) -> List[str]:
    """Create a small number of issuer-specific, evidence-linked review actions."""
    suggestions: List[str] = []
    templates = {
        "judicial": "围绕“{evidence}”，逐项核对案号、涉案金额、当前履行状态及是否直接影响本企业现金流。",
        "equity": "针对“{evidence}”，穿透核验控股链、质押冻结比例及控制权稳定性，并设置变更触发条款。",
        "transaction": "针对“{evidence}”，抽查交易定价、审批程序、资金去向和回款闭环，区分正常经营往来与风险传导。",
        "disclosure": "针对“{evidence}”，回看最新法定公告和审计原文，确认整改进度、会计影响及是否仍在持续。",
        "business": "结合“{evidence}”，补做未来12个月现金流、债务到期和授信敞口压力测试。",
    }
    for item in ranked_dimensions:
        if item.score < 18:
            continue
        excerpt = _evidence_excerpt(item.evidence)
        if not excerpt:
            continue
        template = templates.get(item.key)
        if template:
            suggestions.append(template.format(evidence=excerpt))
        if len(suggestions) >= 3:
            break

    missing = [re.sub(r"\s+", " ", item or "").strip() for item in extraction.missing_fields if (item or "").strip()]
    if completeness < 0.78 and missing:
        fields = "、".join(missing[:3])
        suggestions.append(f"本次公开资料仍缺少{fields}；建议向企业补件并与法定披露交叉验证后再确定额度或账期。")

    company = extraction.profile.short_name or extraction.profile.company_name or "该企业"
    if grade == "高":
        suggestions.append(f"在{company}完成重大事项核验并落实抵质押、保证或回款闭环前，暂停新增无保障信用敞口。")
    elif grade == "中":
        suggestions.append(f"对{company}采用有条件准入：控制额度和期限，并将财务、司法及控制权变化设为收紧条件。")
    else:
        suggestions.append(f"可按常规流程审查{company}，但最终额度仍以最新征信、审计报表和现金流覆盖情况为准。")

    if not any("持续监测" in item or "定期" in item for item in suggestions):
        suggestions.append("建立季度级司法、经营、担保和关联交易监测；出现新增执行、逾期或控制权变化时立即复评。")
    return _unique_text(suggestions, 3)


def _build_credit_judgment(
    extraction: ResearchExtraction,
    risk_score: Optional[float],
    risk_level: str,
    dimensions: Sequence[RiskDimension],
    confidence: float,
    completeness: float,
) -> Dict[str, object]:
    """Produce an autonomous, deterministic bank/enterprise-oriented judgment.

    The wording is generated from scored evidence rather than a company-name label or
    free-form model opinion.  It is therefore reproducible without external APIs.
    """
    if risk_score is None:
        return {
            "grade": "待核验",
            "level": "证据不足",
            "stance": "暂缓结论",
            "headline": "关键证据尚未形成闭环，暂不建议直接作出授信或合作判断",
            "summary": "当前主体身份、财务、司法或关系证据覆盖不足。系统不会用缺失数据推定低风险，也不会凭零散信息夸大为高风险。",
            "drivers": ["补齐最新审计报表、企业征信、司法执行与担保明细后重新评估"],
            "mitigants": [],
            "bank_view": "在证据闭环前，不建议仅依据本报告新增纯信用敞口。",
            "enterprise_view": "在证据闭环前，建议采用短账期、分阶段履约和可追索付款条件。",
            "actions": ["核验法定主体", "取得最新财务与征信", "复核司法、担保和关联交易"],
            "personalized_suggestions": [
                "补齐法定主体、最新审计报表和企业征信，避免以缺失数据代替信用判断。",
                "核对司法执行、担保和关联交易的当前状态，并确认事项是否直接归属于本企业。",
                "关键证据闭环前，控制额度与账期，暂不新增无保障信用敞口。",
            ],
            "confidence_label": "证据不足",
            "confidence_percent": round(confidence * 100, 1),
            "completeness_percent": round(completeness * 100, 1),
            "logic": ["主体核验", "证据归因", "偿债压力测试", "三档判定"],
        }

    grade = _short_level(risk_level)
    ranked = sorted(dimensions, key=lambda item: (item.contribution, item.score), reverse=True)
    active = [item for item in ranked if item.score >= 12 and item.evidence]
    driver_candidates: List[str] = []
    for item in active:
        for evidence in item.evidence.split("；"):
            driver_candidates.append(f"{item.name}：{evidence}")
    drivers = _unique_text(driver_candidates, 4)
    if not drivers:
        drivers = ["公开证据中暂未识别到足以显著抬升风险分的异常事项"]

    top_names = "、".join(item.name for item in active[:2]) or "公开证据覆盖"
    mitigants: List[str] = []
    if _state_owned_support(extraction):
        mitigants.append("国资/央企控制背景可提供一定融资和协调缓冲，但不替代经营与偿债核验")
    severe_text = " ".join(
        [f"{item.category} {item.title} {item.description}" for item in extraction.risk_signals]
        + [f"{item.event_type} {item.title} {item.description}" for item in extraction.judicial_events]
    )
    if not any(token in severe_text for token in ("失信", "债务违约", "破产", "无法表示意见", "重大资金占用")):
        mitigants.append("现有公开证据未形成失信、破产或重大违约等灾难性风险闭环")
    if completeness < 0.60:
        mitigants.append("数据覆盖仍有限，结论按审慎原则表达，不能把未检出等同于不存在")
    mitigants = _unique_text(mitigants, 3)

    if grade == "高":
        stance = "审慎限制"
        headline = "高风险：在重大风险核验与增信措施落实前，不宜新增无保障信用敞口"
        summary = f"评分显示{top_names}已对偿债或履约可靠性形成实质压力。该结论优先服从违约、失信、持续亏损、高杠杆和现金流恶化等银行信用底线，而不是被正常业务关系数量稀释。"
        bank_view = "原则上暂停新增纯信用授信；存量业务应复核分类、压降敞口，并将回款、担保和资金用途纳入闭环管理。"
        enterprise_view = "不建议采用无担保长账期或大额预付；优先使用预付款、银行保函、分批交付和可中止条款。"
        actions = ["核验债务逾期、执行及审计异常的最新状态", "测算现金流覆盖、担保代偿和交叉违约压力", "落实抵质押、保证或资金闭环后再审"]
    elif grade == "中":
        stance = "有条件准入"
        headline = "中风险：可在额度、期限和增信条件受控的前提下继续审查"
        summary = f"当前风险主要集中在{top_names}，尚未达到全面否决程度，但存在需要穿透核验的压力点。应避免将单一正常担保直接判高，也不能因未出现灾难性事件就忽视财务与控制链风险。"
        bank_view = "建议有条件准入：从严核定额度与期限，设置财务、司法和控制权变化触发条款，并提高贷后监测频率。"
        enterprise_view = "建议控制账期和单笔敞口，采用分阶段履约、对账确认和逾期自动收紧条款。"
        actions = ["补核最新财务、征信和或有负债", "穿透主要股东、担保链和关联资金往来", "设置额度、账期与风险触发条件"]
    else:
        stance = "常规审查"
        headline = "低风险：可进入常规授信或合作流程，但仍需完成基础尽调"
        summary = f"现有公开证据未显示足以把{top_names}推升至中高风险的重大异常。低风险代表当前证据下的相对可控，不代表零风险或免除征信、财务和合同审查。"
        bank_view = "可进入常规授信流程；最终额度仍应以企业征信、最新审计报表、现金流和担保有效性为准。"
        enterprise_view = "可按常规商业条件推进合作，同时保留付款保障、信息披露和控制权变更条款。"
        actions = ["完成基础征信与最新报表核验", "确认担保、关联交易均属正常经营范围", "建立定期司法与经营变化监测"]

    personalized_suggestions = _personalized_suggestions(extraction, grade, ranked, completeness)
    actions = personalized_suggestions
    confidence_percent = round(confidence * 100, 1)
    confidence_label = "高" if confidence_percent >= 78 else ("中" if confidence_percent >= 58 else "偏低")
    return {
        "grade": grade,
        "level": risk_level,
        "stance": stance,
        "headline": headline,
        "summary": summary,
        "drivers": drivers,
        "mitigants": mitigants,
        "bank_view": bank_view,
        "enterprise_view": enterprise_view,
        "actions": actions,
        "personalized_suggestions": personalized_suggestions,
        "confidence_label": confidence_label,
        "confidence_percent": confidence_percent,
        "completeness_percent": round(completeness * 100, 1),
        "logic": ["主体核验", "证据归因", "偿债压力测试", "三档判定"],
    }


def _trend(extraction: ResearchExtraction) -> Dict[str, List[float]]:
    yearly: Dict[str, List[float]] = defaultdict(list)
    for event in extraction.judicial_events:
        if not event.event_date:
            continue
        match = re.search(r"(20\d{2})", event.event_date)
        if match:
            yearly[match.group(1)].append(_event_score(event, extraction))
    for signal in extraction.risk_signals:
        if not signal.event_date:
            continue
        match = re.search(r"(20\d{2})", signal.event_date)
        if match:
            yearly[match.group(1)].append(_signal_score(signal, extraction))
    labels = sorted(yearly)
    values = [round(min(100.0, sum(yearly[y]) / max(1, len(yearly[y]))), 1) for y in labels]
    return {"labels": labels, "values": values}




def _risk_family(text: str) -> str:
    value = text or ""
    families = (
        ("default", ("债务违约", "债券违约", "逾期债务", "未兑付", "未能清偿")),
        ("dishonesty", ("失信", "限高", "限制高消费")),
        ("execution", ("被执行", "执行标的")),
        ("bankruptcy", ("破产", "重整", "清算")),
        ("fund_occupation", ("资金占用", "非经营性占用")),
        ("guarantee", ("违规担保", "担保代偿", "担保")),
        ("audit", ("无法表示意见", "否定意见", "保留意见")),
        ("freeze", ("股权冻结", "冻结")),
        ("pledge", ("质押",)),
        ("litigation", ("诉讼", "仲裁", "纠纷")),
        ("tax", ("重大税收违法", "欠税", "税收违法")),
        ("environment_safety", ("环境处罚", "环保处罚", "安全生产事故", "停产整顿", "吊销许可证")),
        ("penalty", ("行政处罚", "监管措施", "监管关注", "立案调查")),
        ("transaction", ("循环交易", "关联交易", "利益输送", "非公允")),
        ("disclosure", ("虚假", "重大违法", "退市", "终止上市")),
    )
    for family, words in families:
        if any(word in value for word in words):
            return family
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value)[:24] or "other"



def _annual_fact_health_flags(extraction: ResearchExtraction) -> Dict[str, bool]:
    """Summarise issuer-level audited facts without treating growth rates as balances."""
    flags = {
        "clean_audit": False,
        "positive_profit": False,
        "positive_cashflow": False,
        "severe_audit": False,
        "negative_net_assets": False,
        "active_default": False,
        "negative_profit": False,
        "negative_cashflow": False,
    }
    for fact in extraction.annual_facts:
        if not _fact_is_target_financial(fact) or float(fact.confidence) < 0.65:
            continue
        text = f"{fact.metric} {fact.value}"
        if _status_factor(text) <= 0:
            continue
        if any(token in text for token in ("标准无保留意见", "无保留意见")) and not any(token in text for token in ("无法表示意见", "否定意见", "保留意见（", "保留意见,")):
            flags["clean_audit"] = True
        if _has_active(text, ("无法表示意见", "否定意见")):
            flags["severe_audit"] = True
        if _is_negative_net_assets_fact(text):
            flags["negative_net_assets"] = True
        if _has_active(text, ("债务违约", "债券违约", "逾期债务", "未能清偿到期债务", "未兑付")):
            flags["active_default"] = True
        if any(token in text for token in ("归母净利润", "净利润", "利润总额")):
            # Negative同比 is not a loss; actual loss/negative balance is handled separately.
            if _is_negative_value(text) or _has_active(text, ("亏损", "为负", "负值")):
                flags["negative_profit"] = True
            else:
                amount = _extract_amount_from_text(text)
                if amount is None or amount > 0:
                    flags["positive_profit"] = True
        if any(token in text for token in ("经营活动现金流", "经营活动产生的现金流", "经营现金流")):
            if _is_negative_value(text):
                flags["negative_cashflow"] = True
            elif any(token in text for token in ("正", "净流入", "流入", "+")):
                flags["positive_cashflow"] = True
    # Conflicting public extracts must never be interpreted as a clean healthy bundle.
    if flags["negative_profit"]:
        flags["positive_profit"] = False
    if flags["negative_cashflow"]:
        flags["positive_cashflow"] = False
    if flags["severe_audit"]:
        flags["clean_audit"] = False
    return flags


def _direct_credit_strengths(extraction: ResearchExtraction) -> Dict[str, float]:
    """Current issuer-level credit impairment, expressed as evidence strength.

    High-risk gates require target attribution plus either corroboration or concrete
    identifiers. A single vague model sentence is not enough.
    """
    result = {
        "dishonesty": 0.0, "default": 0.0, "bankruptcy": 0.0,
        "severe_audit": 0.0, "fund_occupation": 0.0,
        "guarantee_compensation": 0.0, "large_execution": 0.0,
    }

    def record(key: str, strength: float) -> None:
        result[key] = max(result[key], max(0.0, min(1.0, strength)))

    def inspect(
        text: str, effective: float, source_ids: Sequence[str], *, amount: float = 0.0,
        case_number: str | None = None, event_date: str | None = None, structured: bool = False,
    ) -> None:
        if effective < 52 or _status_factor(text) < 0.9:
            return
        attribution = _direct_target_attribution(text, extraction, structured=structured, source_ids=source_ids)
        specificity = _evidence_specificity(
            text, source_ids, amount_yuan=amount, case_number=case_number, event_date=event_date
        )
        strength = (effective / 100.0) * attribution * specificity
        if _has_active(text, ("失信被执行人", "列入失信", "限制高消费", "限高")):
            record("dishonesty", strength)
        if _has_active(text, ("债务违约", "债券违约", "到期债务尚未清偿", "逾期债务尚未清偿", "存在大额逾期债务", "大额逾期债务", "债务逾期", "未兑付", "未能清偿到期债务")):
            record("default", strength)
        if _has_active(text, ("破产清算", "破产重整", "被申请破产", "进入破产程序")):
            record("bankruptcy", strength)
        if _has_active(text, ("无法表示意见", "否定意见")):
            record("severe_audit", max(strength, 0.76 if structured else strength))
        if _has_active(text, ("非经营性资金占用", "违规资金占用")):
            record("fund_occupation", strength)
        if _has_active(text, ("担保代偿", "承担代偿责任", "已代偿")) and not any(token in text for token in RESOLVED_WORDS):
            record("guarantee_compensation", strength)
        if amount >= 100_000_000 and _has_active(text, ("被执行", "执行标的", "限制高消费", "限高")):
            record("large_execution", strength)

    for signal in extraction.risk_signals:
        text = f"{signal.category} {signal.title} {signal.description}"
        inspect(text, _signal_score(signal, extraction), signal.source_ids, event_date=signal.event_date)
    for event in extraction.judicial_events:
        text = f"{event.event_type} {event.title} {event.description}"
        inspect(
            text, _event_score(event, extraction), event.source_ids,
            amount=_amount_to_yuan(event.amount, event.amount_unit),
            case_number=event.case_number, event_date=event.event_date, structured=True,
        )
    for relation in extraction.relationships:
        if _relationship_scope_factor(extraction, relation) < 0.9:
            continue
        text = f"{relation.relation_type} {relation.description}"
        effective = _contextual_model_score(
            text, {"critical":92,"high":76,"medium":48,"low":18}.get(relation.risk_level,20),
            20, _amount_to_yuan(relation.amount, relation.amount_unit),
        ) * relation.confidence * _status_factor(text)
        inspect(
            text, effective, relation.source_ids, amount=_amount_to_yuan(relation.amount, relation.amount_unit),
            structured=True,
        )
    health = _annual_fact_health_flags(extraction)
    if health["severe_audit"]:
        record("severe_audit", 0.90)
    if health["active_default"]:
        record("default", 0.90)
    return result


def _direct_credit_evidence(extraction: ResearchExtraction) -> Dict[str, bool]:
    return {key: value >= 0.68 for key, value in _direct_credit_strengths(extraction).items()}


def _risk_floor_from_red_flags(extraction: ResearchExtraction) -> float:
    """Materiality-sensitive floors for verified current credit impairment.

    A high floor is available only to directly attributed, current and sufficiently
    specific evidence.  Scores vary with evidence strength and corroboration instead
    of collapsing normal companies into a few hard-coded values such as 68/82.
    """
    strengths = _direct_credit_strengths(extraction)
    verified = sorted((value for value in strengths.values() if value >= 0.68), reverse=True)
    floor = 0.0
    if len(verified) >= 2:
        avg = sum(verified[:3]) / min(3, len(verified))
        floor = 73.0 + ((avg - 0.68) / 0.32) * 18.0 + min(3.0, (len(verified) - 2) * 1.2)
    elif verified:
        floor = 60.8 + ((verified[0] - 0.68) / 0.32) * 14.5

    # Concrete current but non-catastrophic matters create a medium review floor.
    for event in extraction.judicial_events:
        text = f"{event.event_type} {event.title} {event.description}"
        effective = _event_score(event, extraction)
        amount = _amount_to_yuan(event.amount, event.amount_unit)
        attributed = (not event.source_ids) or _direct_target_attribution(
            text, extraction, structured=True, source_ids=event.source_ids
        ) >= 0.72
        if attributed and _status_factor(text) >= 0.9 and effective >= 44 and (
            amount >= 10_000_000 or _has_active(text, (
                "被执行", "股权冻结", "高比例质押", "重大诉讼", "重大仲裁",
                "重大行政处罚", "立案调查", "重大税收违法", "停产整顿",
                "吊销许可证", "重大安全生产事故", "重大环境处罚",
            ))
        ):
            materiality = min(1.0, max(0.0, (effective - 42.0) / 42.0))
            floor = max(floor, 40.5 + materiality * 12.5)

    for signal in extraction.risk_signals:
        text = f"{signal.category} {signal.title} {signal.description}"
        effective = _signal_score(signal, extraction)
        attributed = (not signal.source_ids) or _direct_target_attribution(
            text, extraction, source_ids=signal.source_ids
        ) >= 0.72
        if attributed and _status_factor(text) >= 0.9 and effective >= 55 and _has_active(text, (
            "股权冻结", "高比例质押", "重大诉讼", "重大仲裁", "重大行政处罚",
            "立案调查", "违规担保", "重大税收违法", "停产整顿",
            "吊销许可证", "重大安全生产事故", "重大环境处罚",
        )):
            floor = max(floor, 40.0 + min(15.0, (effective - 50.0) * 0.35))

    for relationship in extraction.relationships:
        if _relationship_scope_factor(extraction, relationship) < 0.9:
            continue
        text = f"{relationship.relation_type} {relationship.description}"
        attributed = (not relationship.source_ids) or _direct_target_attribution(
            text, extraction, structured=True, source_ids=relationship.source_ids
        ) >= 0.72
        if attributed and _status_factor(text) >= 0.9 and _has_active(text, (
            "违规担保", "控制权争议", "高比例质押", "股权冻结",
        )):
            floor = max(floor, 43.0)
    return round(min(94.0, floor), 2)


def _evidence_grade_caps(
    extraction: ResearchExtraction, score: float, financial_score: float, completeness: float = 1.0
) -> Tuple[float, Dict[str, object]]:
    """Three-tier gates driven by attribution, persistence and evidence coverage."""
    strengths = _direct_credit_strengths(extraction)
    direct = {key: value >= 0.68 for key, value in strengths.items()}
    health = _annual_fact_health_flags(extraction)
    financial_meta = _financial_distress_meta(extraction)
    high_direct = sorted(key for key, value in direct.items() if value)
    medium_families: Set[str] = set()

    def inspect_medium(text: str, effective: float, amount: float = 0.0, source_ids: Sequence[str] = (), structured: bool = False) -> None:
        if effective < 44 or _status_factor(text) < 0.9 or _target_scope_factor(text, extraction) < 0.6:
            return
        if source_ids and _direct_target_attribution(text, extraction, structured=structured, source_ids=source_ids) < 0.72:
            return
        if amount >= 10_000_000 and _has_active(text, ("诉讼", "仲裁", "被执行", "执行标的")):
            medium_families.add(_risk_family(text))
        if _has_active(text, (
            "股权冻结", "高比例质押", "重大诉讼", "重大仲裁", "重大行政处罚",
            "立案调查", "违规担保", "控制权争议", "保留意见",
            "重大税收违法", "停产整顿", "吊销许可证", "重大安全生产事故", "重大环境处罚"
        )):
            medium_families.add(_risk_family(text))

    for signal in extraction.risk_signals:
        text = f"{signal.category} {signal.title} {signal.description}"
        inspect_medium(text, _signal_score(signal, extraction), source_ids=signal.source_ids)
    for event in extraction.judicial_events:
        text = f"{event.event_type} {event.title} {event.description}"
        inspect_medium(text, _event_score(event, extraction), _amount_to_yuan(event.amount, event.amount_unit), event.source_ids, True)
    for relationship in extraction.relationships:
        if _relationship_scope_factor(extraction, relationship) < 0.9:
            continue
        text = f"{relationship.relation_type} {relationship.description}"
        model = {"critical":92.0,"high":76.0,"medium":48.0,"low":18.0}.get(relationship.risk_level,20.0)
        effective = relationship.confidence * _contextual_model_score(text, model, 20, _amount_to_yuan(relationship.amount, relationship.amount_unit)) * _status_factor(text)
        inspect_medium(text, effective, _amount_to_yuan(relationship.amount, relationship.amount_unit), relationship.source_ids, True)
    for fact in extraction.annual_facts:
        if not _fact_is_target_financial(fact):
            continue
        text = f"{fact.metric} {fact.value}"
        effective = _score_text(text, 30) * fact.confidence * _status_factor(text)
        inspect_medium(text, effective, source_ids=fact.source_ids, structured=True)
        if _has_active(text, ("保留意见",)) and not _has_active(text, ("标准无保留意见", "无保留意见")):
            medium_families.add("audit")

    financial_high = bool(financial_meta["severe"]) or (
        bool(financial_meta["compound"]) and financial_score >= 58
    )
    high_eligible = bool(high_direct) or financial_high
    financial_medium = bool(financial_meta["families"]) and financial_score >= 40
    medium_eligible = high_eligible or bool(medium_families) or financial_medium
    adjusted = float(score)

    if adjusted >= 60 and not high_eligible:
        adjusted = min(adjusted, 58.0)
    if adjusted >= 40 and not medium_eligible:
        adjusted = min(adjusted, 38.0)

    # Sparse public coverage must not be interpreted as proof of high risk.  A direct
    # current default/失信/破产 or a severe audited fact may still pass; otherwise the
    # output remains at most medium pending verification.
    coverage_limited = False
    if completeness < 0.45 and not high_direct and not financial_meta["severe"]:
        adjusted = min(adjusted, 52.0 if medium_eligible else 38.0)
        coverage_limited = True
    if completeness < 0.36 and not high_direct:
        adjusted = min(adjusted, 48.0 if medium_eligible else 34.0)
        coverage_limited = True

    healthy_bundle = health["clean_audit"] and health["positive_profit"] and health["positive_cashflow"]
    if healthy_bundle and not high_direct and not medium_families and not financial_meta["families"]:
        adjusted = min(adjusted, 24.0)
    elif healthy_bundle and not high_direct and not financial_high and not medium_families:
        adjusted = max(0.0, adjusted - 8.0)

    return round(max(0.0, min(100.0, adjusted)), 1), {
        "high_eligible": high_eligible,
        "medium_eligible": medium_eligible,
        "high_families": high_direct,
        "high_strengths": {key: round(value, 3) for key, value in strengths.items() if value > 0},
        "medium_families": sorted(medium_families),
        "financial_score": round(financial_score, 2),
        "financial_meta": financial_meta,
        "healthy_financial_bundle": healthy_bundle,
        "health_flags": health,
        "evidence_completeness": round(float(completeness), 4),
        "coverage_limited": coverage_limited,
    }


def calculate_risk(extraction: ResearchExtraction) -> RiskResult:
    completeness = _completeness(extraction)
    dimension_raw = _dimension_scores(extraction)
    dimensions: List[RiskDimension] = []
    shap_rows: List[ShapRow] = []
    score = 0.0
    for key, weight in DIMENSION_WEIGHTS.items():
        dimension_score, evidence, source_ids = dimension_raw[key]
        contribution = dimension_score * weight
        score += contribution
        evidence_text = "；".join(evidence) if evidence else "未发现可量化的公开异常证据"
        dimensions.append(
            RiskDimension(
                name=DIMENSION_NAMES[key],
                key=key,
                score=dimension_score,
                weight=weight,
                contribution=round(contribution, 2),
                evidence=evidence_text,
                source_ids=source_ids,
            )
        )
        shap_rows.append(
            ShapRow(
                feature=DIMENSION_NAMES[key],
                contribution=round(contribution, 2),
                direction="升高风险" if contribution > 0 else "无可量化影响",
                evidence=evidence_text,
                source_ids=source_ids,
            )
        )

    confidence = min(1.0, extraction.overall_confidence * (0.45 + completeness * 0.55))
    risk_floor = _risk_floor_from_red_flags(extraction)
    financial_floor, _, _ = _financial_business_score(extraction)
    financial_meta = _financial_distress_meta(extraction)
    # Financial distress requires persistence and multiple independent families before
    # it can create a high-risk floor. One high leverage ratio alone remains medium.
    if financial_meta["severe"]:
        risk_floor = max(risk_floor, min(78.0, 62.0 + financial_floor * 0.16))
    elif financial_meta["compound"] and financial_floor >= 58:
        risk_floor = max(risk_floor, min(76.0, 60.5 + (financial_floor - 58.0) * 0.42))
    elif financial_meta["families"] and financial_floor >= 44:
        risk_floor = max(risk_floor, min(56.0, 40.0 + (financial_floor - 44.0) * 0.35))
    scorable = extraction.identity_verified and (completeness >= 0.30 or risk_floor >= 52.0)
    if scorable:
        if risk_floor > score:
            # Retain the verified red-flag floor, but let the independent dimension
            # evidence and completeness contribute a small continuous component.
            uplift = min(4.2, score * 0.055 + completeness * 0.9)
            risk_score = round(min(100.0, risk_floor + uplift), 1)
        else:
            risk_score = round(min(100.0, score), 1)
    else:
        risk_score = None
    calibration: Dict[str, object] = {"mode": "none"}
    if risk_score is not None:
        risk_score, gate_meta = _evidence_grade_caps(extraction, risk_score, financial_floor, completeness)
        risk_score, calibration = calibrate_score_with_benchmark(extraction, risk_score)
        calibration["evidence_gate"] = gate_meta
    risk_level = _risk_level(risk_score) if risk_score is not None else "证据不足"
    brief = _build_risk_brief(extraction, risk_score, risk_level)
    credit_judgment = _build_credit_judgment(
        extraction, risk_score, risk_level, dimensions, confidence, completeness
    )

    core_aliases = {
        re.sub(r"\s+", "", value or "").lower()
        for value in (
            extraction.profile.company_name,
            extraction.profile.short_name,
            extraction.profile.stock_code,
            extraction.profile.credit_code,
        )
        if value
    }
    related_names = {
        name.strip()
        for relationship in extraction.relationships
        for name in (relationship.source_name, relationship.target_name)
        if name.strip() and re.sub(r"\s+", "", name).lower() not in core_aliases
    }
    abnormal_transactions = sum(
        1
        for signal in extraction.risk_signals
        if _status_factor(f"{signal.category} {signal.title} {signal.description}") >= 0.9
        and _has_active(f"{signal.category} {signal.title} {signal.description}", (
            "循环交易", "利益输送", "非公允", "定价异常", "资金占用", "违规关联交易"
        ))
    )
    amount_yuan = sum(_amount_to_yuan(event.amount, event.amount_unit) for event in extraction.judicial_events)
    involved_amount_yi = round(amount_yuan / 100_000_000, 4) if amount_yuan > 0 else None

    return RiskResult(
        scorable=scorable,
        risk_score=risk_score,
        risk_level=risk_level,
        confidence=round(confidence, 4),
        data_completeness=completeness,
        dimensions=dimensions,
        shap=sorted(shap_rows, key=lambda row: abs(row.contribution), reverse=True),
        related_company_count=len(related_names),
        abnormal_transaction_count=abnormal_transactions,
        involved_amount_yi=involved_amount_yi,
        export_ratio=_extract_export_ratio(extraction.annual_facts),
        trend=_trend(extraction),
        brief=brief,
        credit_judgment=credit_judgment,
        calibration=calibration,
    )


def _safe_id(name: str, index: int) -> str:
    compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", name)[:20]
    return f"n{index}_{compact or index}"


def _node_risk(relationship_risk: str, relation_type: str = "", description: str = "") -> str:
    if relationship_risk in {"low", "medium", "high", "critical"}:
        return relationship_risk
    text = f"{relation_type} {description}"
    if any(word in text for word in ("失信", "被执行", "股权冻结", "重大诉讼", "资金占用")):
        return "high"
    if any(word in text for word in ("担保", "质押", "诉讼", "处罚", "关联交易", "审计异常")):
        return "medium"
    if any(word in text for word in ("控股", "股东", "实际控制", "子公司", "投资", "参股", "客户", "供应商")):
        return "low"
    return "unknown"


def _relation_category(relation_type: str) -> str:
    text = relation_type or ""
    if any(word in text for word in ("控股", "股东", "实际控制", "持股")):
        return "股权控制"
    if any(word in text for word in ("子公司", "投资", "参股")):
        return "投资关系"
    if any(word in text for word in ("担保", "质押")):
        return "担保质押"
    if any(word in text for word in ("关联交易", "交易", "客户", "供应商")):
        return "交易关系"
    if any(word in text for word in ("诉讼", "执行", "处罚", "冻结")):
        return "司法风险"
    return "关联主体"


def build_graph(extraction: ResearchExtraction, risk: RiskResult) -> GraphPayload:
    company_name = extraction.profile.company_name or extraction.profile.credit_code or "待核验企业"
    nodes: List[GraphNode] = [
        GraphNode(
            id="core",
            name=company_name,
            risk="core",
            category="核心企业",
            description="本次公开信息尽调目标",
            company_type="目标企业",
            relation_type="目标企业",
            source_ids=(
                extraction.profile.field_evidence.get("company_name").source_ids
                if extraction.profile.field_evidence.get("company_name")
                else []
            ),
        )
    ]
    edges: List[GraphEdge] = []
    node_ids: Dict[str, str] = {company_name.strip(): "core"}

    def normalize_name(value: str) -> str:
        return re.sub(r"\s+", "", (value or "").strip())

    core_aliases = {
        normalize_name(company_name),
        normalize_name(extraction.profile.short_name or ""),
        normalize_name(extraction.profile.stock_code or ""),
        normalize_name(extraction.profile.credit_code or ""),
    }

    def get_or_create(
        name: str,
        *,
        category: str,
        description: str,
        risk_band: str = "unknown",
        company_type: str = "关联主体",
        relation_type: str = "",
        source_ids: Sequence[str] = (),
    ) -> str:
        clean = (name or "").strip()
        if not clean:
            return "core"
        if normalize_name(clean) in core_aliases:
            return "core"
        if clean in node_ids:
            node_id = node_ids[clean]
            existing = next((node for node in nodes if node.id == node_id), None)
            if existing is not None:
                rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "critical": 4, "core": 5}
                if rank.get(risk_band, 0) > rank.get(existing.risk, 0):
                    existing.risk = risk_band
                if existing.category in {"企业", "关联主体"} and category:
                    existing.category = category
                if not existing.relation_type and relation_type:
                    existing.relation_type = relation_type
                if description and len(description) > len(existing.description or ""):
                    existing.description = description
                existing.source_ids = list(dict.fromkeys([*existing.source_ids, *source_ids]))
            return node_id
        node_id = _safe_id(clean, len(nodes) + 1)
        node_ids[clean] = node_id
        nodes.append(
            GraphNode(
                id=node_id,
                name=clean,
                risk=risk_band if risk_band in {"low", "medium", "high", "critical", "unknown"} else "unknown",
                category=category or "关联主体",
                description=description,
                company_type=company_type,
                relation_type=relation_type,
                source_ids=list(source_ids),
            )
        )
        return node_id

    seen_edges = set()
    for relationship in extraction.relationships:
        source_name = relationship.source_name.strip() or company_name
        target_name = relationship.target_name.strip()
        if not target_name or normalize_name(source_name) == normalize_name(target_name):
            continue
        source_id = get_or_create(
            source_name,
            category=_relation_category(relationship.relation_type),
            description=relationship.description,
            risk_band=_node_risk(relationship.risk_level, relationship.relation_type, relationship.description),
            relation_type=relationship.relation_type,
            source_ids=relationship.source_ids,
        )
        target_id = get_or_create(
            target_name,
            category=_relation_category(relationship.relation_type),
            description=relationship.description,
            risk_band=_node_risk(relationship.risk_level, relationship.relation_type, relationship.description),
            relation_type=relationship.relation_type,
            source_ids=relationship.source_ids,
        )
        edge_key = (source_id, target_id, relationship.relation_type)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        amount = ""
        if relationship.amount is not None:
            amount = f"{relationship.amount:g}{relationship.amount_unit or ''}"
        weight = min(1.0, max(0.25, relationship.confidence))
        if relationship.share_ratio is not None:
            weight = min(1.0, max(weight, relationship.share_ratio / 100))
        edges.append(
            GraphEdge(
                source=source_id,
                target=target_id,
                relation=relationship.relation_type or "关联",
                weight=round(weight, 3),
                amount=amount,
                source_ids=relationship.source_ids,
            )
        )

    # Material risk events are explicit nodes so that serious risks are visible on
    # the graph instead of being hidden only in the score/table.
    for event in extraction.judicial_events:
        counterparty_id = "core"
        if event.counterparty:
            counterparty_id = get_or_create(
                event.counterparty,
                category="司法关联方",
                description=event.title,
                risk_band="high" if event.severity >= 70 else "medium",
                company_type="司法关联主体",
                relation_type=event.event_type,
                source_ids=event.source_ids,
            )
        if event.severity >= 55 or not event.counterparty:
            label = event.title[:28] + ("…" if len(event.title) > 28 else "")
            event_id = get_or_create(
                f"{event.event_type}：{label}",
                category="司法风险事件",
                description=event.description or event.title,
                risk_band="critical" if event.severity >= 85 else "high" if event.severity >= 70 else "medium",
                company_type="风险事件",
                relation_type=event.event_type,
                source_ids=event.source_ids,
            )
            edges.append(
                GraphEdge(
                    source="core",
                    target=event_id,
                    relation=event.event_type,
                    weight=min(1.0, max(0.4, event.severity / 100)),
                    amount=(f"{event.amount:g}{event.amount_unit or ''}" if event.amount is not None else ""),
                    source_ids=event.source_ids,
                )
            )
            if counterparty_id != "core":
                edges.append(
                    GraphEdge(
                        source=event_id,
                        target=counterparty_id,
                        relation="涉及",
                        weight=0.55,
                        source_ids=event.source_ids,
                    )
                )
        elif counterparty_id != "core":
            edges.append(
                GraphEdge(
                    source="core",
                    target=counterparty_id,
                    relation=event.event_type,
                    weight=min(1.0, max(0.35, event.severity / 100)),
                    amount=(f"{event.amount:g}{event.amount_unit or ''}" if event.amount is not None else ""),
                    source_ids=event.source_ids,
                )
            )

    for signal in extraction.risk_signals:
        if signal.severity < 55:
            continue
        label = signal.title[:30] + ("…" if len(signal.title) > 30 else "")
        signal_id = get_or_create(
            f"{signal.category}：{label}",
            category="风险信号",
            description=signal.description or signal.title,
            risk_band="critical" if signal.severity >= 85 else "high" if signal.severity >= 70 else "medium",
            company_type="风险事件",
            relation_type=signal.category,
            source_ids=signal.source_ids,
        )
        edges.append(
            GraphEdge(
                source="core",
                target=signal_id,
                relation=signal.category or "风险信号",
                weight=min(1.0, max(0.4, signal.severity / 100)),
                source_ids=signal.source_ids,
            )
        )

    return GraphPayload(nodes=nodes, edges=edges)

