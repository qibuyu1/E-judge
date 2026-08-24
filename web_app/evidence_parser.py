from __future__ import annotations

import re
from typing import Iterable, List, Sequence

from .query_utils import EntityQuery
from .schemas import CompanyProfile, FieldEvidence, IdentityResolution, Relationship, ResearchExtraction, RiskSignal, SearchSource

COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "集团有限公司", "有限公司")
COMPANY_NAME_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{2,46}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))")
CREDIT_RE = re.compile(r"(?<![0-9A-Z])([0-9A-HJ-NP-RTUWXY]{18})(?![0-9A-Z])")
STOCK_RE = re.compile(r"(?:证券代码|股票代码|代码)\s*[:：]?\s*(\d{6})")
SHORT_NAME_RE = re.compile(r"(?:证券简称|股票简称|公司简称)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9*ＳＴST-]{2,24})")

RELATION_PREFIXES = (
    "控股子公司", "全资子公司", "参股公司", "子公司", "孙公司", "供应商", "客户", "关联方",
    "旗下", "下属", "所属", "投资的", "拟收购", "宣布其", "公司称其", "担保对象",
)
BAD_NAME_TOKENS = ("公告", "关于", "宣布", "控股子公司", "全资子公司", "参股公司", "供应商", "客户", "项目")
BAD_NAME_PREFIXES = ("因在", "因", "根据", "截至", "其中", "关于", "经查", "涉及", "发行人", "上市公司", "本公司")

FIELD_PATTERNS = {
    "legal_person": [re.compile(r"(?:法定代表人|法人代表|法人)\s*[:：为是]?\s*([\u4e00-\u9fffA-Za-z·]{2,20})")],
    "registered_address": [re.compile(r"(?:注册地址|住所|公司住所)\s*[:：为]?\s*([^\n；。]{5,120})")],
    "registered_capital": [re.compile(r"(?:注册资本|注册资金)\s*[:：为]?\s*([0-9,.]+\s*(?:万|亿)?\s*(?:元|人民币|美元|港元)?)")],
    "company_status": [re.compile(r"(?:登记状态|经营状态|企业状态)\s*[:：为]?\s*([\u4e00-\u9fff]{2,16})")],
    "established_date": [re.compile(r"(?:成立日期|成立时间|注册日期)\s*[:：为]?\s*(\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2}日?)")],
    "controlling_shareholder": [re.compile(r"(?:控股股东|第一大股东)\s*[:：为]?\s*([^\n；。，]{2,80})")],
    "actual_controller": [re.compile(r"(?:实际控制人|最终控制人)\s*[:：为]?\s*([^\n；。，]{2,80})")],
    "industry": [re.compile(r"(?:所属行业|行业分类|行业)\s*[:：为]?\s*([^\n；。，]{2,50})")],
    "business_scope": [re.compile(r"(?:经营范围)\s*[:：]?\s*([^\n]{10,320})")],
}


def _text(source: SearchSource) -> str:
    return " ".join(filter(None, [source.title, source.snippet, source.text]))


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value or "").lower()


def _trim_candidate(value: str) -> str:
    candidate = value.strip(" ：:，,。；;()（）[]【】\t\n")
    # The old regex could capture prose such as “宣布其控股子公司河南…有限公司”.
    # Strip relation/prose prefixes repeatedly and reject any remaining cue words.
    changed = True
    while changed:
        changed = False
        for prefix in RELATION_PREFIXES:
            pos = candidate.rfind(prefix)
            if pos >= 0 and pos + len(prefix) < len(candidate):
                candidate = candidate[pos + len(prefix):].strip(" ：:，,。；;")
                changed = True
    for token in ("当事人", "发行人", "上市公司", "公司全称", "企业名称", "名称"):
        if candidate.startswith(token):
            candidate = candidate[len(token):].lstrip(" ：:")
    return candidate


def _valid_company_name(value: str) -> bool:
    if not value or not value.endswith(COMPANY_SUFFIXES):
        return False
    if not (4 <= len(value) <= 60):
        return False
    if any(token in value for token in BAD_NAME_TOKENS):
        return False
    if value.startswith(BAD_NAME_PREFIXES):
        return False
    core = value
    for suffix in COMPANY_SUFFIXES:
        if core.endswith(suffix):
            core = core[:-len(suffix)]
            break
    if len(core) < 2 or core in {"股份", "有限", "责任", "集团", "企业", "公司"}:
        return False
    return True


def _candidate_names(text: str) -> List[str]:
    names: list[str] = []
    for match in COMPANY_NAME_RE.finditer(text):
        value = _trim_candidate(match.group(1))
        if _valid_company_name(value) and value not in names:
            names.append(value)
    return names


def _structured_value(source: SearchSource, label: str) -> str:
    pattern = re.compile(rf"{re.escape(label)}\s*[:：]\s*([^；。\n]+)")
    match = pattern.search(_text(source))
    if not match:
        return ""
    value = match.group(1).strip()
    # A title/snippet/text concatenation may omit punctuation between fields. Stop at
    # the next known label and, for legal names, return only the first company suffix.
    value = re.split(
        r"\s+(?=(?:证券代码|股票代码|统一社会信用代码|公司全称|企业全称|证券简称|法定代表人|注册地址|注册资本)\s*[:：])",
        value,
        maxsplit=1,
    )[0].strip()
    if label in {"公司全称", "企业全称"}:
        company_match = COMPANY_NAME_RE.search(value)
        if company_match:
            value = _trim_candidate(company_match.group(1))
    return value


def _official_source(source: SearchSource) -> bool:
    return source.reliability >= 0.9 or source.source_type.startswith("structured_") or source.source_type.startswith("structured")


def _candidate_context_has_relation(text: str, candidate: str) -> bool:
    pos = text.find(candidate)
    if pos < 0:
        return False
    before = text[max(0, pos - 18):pos]
    return any(prefix in before for prefix in RELATION_PREFIXES)


def _issuer_legal_name(code: str, short_name: str, sources: Sequence[SearchSource]) -> tuple[str, list[str], float]:
    candidates: dict[str, tuple[float, list[str]]] = {}
    for source in sources:
        text = _text(source)
        compact = _compact(text)
        code_hit = code in compact
        alias_hit = bool(short_name and _compact(short_name) in compact)
        if not code_hit and not alias_hit:
            continue
        # Structured profile sources explicitly label the issuer name.
        explicit = _structured_value(source, "公司全称") or _structured_value(source, "企业全称")
        source_names = [explicit] if _valid_company_name(explicit) else _candidate_names(text)
        for name in source_names:
            if _candidate_context_has_relation(text, name):
                continue
            score = source.reliability * 0.6 + source.search_score * 0.2
            if source.source_type == "structured_listed_company_profile":
                score += 0.8
            if _official_source(source):
                score += 0.32
            if code_hit:
                score += 0.38
            if alias_hit:
                score += 0.18
            if name in source.title:
                score += 0.22
            previous = candidates.get(name)
            ids = list(dict.fromkeys([*(previous[1] if previous else []), source.id]))
            candidates[name] = (max(score, previous[0] if previous else 0), ids)
    if not candidates:
        return "", [], 0.0
    name, (score, ids) = max(candidates.items(), key=lambda item: (item[1][0], -len(item[0])))
    return name, ids[:5], min(0.99, max(0.82, score / 1.75))



def _legal_name_like(value: str) -> bool:
    return bool(value and value.endswith(COMPANY_SUFFIXES) and 4 <= len(value) <= 60)


def _source_mentions_target(source: SearchSource, entity: EntityQuery, aliases: Sequence[str] = ()) -> bool:
    text = _compact(_text(source))
    if not text:
        return False
    if entity.query_type in {"stock_code", "credit_code"} and _compact(entity.normalized) in text:
        return True
    names = [entity.normalized, entity.company_core, *aliases]
    for name in names:
        compact = _compact(name)
        if len(compact) >= 4 and compact in text:
            return True
    return False


def deterministic_identity(entity: EntityQuery, sources: Sequence[SearchSource], name_hints: Sequence[str] = ()) -> IdentityResolution:
    """Resolve one immutable target entity from evidence.

    Critical rule: for a full company-name query, the user's legal name is the identity
    anchor.  Search results may add codes/aliases, but an article mentioning a subsidiary
    or another company is never allowed to replace the queried company.  This fixes the
    former bug where *every* company-name source was treated as an exact identity hit.
    """
    normalized = entity.normalized
    if entity.query_type == "stock_code":
        structured = [s for s in sources if s.source_type in {"structured_stock_master", "structured_listed_company_profile"}]
        if structured:
            best = max(structured, key=lambda s: (s.source_type == "structured_listed_company_profile", s.reliability, len(s.text)))
            short_name = _structured_value(best, "证券简称") or next((h for h in name_hints if h and not h.endswith(COMPANY_SUFFIXES)), "")
            explicit_name = _structured_value(best, "公司全称") or _structured_value(best, "企业全称")
            legal_name, legal_ids, legal_conf = _issuer_legal_name(normalized, short_name, sources)
            company_name = explicit_name if _valid_company_name(explicit_name) else legal_name or short_name
            ids = list(dict.fromkeys([best.id, *legal_ids]))
            aliases = [item for item in [*name_hints, short_name] if item and item != company_name]
            return IdentityResolution(
                company_name=company_name or None,
                short_name=short_name or (company_name if company_name and not company_name.endswith(COMPANY_SUFFIXES) else None),
                stock_code=normalized,
                aliases=list(dict.fromkeys(aliases))[:8],
                source_ids=ids,
                confidence=max(best.reliability, legal_conf, 0.88),
                verified=bool(company_name),
                reason="证券结构化主数据已确认代码对应主体；法定全称仅从发行人/结构化来源升级",
            )

    # Full legal-name input is immutable.  We only need evidence that the same name is
    # present in at least one public source; other extracted names are relationships.
    if entity.query_type == "company_name" and _legal_name_like(normalized):
        exact_ids = [s.id for s in sources if _compact(normalized) in _compact(_text(s))]
        core_ids = [s.id for s in sources if len(_compact(entity.company_core)) >= 4 and _compact(entity.company_core) in _compact(_text(s))]
        ids = list(dict.fromkeys([*exact_ids, *core_ids]))[:6]
        return IdentityResolution(
            company_name=normalized,
            aliases=list(dict.fromkeys([h for h in name_hints if h and h != normalized]))[:6],
            source_ids=ids,
            confidence=0.98 if exact_ids else (0.86 if core_ids else 0.0),
            verified=bool(ids),
            reason="用户输入的法定企业全称作为不可变主体锚点；公开来源仅用于核验和补充字段",
        )

    candidates: dict[str, tuple[float, list[str]]] = {}
    matched_ids: list[str] = []
    ident = _compact(normalized)
    query_core = _compact(entity.company_core)
    hints = [h for h in name_hints if len(_compact(h)) >= 2]
    for source in sources:
        text = _text(source)
        compact = _compact(text)
        if entity.query_type == "company_name":
            exact_hit = bool(ident and ident in compact)
            core_hit = bool(len(query_core) >= 4 and query_core in compact)
            hint_hit = any(_compact(hint) in compact for hint in hints)
            if not (exact_hit or core_hit or hint_hit):
                continue
        else:
            exact_hit = bool(ident and ident in compact)
            hint_hit = any(_compact(hint) in compact for hint in hints)
            if not exact_hit and not hint_hit:
                continue
        matched_ids.append(source.id)
        for name in _candidate_names(text):
            if _candidate_context_has_relation(text, name):
                continue
            name_compact = _compact(name)
            # A candidate for a name query must share the queried entity core.  This
            # blocks arbitrary company names from the same article/search result.
            if entity.query_type == "company_name":
                if not (ident in name_compact or name_compact in ident or (len(query_core) >= 4 and query_core in name_compact)):
                    continue
            score = source.reliability * 0.55 + source.search_score * 0.25
            if exact_hit:
                score += 0.55
            if hint_hit:
                score += 0.20
            if name in source.title:
                score += 0.25
            if entity.query_type == "company_name":
                if ident == name_compact:
                    score += 1.0
                elif ident in name_compact or name_compact in ident:
                    score += 0.65
                elif len(query_core) >= 4 and query_core in name_compact:
                    score += 0.42
            previous = candidates.get(name)
            ids = list(dict.fromkeys([*(previous[1] if previous else []), source.id]))
            candidates[name] = (max(score, previous[0] if previous else 0), ids)

    if candidates:
        name, (score, ids) = max(candidates.items(), key=lambda item: (item[1][0], len(item[1][1]), -len(item[0])))
        credit = entity.normalized if entity.query_type == "credit_code" else None
        stock = entity.normalized if entity.query_type == "stock_code" else None
        return IdentityResolution(
            company_name=name,
            short_name=next((h for h in name_hints if h and h != name and not h.endswith(COMPANY_SUFFIXES)), None),
            credit_code=credit,
            stock_code=stock,
            aliases=list(dict.fromkeys(name_hints))[:8],
            source_ids=ids[:6],
            confidence=min(0.99, max(0.78, score / 1.8)),
            verified=bool(ids),
            reason="查询标识与企业名称在同一公开证据中匹配",
        )
    return IdentityResolution(source_ids=list(dict.fromkeys(matched_ids))[:4], verified=False, reason="未能从公开证据中稳定提取企业主体")



def _clean_field_value(field: str, value: str) -> str:
    text = value.strip(" ：:，,。；;\t")
    text = re.sub(r"^(?:变更为|现为|为|系|指|由|即)\s*", "", text)
    text = re.split(
        r"\s*(?=(?:统一社会信用代码|证券代码|股票代码|法定代表人|注册地址|注册资本|成立日期|成立时间|经营状态|登记状态|所属行业|经营范围|控股股东|实际控制人|主要产品)\s*[:：])",
        text,
        maxsplit=1,
    )[0].strip(" ：:，,。；;")
    if text in {"未获取", "暂无", "不详", "点击查看", "查询", "相关信息"}:
        return ""
    if field in {"controlling_shareholder", "actual_controller"}:
        company_names = _candidate_names(text)
        if company_names:
            return company_names[0]
        text = re.split(r"(?:提供担保|及其关联|及关联|的持股|持有|控制|，|；|。)", text, maxsplit=1)[0].strip()
        if text in {"实际", "控制", "股东", "无", "不存在", "未知"}:
            return ""
        if any(token in text for token in ("证明", "公告", "报告", "情况", "原因", "无犯罪", "责任", "风险", "核验", "提供")):
            return ""
        if re.fullmatch(r"[\u4e00-\u9fff·]{2,8}", text):
            return text
        if 2 <= len(text) <= 32 and any(token in text for token in ("集团", "公司", "委员会", "国资", "投资", "控股")):
            return text
        return ""
    if field == "industry":
        text = re.split(r"[；。，]", text, maxsplit=1)[0].strip()
        if len(text) > 28 or any(token in text for token in ("情况", "原因", "分析", "变化", "经营等", "业务经营", "说明")):
            return ""
    if field == "company_status":
        text = re.split(r"[；。， ]", text, maxsplit=1)[0].strip()
        if text not in {"存续", "在业", "开业", "正常", "注销", "吊销", "迁出", "撤销", "停业", "清算", "歇业", "仍注册"} and len(text) > 8:
            return ""
    if field == "registered_address":
        text = re.split(r"(?:法定代表人|注册资本|成立日期|经营范围|经营状态|统一社会信用代码)\s*[:：]", text, maxsplit=1)[0].strip(" ，,；;")
        if len(text) < 5 or len(text) > 120:
            return ""
    if field == "legal_person":
        text = re.split(r"[；。，, ]", text, maxsplit=1)[0].strip()
        if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]{2,20}", text):
            return ""
    return text




def select_profile_sources(
    entity: EntityQuery,
    identity: IdentityResolution,
    sources: Sequence[SearchSource],
) -> list[SearchSource]:
    """Keep only sources that are safe for scalar company-profile fields.

    Relation/risk pages may contain dozens of other legal entities with their own
    addresses, legal representatives and capital.  Those pages are useful for the
    graph, but must not be allowed to populate the queried company's profile.
    """
    company = _compact(identity.company_name or "")
    short = _compact(identity.short_name or "")
    code = _compact(identity.credit_code or entity.normalized if entity.query_type == "credit_code" else identity.credit_code or "")
    stock = _compact(identity.stock_code or entity.normalized if entity.query_type == "stock_code" else identity.stock_code or "")
    result: list[SearchSource] = []
    for source in sources:
        if source.source_type.startswith("structured"):
            if source.source_type in {"structured_stock_master", "structured_listed_company_profile"}:
                result.append(source)
            continue
        text_compact = _compact(_text(source))
        title_compact = _compact(source.title)
        exact_company = bool(company and company in text_compact)
        exact_code = bool(code and code in text_compact)
        exact_stock = bool(stock and stock in text_compact)
        short_in_title = bool(short and len(short) >= 2 and short in title_compact)
        profile_purpose = any(token in (source.search_purpose or "") for token in ("主体", "工商", "档案", "年报", "经营披露", "证券"))
        if exact_company or exact_code or exact_stock or (short_in_title and profile_purpose):
            result.append(source)
    result.sort(key=lambda s: (s.source_type.startswith("structured"), s.reliability, s.search_score, len(s.text)), reverse=True)
    return result[:24]


ADVERSE_SIGNAL_PATTERNS: tuple[tuple[str, str, int, re.Pattern[str]], ...] = (
    ("司法执行", "被列为失信被执行人", 98, re.compile(r"(?:列入|纳入|被列为|成为)?失信被执行人")),
    ("司法执行", "被执行风险", 88, re.compile(r"(?:被执行人|强制执行|执行标的)")),
    ("司法执行", "限制高消费", 92, re.compile(r"(?:限制高消费|限高令|限制消费令)")),
    ("经营偿债", "大额逾期债务", 98, re.compile(r"(?:逾期的?有息(?:总)?负债|债务逾期|逾期债务|贷款逾期|未能按期偿付)")),
    ("经营偿债", "债券或融资违约", 96, re.compile(r"(?:债券违约|债务违约|本息违约|未按期兑付|未能兑付|展期后仍未兑付)")),
    ("经营偿债", "破产或重整风险", 99, re.compile(r"(?:破产重整|申请破产|破产清算|预重整|重整申请)")),
    ("经营偿债", "退市或终止上市", 88, re.compile(r"(?:终止上市|被实施退市|退市整理|摘牌|股票终止上市)")),
    ("信息披露", "无法表示意见审计", 99, re.compile(r"(?:无法表示意见|否定意见)")),
    ("信息披露", "非标准审计意见", 82, re.compile(r"(?:保留意见|非标准审计意见|非标审计)")),
    ("关联交易", "资金占用", 96, re.compile(r"(?:非经营性资金占用|资金占用|违规占用)")),
    ("关联交易", "违规或高风险担保", 90, re.compile(r"(?:违规担保|担保逾期|担保代偿|对外担保.*逾期)")),
    ("司法监管", "重大诉讼仲裁", 84, re.compile(r"(?:重大诉讼|累计诉讼|重大仲裁|累计仲裁)")),
    ("司法监管", "行政处罚或监管措施", 76, re.compile(r"(?:行政处罚|监管措施|纪律处分|立案调查|证监会立案)")),
    ("股权控制", "股权冻结", 80, re.compile(r"(?:股权冻结|股份冻结|司法冻结)")),
)


def _identity_aliases(entity: EntityQuery, identity: IdentityResolution) -> list[str]:
    values = [identity.company_name, identity.short_name, identity.credit_code, identity.stock_code]
    if entity.query_type == "company_name":
        values.extend([entity.normalized, entity.company_core])
    else:
        values.append(entity.normalized)
    return [value for value in dict.fromkeys(values) if value]


def _source_is_about_target(source: SearchSource, entity: EntityQuery, identity: IdentityResolution) -> bool:
    combined = _compact(_text(source))
    title = _compact(source.title)
    aliases = _identity_aliases(entity, identity)
    for alias in aliases:
        token = _compact(alias)
        if not token:
            continue
        if len(token) >= 4 and token in combined:
            return True
        if len(token) >= 2 and token in title:
            return True
    return source.source_type.startswith("structured")


def _risk_excerpt(text: str, match: re.Match[str], limit: int = 180) -> str:
    start = max(0, match.start() - 55)
    end = min(len(text), match.end() + 105)
    return re.sub(r"\s+", " ", text[start:end]).strip(" ；。")[:limit]


def _risk_match_is_negated(text: str, match: re.Match[str]) -> bool:
    window = text[max(0, match.start() - 18):min(len(text), match.end() + 18)]
    before = text[max(0, match.start() - 14):match.start()]
    negations = ("不存在", "未发生", "未出现", "未被列入", "未被纳入", "不涉及", "无相关", "无重大", "已解除", "已撤销", "已结清", "已清偿")
    if any(token in window for token in negations):
        return True
    if before.endswith("无") or before.endswith("没有"):
        return True
    return False


def explicit_risk_signals(
    entity: EntityQuery,
    identity: IdentityResolution,
    sources: Sequence[SearchSource],
) -> list[RiskSignal]:
    """Capture explicit adverse phrases with corroboration-aware severity.

    A single search snippet may be stale, truncated or about a related entity. It is
    retained for review but cannot by itself create a high-risk conclusion unless it
    contains concrete identifiers such as amount, date or case/announcement number.
    """
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for source in sources:
        if not _source_is_about_target(source, entity, identity):
            continue
        raw = _text(source)
        for category, title, severity, pattern in ADVERSE_SIGNAL_PATTERNS:
            match = pattern.search(raw)
            if not match or _risk_match_is_negated(raw, match):
                continue
            description = _risk_excerpt(raw, match)
            key = (category, title)
            bucket = grouped.setdefault(key, {"severity": severity, "descriptions": [], "sources": [], "reliability": []})
            bucket["descriptions"].append(description)
            bucket["sources"].append(source.id)
            bucket["reliability"].append(source.reliability)

    result: list[RiskSignal] = []
    for (category, title), bucket in grouped.items():
        source_ids = list(dict.fromkeys(bucket["sources"]))
        descriptions = list(dict.fromkeys(bucket["descriptions"]))
        joined = "；".join(descriptions[:3])
        concrete = bool(re.search(
            r"(?:20\d{2}[-/.年]\d{1,2}|\d+(?:\.\d+)?\s*(?:亿元|万元|元)|(?:案号|执行案号|公告编号|处罚决定书)[:：]?\s*[^；。\s]{4,})",
            joined,
        ))
        corroborated = len(source_ids) >= 2
        reliability = max(bucket["reliability"] or [0.72])
        official_filing = reliability >= 0.90 and "公告" in joined
        raw_severity = int(bucket["severity"])
        adjusted_severity = raw_severity if corroborated or concrete or official_filing else min(raw_severity, 58)
        confidence = min(0.99, max(0.68, reliability if corroborated or concrete or official_filing else reliability * 0.78))
        result.append(RiskSignal(
            category=category, title=title, description=joined, severity=adjusted_severity,
            source_ids=source_ids, confidence=confidence,
        ))
    result.sort(key=lambda item: (item.severity, item.confidence), reverse=True)
    return result[:24]


def _best_field(
    field: str,
    sources: Sequence[SearchSource],
    company_name: str = "",
) -> tuple[str | None, FieldEvidence | None]:
    candidates: list[tuple[float, str, str]] = []
    target = _compact(company_name)
    target_core = target
    for suffix in COMPANY_SUFFIXES:
        suffix_compact = _compact(suffix)
        if target_core.endswith(suffix_compact):
            target_core = target_core[:-len(suffix_compact)]
            break
    for source in sources:
        text = _text(source)
        source_title = _compact(source.title)
        source_profile_context = source.source_type in {"structured_stock_master", "structured_listed_company_profile"} or (
            any(token in (source.search_purpose or "") for token in ("主体", "工商", "档案", "年报", "经营披露", "证券"))
            and bool(target and (target in source_title or (len(target_core) >= 4 and target_core in source_title)))
        )
        for pattern in FIELD_PATTERNS.get(field, []):
            for match in pattern.finditer(text):
                value = _clean_field_value(field, match.group(1))
                if not value:
                    continue
                before = text[max(0, match.start() - 160):match.start()]
                nearby_names = _candidate_names(before)
                if target and nearby_names:
                    nearest = _compact(nearby_names[-1])
                    same_target = target in nearest or nearest in target or (len(target_core) >= 4 and target_core in nearest)
                    if not same_target:
                        continue
                elif target and not source_profile_context:
                    # On a relation/risk article, an unanchored scalar label is too
                    # ambiguous to populate the queried company's legal profile.
                    continue
                score = source.reliability * 0.7 + source.search_score * 0.3 + (0.16 if source_profile_context else 0)
                if target and (target in _compact(before) or target in source_title):
                    score += 0.14
                candidates.append((score, value, source.id))
    if not candidates:
        return None, None
    candidates.sort(key=lambda row: (row[0], -len(row[1])), reverse=True)
    score, value, sid = candidates[0]
    return value, FieldEvidence(source_ids=[sid], confidence=min(0.98, max(0.58, score)))




def _explicit_relationships(company_name: str, sources: Sequence[SearchSource]) -> list[Relationship]:
    """Extract only explicitly worded relationships from public evidence.

    This deterministic pass complements the model and preserves coverage when a JSON
    lane needs repair. It intentionally ignores vague co-occurrence.
    """
    if not company_name:
        return []
    results: list[Relationship] = []
    company_pattern = r"([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,60}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))"
    label_patterns = (
        ("控股股东", "incoming", re.compile(rf"(?:控股股东|第一大股东)\s*[:：为]?\s*{company_pattern}")),
        ("实际控制", "incoming", re.compile(rf"(?:实际控制人|最终控制人)\s*[:：为]?\s*{company_pattern}")),
        ("全资子公司", "outgoing", re.compile(rf"(?:全资子公司)\s*[:：包括有]?\s*{company_pattern}")),
        ("控股子公司", "outgoing", re.compile(rf"(?:控股子公司)\s*[:：包括有]?\s*{company_pattern}")),
        ("参股公司", "outgoing", re.compile(rf"(?:参股公司|参股企业)\s*[:：包括有]?\s*{company_pattern}")),
        ("对外投资", "outgoing", re.compile(rf"(?:对外投资企业|被投资企业|投资标的)\s*[:：包括有]?\s*{company_pattern}")),
        ("担保对象", "outgoing", re.compile(rf"(?:担保对象|被担保人|被担保方)\s*[:：为]?\s*{company_pattern}")),
        ("关联方", "outgoing", re.compile(rf"(?:关联方|关联企业)\s*[:：包括有]?\s*{company_pattern}")),
    )
    for source in sources:
        text = _text(source)
        for relation_type, direction, pattern in label_patterns:
            for match in pattern.finditer(text):
                target = _trim_candidate(match.group(1))
                if not _valid_company_name(target) or _compact(target) == _compact(company_name):
                    continue
                if direction == "incoming":
                    source_name, target_name = target, company_name
                else:
                    source_name, target_name = company_name, target
                results.append(Relationship(
                    source_name=source_name,
                    target_name=target_name,
                    relation_type=relation_type,
                    description=f"公开来源明确披露的{relation_type}关系",
                    source_ids=[source.id],
                    confidence=min(0.95, max(0.68, source.reliability)),
                    risk_level="medium" if "担保" in relation_type else "low",
                ))

        # “公司持有X 60%股权” and “X持有公司60%股份” forms.
        for match in re.finditer(rf"(?:本公司|公司|{re.escape(company_name)})\s*(?:直接|间接)?持有\s*{company_pattern}\s*([0-9]+(?:\.[0-9]+)?)?%?\s*(?:股权|股份)", text):
            target = _trim_candidate(match.group(1))
            ratio = float(match.group(2)) if match.group(2) else None
            if _valid_company_name(target) and _compact(target) != _compact(company_name):
                results.append(Relationship(
                    source_name=company_name, target_name=target,
                    relation_type="控股子公司" if ratio is not None and ratio >= 50 else "对外投资",
                    share_ratio=ratio, description="公开来源披露的持股关系",
                    source_ids=[source.id], confidence=min(0.96, max(0.7, source.reliability)), risk_level="low",
                ))
        for match in re.finditer(rf"{company_pattern}\s*(?:直接|间接)?持有\s*(?:本公司|公司|{re.escape(company_name)})\s*([0-9]+(?:\.[0-9]+)?)?%?\s*(?:股权|股份)", text):
            holder = _trim_candidate(match.group(1))
            ratio = float(match.group(2)) if match.group(2) else None
            if _valid_company_name(holder) and _compact(holder) != _compact(company_name):
                results.append(Relationship(
                    source_name=holder, target_name=company_name,
                    relation_type="控股股东" if ratio is not None and ratio >= 50 else "股东",
                    share_ratio=ratio, description="公开来源披露的持股关系",
                    source_ids=[source.id], confidence=min(0.96, max(0.7, source.reliability)), risk_level="low",
                ))
    dedup: dict[tuple[str, str, str], Relationship] = {}
    for item in results:
        key = (_compact(item.source_name), _compact(item.target_name), _compact(item.relation_type))
        current = dedup.get(key)
        if current is None or item.confidence > current.confidence:
            dedup[key] = item
    return list(dedup.values())[:40]


def parse_local_extraction(
    entity: EntityQuery,
    identity: IdentityResolution,
    sources: Sequence[SearchSource],
    relation_sources: Sequence[SearchSource] | None = None,
) -> ResearchExtraction:
    profile = CompanyProfile(
        company_name=identity.company_name,
        short_name=identity.short_name,
        credit_code=identity.credit_code or (entity.normalized if entity.query_type == "credit_code" else None),
        stock_code=identity.stock_code or (entity.normalized if entity.query_type == "stock_code" else None),
    )
    all_sources = list(relation_sources or sources)
    identity_evidence = FieldEvidence(source_ids=identity.source_ids, confidence=identity.confidence)
    for field in ("company_name", "short_name", "credit_code", "stock_code"):
        if getattr(profile, field) and identity.source_ids:
            profile.field_evidence[field] = identity_evidence
    for field in FIELD_PATTERNS:
        value, evidence = _best_field(field, sources, profile.company_name or identity.company_name or "")
        if value:
            setattr(profile, field, value)
            if evidence:
                profile.field_evidence[field] = evidence

    products: list[str] = []
    for source in sources:
        match = re.search(r"(?:主要产品|主营产品|主营业务)\s*[:：]\s*([^；。\n]{2,180})", _text(source))
        if match:
            raw_products = match.group(1).strip()
            if len(raw_products) <= 180 and not re.match(r"^(?:打造|致力于|聚焦|以.+为核心)", raw_products):
                products.extend(
                    item.strip() for item in re.split(r"[、,，；;]", raw_products)
                    if 1 < len(item.strip()) <= 50
                )
    profile.main_products = list(dict.fromkeys(products))[:12]

    relationships: list[Relationship] = []
    if profile.company_name and profile.controlling_shareholder:
        ev = profile.field_evidence.get("controlling_shareholder", FieldEvidence())
        relationships.append(Relationship(source_name=profile.controlling_shareholder, target_name=profile.company_name, relation_type="控股股东", description="公开来源披露的控股股东关系", source_ids=ev.source_ids, confidence=ev.confidence, risk_level="low"))
    if profile.company_name and profile.actual_controller:
        ev = profile.field_evidence.get("actual_controller", FieldEvidence())
        relationships.append(Relationship(source_name=profile.actual_controller, target_name=profile.company_name, relation_type="实际控制", description="公开来源披露的实际控制关系", source_ids=ev.source_ids, confidence=ev.confidence, risk_level="low"))
    relationships.extend(_explicit_relationships(profile.company_name or identity.company_name or "", all_sources))
    risk_signals = explicit_risk_signals(entity, identity, all_sources)

    critical = {
        "统一社会信用代码": profile.credit_code, "法定代表人": profile.legal_person,
        "注册地址": profile.registered_address, "注册资本": profile.registered_capital,
        "企业状态": profile.company_status, "成立日期": profile.established_date,
        "控股股东": profile.controlling_shareholder, "实际控制人": profile.actual_controller,
        "所属行业": profile.industry, "主要产品": profile.main_products,
    }
    return ResearchExtraction(
        profile=profile, relationships=relationships, risk_signals=risk_signals, missing_fields=[k for k, v in critical.items() if not v],
        overall_confidence=max(0.5, identity.confidence * 0.9), identity_verified=identity.verified,
    )


def merge_extractions(primary: ResearchExtraction, secondary: ResearchExtraction) -> ResearchExtraction:
    profile = primary.profile.model_copy(deep=True)
    fields = [
        "company_name", "short_name", "credit_code", "stock_code", "legal_person", "registered_address",
        "registered_capital", "company_status", "established_date", "industry", "business_scope",
        "controlling_shareholder", "actual_controller", "website",
    ]
    for field in fields:
        pv, sv = getattr(profile, field), getattr(secondary.profile, field)
        pe, se = profile.field_evidence.get(field), secondary.profile.field_evidence.get(field)
        pc, sc = (pe.confidence if pe else 0), (se.confidence if se else 0)
        primary_is_structured_profile = bool(
            pe and any(str(source_id).startswith("PROFILE-") for source_id in pe.source_ids)
        )
        primary_is_target_scoped = bool(pe and pe.source_ids and pe.confidence >= 0.70)
        protected_scalar_fields = {
            "legal_person", "registered_address", "registered_capital", "company_status",
            "established_date", "industry", "controlling_shareholder", "actual_controller",
        }
        if field in {"company_name", "stock_code", "credit_code"} and pv:
            # Identity is immutable after deterministic verification. This blocks a
            # subsidiary mentioned in an announcement from replacing the issuer.
            continue
        if (primary_is_structured_profile or (field in protected_scalar_fields and primary_is_target_scoped)) and pv:
            # Target-scoped deterministic/structured evidence is stronger than a
            # free-form model guess from a page containing several companies.
            continue
        if sv and (not pv or sc > pc):
            setattr(profile, field, sv)
            if se:
                profile.field_evidence[field] = se
    profile.main_products = list(dict.fromkeys([*primary.profile.main_products, *secondary.profile.main_products]))[:16]
    for name, evidence in secondary.profile.field_evidence.items():
        if name in {"company_name", "stock_code"} and profile.field_evidence.get(name):
            continue
        current = profile.field_evidence.get(name)
        if current is None or evidence.confidence > current.confidence:
            profile.field_evidence[name] = evidence

    def dedup(items: Iterable, key_fn):
        result = {}
        for item in items:
            key = key_fn(item)
            previous = result.get(key)
            conf = getattr(item, "confidence", getattr(item, "severity", 0) / 100)
            prev_conf = getattr(previous, "confidence", getattr(previous, "severity", 0) / 100) if previous else -1
            if previous is None or conf > prev_conf:
                result[key] = item
        return list(result.values())

    relationships = dedup([*primary.relationships, *secondary.relationships], lambda r: (_compact(r.source_name), _compact(r.target_name), _compact(r.relation_type)))
    judicial = dedup([*primary.judicial_events, *secondary.judicial_events], lambda e: (e.case_number or "", _compact(e.title), e.event_date or "", _compact(e.counterparty or "")))
    annual = dedup([*primary.annual_facts, *secondary.annual_facts], lambda f: (f.year, _compact(f.metric), _compact(f.value)))
    signals = dedup([*primary.risk_signals, *secondary.risk_signals], lambda s: (_compact(s.category), _compact(s.title), s.event_date or ""))
    resolved = {
        "统一社会信用代码": profile.credit_code, "法定代表人": profile.legal_person,
        "注册地址": profile.registered_address, "注册资本": profile.registered_capital,
        "企业状态": profile.company_status, "成立日期": profile.established_date,
        "控股股东": profile.controlling_shareholder, "实际控制人": profile.actual_controller,
        "所属行业": profile.industry, "主要产品": profile.main_products,
    }
    missing = [x for x in dict.fromkeys([*primary.missing_fields, *secondary.missing_fields]) if not resolved.get(x)]
    return ResearchExtraction(
        profile=profile, relationships=relationships, judicial_events=judicial, annual_facts=annual,
        risk_signals=signals, missing_fields=missing,
        conflicts=list(dict.fromkeys([*primary.conflicts, *secondary.conflicts])),
        overall_confidence=max(primary.overall_confidence, secondary.overall_confidence),
        identity_verified=primary.identity_verified or secondary.identity_verified,
    )
