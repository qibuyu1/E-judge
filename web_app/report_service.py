from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import uuid4

from .cache import research_cache
from .config import settings
from .deepseek_client import DeepSeekConfigurationError, deepseek_client
from .evidence_parser import deterministic_identity, merge_extractions, parse_local_extraction, select_profile_sources
from .query_utils import EntityQuery, display_model_name, parse_entity_query
from .risk_engine import build_graph, calculate_risk
from .listed_profile_resolver import ListedCompanyProfile, resolve_listed_profile
from .profile_quality import clean_profile_value, sanitize_profile_object
from .listed_relations_resolver import ListedRelationsBundle, resolve_listed_relations
from .schemas import (
    CompanyProfile,
    DecisionAnalysis,
    FieldEvidence,
    IdentityResolution,
    NarrativeItem,
    NarrativeResult,
    ReportPayload,
    ResearchExtraction,
    SearchSource,
)
from .search_service import (
    SearchRunResult,
    TavilyConfigurationError,
    TavilySearchError,
    filter_relevant_sources,
    merge_sources,
    tavily_search_client,
)
from .stock_resolver import StockHint, resolve_stock_hint

logger = logging.getLogger(__name__)
ProgressCallback = Optional[Callable[[int, str, str], Any]]


class ResearchUnavailableError(RuntimeError):
    def __init__(self, message: str, *, code: str = "RESEARCH_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


_FIELD_LABELS = {
    "company_name": "企业全称", "short_name": "企业简称", "credit_code": "统一社会信用代码",
    "stock_code": "股票代码", "legal_person": "法定代表人", "registered_address": "注册地址",
    "registered_capital": "注册资本", "company_status": "企业状态", "established_date": "成立日期",
    "industry": "所属行业", "business_scope": "经营范围", "controlling_shareholder": "控股股东",
    "actual_controller": "实际控制人", "main_products": "主要产品", "website": "官方网站",
    "企业身份核验": "企业身份核验",
}


def _humanize_missing_fields(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        label = _FIELD_LABELS.get((value or "").strip(), (value or "").strip())
        if label and label not in result:
            result.append(label)
    return result


def _severity_band(value: int) -> str:
    return "high" if value >= 70 else "medium" if value >= 45 else "low"


def _impact_and_action(category: str, title: str) -> tuple[str, str]:
    text = f"{category} {title}"
    if any(word in text for word in ("诉讼", "执行", "失信", "处罚", "司法")):
        return ("可能影响偿债能力、资产可执行性及后续授信条件。", "核对案件最新状态、涉案金额、责任主体和资产保全情况，并测算对现金流的实际影响。")
    if any(word in text for word in ("股权", "控制", "质押", "冻结", "股东")):
        return ("可能削弱控制权稳定性，并放大股权结构变化带来的治理风险。", "穿透核验控制链、持股比例、质押冻结状态及近期股权变更，确认实际控制权是否稳定。")
    if any(word in text for word in ("关联", "担保", "资金占用", "往来", "交易")):
        return ("可能形成风险传导、利益输送、回款依赖或新增或有负债。", "抽查重大关联交易和担保合同，核验定价、审批、资金流向、回款闭环及反担保安排。")
    if any(word in text for word in ("审计", "披露", "年报", "财务")):
        return ("可能降低公开财务信息的可靠性，影响风险判断基础。", "回看原始年报和审计意见，补充核验关键财务科目、会计口径及异常变动原因。")
    return ("该事项可能影响企业经营稳定性或授信风险判断。", "由客户经理结合原始证据和企业补充材料进行人工复核，并记录核验结论。")


def _build_decision_cards(extraction: ResearchExtraction, risk: Any) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    ranked_signals = sorted(extraction.risk_signals, key=lambda item: (item.severity, item.confidence), reverse=True)
    for signal in ranked_signals[:4]:
        impact, action = _impact_and_action(signal.category, signal.title)
        cards.append({
            "severity": _severity_band(signal.severity),
            "label": signal.category or "风险信号",
            "title": signal.title or "需关注的公开风险信号",
            "summary": signal.description or "公开来源记录了该风险事项。",
            "impact": impact,
            "action": action,
            "source_ids": signal.source_ids,
        })
    for event in sorted(extraction.judicial_events, key=lambda item: item.severity, reverse=True):
        if len(cards) >= 5:
            break
        title = event.title or event.event_type
        if any(card["title"] == title for card in cards):
            continue
        impact, action = _impact_and_action(event.event_type, title)
        amount = f"；涉及金额约{event.amount:g}{event.amount_unit or ''}" if event.amount is not None else ""
        cards.append({
            "severity": _severity_band(event.severity),
            "label": event.event_type or "司法事项",
            "title": title,
            "summary": (event.description or title) + amount,
            "impact": impact,
            "action": action,
            "source_ids": event.source_ids,
        })
    if not cards:
        for dimension in sorted(risk.dimensions, key=lambda item: item.contribution, reverse=True):
            if dimension.contribution <= 0:
                continue
            impact, action = _impact_and_action(dimension.name, dimension.evidence)
            cards.append({
                "severity": "medium" if dimension.contribution >= 5 else "low",
                "label": dimension.name,
                "title": f"{dimension.name}贡献{dimension.contribution:.1f}分",
                "summary": dimension.evidence or "该维度存在需要人工复核的公开信号。",
                "impact": impact,
                "action": action,
                "source_ids": dimension.source_ids,
            })
            if len(cards) >= 3:
                break
    missing = _humanize_missing_fields(extraction.missing_fields)
    if missing:
        cards.append({
            "severity": "medium",
            "label": "数据缺口",
            "title": "仍有关键字段未被公开来源覆盖",
            "summary": "尚未获取：" + "、".join(missing[:8]) + "。未检索到不代表该事项不存在。",
            "impact": "信息不完整会降低自动评分的确定性，不能直接据此作出授信结论。",
            "action": "向企业补充索取工商档案、最新审计报告、股权结构、授信和担保材料，并与法定披露交叉核验。",
            "source_ids": [],
        })
    if not cards:
        cards.append({
            "severity": "low", "label": "初筛结论", "title": "当前公开证据未确认重大异常",
            "summary": "本次检索未发现可被公开来源确认的重大风险事项。",
            "impact": "该结论仅代表公开信息初筛结果，不覆盖未公开交易与银行内部数据。",
            "action": "按正常授信流程继续核验财务、流水、担保和关联方材料。", "source_ids": [],
        })
    return cards[:6]


async def _notify(callback: ProgressCallback, progress: int, stage: str, message: str) -> None:
    if callback is None:
        return
    result = callback(progress, stage, message)
    if inspect.isawaitable(result):
        await result


def _source_ids_to_suffix(source_ids: Sequence[str]) -> str:
    values = [sid for sid in source_ids if sid]
    return f"（证据：{'、'.join(values)}）" if values else ""


def _fallback_narrative(extraction: ResearchExtraction, risk: Any) -> NarrativeResult:
    findings: List[NarrativeItem] = []
    recommendations: List[NarrativeItem] = []

    for signal in sorted(extraction.risk_signals, key=lambda item: item.severity, reverse=True)[:4]:
        detail = signal.description or signal.title
        findings.append(
            NarrativeItem(
                text=f"{signal.category}｜{signal.title}：{detail}",
                source_ids=signal.source_ids,
            )
        )
    for event in sorted(extraction.judicial_events, key=lambda item: item.severity, reverse=True)[:3]:
        amount = f"，涉及{event.amount}{event.amount_unit or ''}" if event.amount is not None else ""
        findings.append(
            NarrativeItem(
                text=f"{event.event_type}｜{event.title}{amount}",
                source_ids=event.source_ids,
            )
        )
    if not findings:
        for dimension in sorted(risk.dimensions, key=lambda item: item.contribution, reverse=True):
            if dimension.contribution <= 0:
                continue
            findings.append(
                NarrativeItem(
                    text=f"{dimension.name}贡献{dimension.contribution:.1f}分：{dimension.evidence}",
                    source_ids=dimension.source_ids,
                )
            )
    if extraction.missing_fields:
        findings.append(NarrativeItem(text="信息缺口｜仍需补充核验：" + "、".join(extraction.missing_fields[:8])))
    if not findings:
        findings.append(NarrativeItem(text="当前公开证据未发现可确认的重大异常，但不等于不存在未公开风险。"))

    high_categories = {signal.category for signal in extraction.risk_signals if signal.severity >= 60}
    if any("诉讼" in category or "执行" in category for category in high_categories) or extraction.judicial_events:
        recommendations.append(NarrativeItem(text="核验司法事项的最新进展、责任主体、金额口径及对偿债能力的影响。"))
    if any(keyword in " ".join(high_categories) for keyword in ("股权", "质押", "冻结", "控制")):
        recommendations.append(NarrativeItem(text="穿透核验控股链条、股权质押/冻结比例及实际控制权稳定性。"))
    if any(keyword in " ".join(high_categories) for keyword in ("关联", "担保", "资金占用")):
        recommendations.append(NarrativeItem(text="抽查重大关联交易、担保和资金往来的定价依据、审批程序与回款闭环。"))
    if extraction.conflicts:
        recommendations.append(NarrativeItem(text="对来源冲突字段回看原始公告，并以最新法定披露为准。"))
    if extraction.missing_fields:
        recommendations.append(NarrativeItem(text="向客户补充索取公开信息未覆盖的工商档案、审计报告、授信和担保材料。"))
    recommendations.append(NarrativeItem(text="将本报告作为公开信息初筛与审查导航，不替代银行法定尽职调查。"))
    return NarrativeResult(findings=findings[:7], recommendations=recommendations[:6], review_notes=[])


def _company_payload(extraction: ResearchExtraction) -> Dict[str, Any]:
    # Final serialization guard: no stale model/backfill value can leak into a wrong column.
    sanitize_profile_object(extraction.profile)
    p = extraction.profile
    return {
        "name": p.company_name,
        "short_name": p.short_name,
        "stock_code": p.stock_code,
        "credit_code": p.credit_code,
        "legal_person": p.legal_person,
        "registered_address": p.registered_address,
        "registered_capital": p.registered_capital,
        "company_status": p.company_status,
        "established_date": p.established_date,
        "controlling_shareholder": p.controlling_shareholder,
        "actual_controller": p.actual_controller,
        "main_products": "、".join(p.main_products) if p.main_products else None,
        "industry": p.industry,
        "business_scope": p.business_scope,
        "website": p.website,
        "analysis_date": datetime.now().strftime("%Y/%m/%d"),
    }


def _source_payload(sources: Sequence[SearchSource]) -> List[Dict[str, Any]]:
    return [
        {
            "id": source.id,
            "title": source.title,
            "url": source.url,
            "snippet": (source.snippet or source.text[:500])[:500],
            "source_type": source.source_type,
            "provider": source.provider,
            "search_purpose": source.search_purpose,
            "search_score": round(source.search_score, 3),
            "reliability": round(source.reliability, 2),
            "fetched": source.fetched,
        }
        for source in sources
        if source.url
    ]


def _trace(stage: str, detail: str, status: str = "完成") -> Dict[str, str]:
    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "stage": stage,
        "status": status,
        "detail": detail,
    }


def _valid_ids(source_ids: Sequence[str], allowed: set[str]) -> List[str]:
    return list(dict.fromkeys(source_id for source_id in source_ids if source_id in allowed))


def _identity_matches(entity: EntityQuery, extraction: ResearchExtraction) -> bool:
    profile = extraction.profile
    if entity.query_type == "credit_code":
        return (profile.credit_code or "").upper() == entity.normalized
    if entity.query_type == "stock_code":
        return (profile.stock_code or "") == entity.normalized
    name = (profile.company_name or "").replace(" ", "")
    query = entity.normalized.replace(" ", "")
    core = entity.company_core.replace(" ", "")
    return bool(name and (query in name or name in query or (len(core) >= 4 and core in name)))



_DIRECT_CREDIT_TERMS = (
    "失信被执行人", "列入失信", "限制高消费", "限高", "债务违约", "债券违约",
    "未兑付", "未能清偿到期债务", "到期债务尚未清偿", "逾期债务尚未清偿",
    "债务逾期", "破产清算", "破产重整", "担保代偿",
    "非经营性资金占用", "无法表示意见", "否定意见",
)


def _risk_evidence_support(
    item_text: str,
    source_ids: Sequence[str],
    source_map: Dict[str, SearchSource],
    identity: IdentityResolution,
) -> tuple[bool, int | None]:
    """Validate issuer attribution and the strength of adverse evidence.

    The previous implementation searched the whole page for a company name, a risk
    word and any date/amount.  On long announcements this could incorrectly join the
    issuer heading, a subsidiary's event and an unrelated date into one high-risk
    fact.  This version requires the issuer and adverse term in the *same local
    passage*. A single source may create a high-risk fact only when that passage is a
    concrete official filing; otherwise it remains a review clue capped at low/medium.
    """
    ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id in source_map))
    if not ids:
        return False, None

    aliases_raw = [
        value.strip() for value in (
            identity.company_name, identity.short_name, identity.credit_code, identity.stock_code,
            *identity.aliases,
        ) if value and len(re.sub(r"\s+", "", value)) >= 2
    ]
    aliases = [re.sub(r"\s+", "", value).lower() for value in aliases_raw]
    item = item_text or ""
    direct_terms = [term for term in _DIRECT_CREDIT_TERMS if term in item]
    adverse_terms = list(_DIRECT_CREDIT_TERMS) if direct_terms else [
        term for term in (
            "被执行", "执行标的", "诉讼", "仲裁", "股权冻结", "质押", "违规担保",
            "资金占用", "行政处罚", "监管措施", "立案调查", "虚假记载", "重大违法",
        ) if term in item
    ]
    if not adverse_terms:
        return False, None

    company_re = re.compile(
        r"[\u4e00-\u9fffA-Za-z0-9（）()·&\-]{4,80}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司)"
    )
    negations = ("不存在", "未发生", "未被", "不涉及", "没有", "无相关", "已解除", "已撤销", "已结案", "已清偿")
    concrete_re = re.compile(
        r"(?:20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?|"
        r"\d+(?:\.\d+)?\s*(?:亿元|万元|元)|"
        r"(?:案号|执行案号|公告编号|处罚决定书|裁判文书)[:：]?\s*[^；。\s]{4,})"
    )

    def host_group(source: SearchSource) -> str:
        try:
            host = (urllib.parse.urlparse(source.url).hostname or "").lower().removeprefix("www.")
        except Exception:
            host = ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else (host or source.provider or source.id)

    def official_source(source: SearchSource) -> bool:
        value = f"{source.provider} {source.source_type} {source.title} {source.url}".lower()
        official_tokens = (
            "cninfo", "sse.com", "szse.cn", "bse.cn", "court.gov", "zxgk.court",
            "gov.cn", "信用中国", "人民法院", "证券交易所", "巨潮资讯", "上市公司公告",
        )
        return source.reliability >= 0.88 and ("公告" in source.title or any(token in value for token in official_tokens))

    local_hits: list[tuple[SearchSource, bool, bool]] = []
    groups: set[str] = set()
    for source_id in ids:
        source = source_map[source_id]
        raw = f"{source.title}。{source.snippet or ''}。{source.text or ''}"
        passages = [part.strip() for part in re.split(r"[。；;\n\r]+", raw) if part.strip()]
        found = False
        for index, passage in enumerate(passages):
            compact_passage = re.sub(r"\s+", "", passage).lower()
            for term in adverse_terms:
                term_compact = re.sub(r"\s+", "", term).lower()
                if term_compact not in compact_passage:
                    continue
                term_pos = compact_passage.find(term_compact)
                near_before = compact_passage[max(0, term_pos - 18):term_pos]
                if any(token in near_before for token in negations) or re.search(r"(?:无|未|不|没有).{0,8}$", near_before):
                    continue

                target_aliases = [alias for alias in aliases if alias in compact_passage]
                # A risk sentence naming a different company does not belong to the
                # issuer merely because the issuer appeared in the page title or the
                # preceding sentence.
                if not target_aliases:
                    continue
                mentioned = [re.sub(r"\s+", "", name).lower() for name in company_re.findall(passage)]
                other_entities = [
                    name for name in mentioned
                    if not any(alias in name or name in alias for alias in aliases)
                ]
                target_distance = min(
                    (abs(compact_passage.find(alias) - term_pos) for alias in target_aliases),
                    default=9999,
                )
                if other_entities and target_distance > 90 and not any(
                    token in compact_passage for token in (
                        "本公司承担", "上市公司承担", "连带责任", "交叉违约", "资金占用", "担保代偿"
                    )
                ):
                    continue

                # Amount/date/case may sit in the immediately following sentence of
                # the same filing, but issuer attribution may not.
                context = passage
                if index + 1 < len(passages):
                    context += "。" + passages[index + 1]
                local_official = official_source(source)
                local_concrete = bool(concrete_re.search(context)) or (local_official and "公告" in passage)
                local_hits.append((source, local_concrete, local_official))
                groups.add(host_group(source))
                found = True
                break
            if found:
                break

    if not local_hits:
        return False, None

    independent = len(groups) >= 2
    concrete_count = sum(1 for _, concrete, _ in local_hits if concrete)
    official_concrete = any(concrete and official for _, concrete, official in local_hits)
    reliable_count = sum(1 for source, _, _ in local_hits if source.reliability >= 0.82)

    if direct_terms:
        if independent and (concrete_count or reliable_count >= 2):
            return True, None
        if official_concrete:
            return True, None
        if concrete_count and reliable_count:
            return True, 58
        return True, 44 if reliable_count else 36

    if independent and concrete_count:
        return True, None
    if concrete_count and reliable_count:
        return True, 54
    return True, 42 if reliable_count else 34

def _cap_decision_cards_to_overall(cards: List[Dict[str, Any]], risk_level: str) -> List[Dict[str, Any]]:
    """Keep detail-card severity consistent with the final three-tier conclusion."""
    maximum = "high" if "高" in risk_level else "medium" if "中" in risk_level else "low"
    order = {"low": 1, "medium": 2, "high": 3}
    cap = order[maximum]
    result: List[Dict[str, Any]] = []
    for card in cards:
        item = dict(card)
        severity = item.get("severity", "low")
        if order.get(severity, 1) > cap:
            item["severity"] = maximum
        result.append(item)
    return result


def _sanitize_extraction(
    entity: EntityQuery,
    extraction: ResearchExtraction,
    sources: Sequence[SearchSource],
    locked_identity: IdentityResolution | None = None,
) -> ResearchExtraction:
    """Remove unsupported facts and keep the verified query identity immutable."""
    allowed = {source.id for source in sources}
    profile = extraction.profile
    identity_for_scope = locked_identity or IdentityResolution(
        company_name=profile.company_name,
        short_name=profile.short_name,
        credit_code=profile.credit_code,
        stock_code=profile.stock_code,
        aliases=[],
        source_ids=[],
        confidence=extraction.overall_confidence,
        verified=extraction.identity_verified,
    )
    profile_allowed = {source.id for source in select_profile_sources(entity, identity_for_scope, sources)}
    identity_fields = {"company_name", "short_name", "credit_code", "stock_code"}
    scalar_fields = {
        "legal_person", "registered_address", "registered_capital", "company_status",
        "established_date", "industry", "business_scope", "controlling_shareholder",
        "actual_controller", "website", "main_products",
    }
    sanitized_evidence: Dict[str, FieldEvidence] = {}
    for field_name, evidence in profile.field_evidence.items():
        field_allowed = allowed if field_name in identity_fields else profile_allowed
        valid = list(dict.fromkeys(source_id for source_id in evidence.source_ids if source_id in field_allowed))
        if valid:
            sanitized_evidence[field_name] = FieldEvidence(source_ids=valid, confidence=evidence.confidence)
    profile.field_evidence = sanitized_evidence

    # A scalar profile value without target-scoped evidence is unsafe because many
    # relation/risk pages contain another company's address, legal person or capital.
    for field_name in scalar_fields:
        value = getattr(profile, field_name, None)
        if not value:
            continue
        if field_name == "main_products":
            if not profile.field_evidence.get(field_name):
                profile.main_products = []
        elif not profile.field_evidence.get(field_name):
            setattr(profile, field_name, None)

    extraction.relationships = [
        item.model_copy(update={"source_ids": _valid_ids(item.source_ids, allowed)})
        for item in extraction.relationships
        if _valid_ids(item.source_ids, allowed)
    ]
    source_map = {source.id: source for source in sources}
    verified_identity = locked_identity or identity_for_scope
    sanitized_events = []
    for item in extraction.judicial_events:
        valid_ids = _valid_ids(item.source_ids, allowed)
        supported, severity_cap = _risk_evidence_support(
            f"{item.event_type} {item.title} {item.description} {item.case_number or ''} {item.event_date or ''}",
            valid_ids, source_map, verified_identity,
        )
        if not supported:
            continue
        severity = min(item.severity, severity_cap) if severity_cap is not None else item.severity
        sanitized_events.append(item.model_copy(update={"source_ids": valid_ids, "severity": severity}))
    extraction.judicial_events = sanitized_events
    extraction.annual_facts = [
        item.model_copy(update={"source_ids": _valid_ids(item.source_ids, allowed)})
        for item in extraction.annual_facts
        if _valid_ids(item.source_ids, allowed)
    ]
    sanitized_signals = []
    for item in extraction.risk_signals:
        valid_ids = _valid_ids(item.source_ids, allowed)
        supported, severity_cap = _risk_evidence_support(
            f"{item.category} {item.title} {item.description} {item.event_date or ''}",
            valid_ids, source_map, verified_identity,
        )
        if not supported:
            continue
        severity = min(item.severity, severity_cap) if severity_cap is not None else item.severity
        confidence = min(item.confidence, 0.72) if severity_cap is not None else item.confidence
        sanitized_signals.append(item.model_copy(update={
            "source_ids": valid_ids, "severity": severity, "confidence": confidence,
        }))
    extraction.risk_signals = sanitized_signals

    if locked_identity and locked_identity.verified and locked_identity.company_name:
        evidence = FieldEvidence(source_ids=_valid_ids(locked_identity.source_ids, allowed), confidence=locked_identity.confidence)
        locked_values = {
            "company_name": locked_identity.company_name,
            "short_name": locked_identity.short_name,
            "credit_code": locked_identity.credit_code or (entity.normalized if entity.query_type == "credit_code" else None),
            "stock_code": locked_identity.stock_code or (entity.normalized if entity.query_type == "stock_code" else None),
        }
        for field_name, value in locked_values.items():
            if value:
                setattr(profile, field_name, value)
                if evidence.source_ids:
                    profile.field_evidence[field_name] = evidence
        extraction.identity_verified = True
    else:
        extraction.identity_verified = bool(
            extraction.identity_verified
            and _identity_matches(entity, extraction)
            and (
                profile.field_evidence.get("company_name")
                or profile.field_evidence.get("credit_code")
                or profile.field_evidence.get("stock_code")
            )
        )

    if not extraction.identity_verified and "企业身份核验" not in extraction.missing_fields:
        extraction.missing_fields.insert(0, "企业身份核验")
    extraction.missing_fields = _humanize_missing_fields(extraction.missing_fields)
    extraction.conflicts = list(dict.fromkeys(item for item in extraction.conflicts if item))
    return extraction


def _inject_identity(extraction: ResearchExtraction, identity: IdentityResolution) -> None:
    if not identity.verified:
        return
    profile = extraction.profile
    evidence = FieldEvidence(source_ids=identity.source_ids, confidence=identity.confidence)
    values = {
        "company_name": identity.company_name,
        "short_name": identity.short_name,
        "credit_code": identity.credit_code,
        "stock_code": identity.stock_code,
    }
    for field, value in values.items():
        if value and not getattr(profile, field):
            setattr(profile, field, value)
            profile.field_evidence[field] = evidence


def _stock_hint_source(hint: StockHint) -> SearchSource:
    legal_name = hint.company_name or ""
    title_name = legal_name or hint.short_name
    detail = f"证券代码：{hint.code}；证券简称：{hint.short_name}；交易市场：{hint.exchange}。"
    if legal_name:
        detail += f" 公司全称：{legal_name}。"
    return SearchSource(
        id=f"STOCK-{hint.code}",
        title=f"{hint.provider}：{title_name}（{hint.code}）",
        url=hint.source_url,
        snippet=detail,
        text=detail,
        source_type="structured_stock_master",
        provider=hint.provider,
        search_query=hint.code,
        search_purpose="证券代码解析",
        search_score=1.0,
        reliability=max(0.84, min(0.99, hint.reliability)),
        fetched=True,
    )





def _profile_backfill_rules() -> Dict[str, Sequence[str]]:
    # Public company pages use several equivalent labels.  Captures stop before the
    # next known label so one long flattened page cannot pour the entire company
    # introduction into the address or legal-person cell.
    stop = r"(?=\s*(?:统一社会信用代码|公司代码|股票代码|证券代码|法定代表人|法人代表|注册地址|公司住所|住所|办公地址|注册资本|注册资金|成立日期|成立时间|经营状态|企业状态|登记状态|所属行业|行业|经营范围|主营业务|主要产品|控股股东|实际控制人|最终控制人)\s*[:：]|[；。\n]|$)"
    return {
        "credit_code": [r"统一社会信用代码[:：]\s*([0-9A-Z]{18})"],
        "legal_person": [
            rf"法定代表人[:：]\s*(.{{2,30}}?){stop}",
            rf"法人代表[:：]\s*(.{{2,30}}?){stop}",
        ],
        "registered_address": [
            rf"注册地址[:：]\s*(.{{4,120}}?){stop}",
            rf"公司住所[:：]\s*(.{{4,120}}?){stop}",
            rf"住所[:：]\s*(.{{4,120}}?){stop}",
            rf"办公地址[:：]\s*(.{{4,120}}?){stop}",
        ],
        "registered_capital": [
            rf"注册资本[:：]\s*(.{{2,40}}?){stop}",
            rf"注册资金[:：]\s*(.{{2,40}}?){stop}",
        ],
        "company_status": [
            rf"企业状态[:：]\s*(.{{2,30}}?){stop}",
            rf"经营状态[:：]\s*(.{{2,30}}?){stop}",
            rf"登记状态[:：]\s*(.{{2,30}}?){stop}",
        ],
        "established_date": [
            r"(?:成立日期|成立时间)[:：]\s*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?)"
        ],
        "industry": [
            rf"所属行业[:：]\s*(.{{2,60}}?){stop}",
            rf"行业[:：]\s*(.{{2,60}}?){stop}",
        ],
        "controlling_shareholder": [rf"控股股东[:：]\s*(.{{2,80}}?){stop}"],
        "actual_controller": [
            rf"实际控制人[:：]\s*(.{{2,80}}?){stop}",
            rf"最终控制人[:：]\s*(.{{2,80}}?){stop}",
        ],
        "business_scope": [
            rf"经营范围[:：]\s*(.{{8,900}}?){stop}",
            rf"主营业务[:：]\s*(.{{8,900}}?){stop}",
        ],
    }



def _is_meaningful_profile_value(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and text.lower() not in {"null", "none", "undefined", "未获取", "待核验", "-", "--"})


def _clean_profile_backfill(field_name: str, value: str | None) -> str:
    strict = clean_profile_value(field_name, value)
    if strict:
        return strict
    candidate = (value or "").strip(" ：:，,；;。")
    candidate = re.split(
        r"\s*(?=(?:统一社会信用代码|法定代表人|法人代表|注册地址|公司住所|住所|办公地址|注册资本|注册资金|成立日期|成立时间|经营状态|企业状态|登记状态|所属行业|行业|经营范围|主营业务|控股股东|实际控制人|最终控制人|主要产品)\s*[:：])",
        candidate,
        maxsplit=1,
    )[0].strip(" ：:，,；;。")
    if not _is_meaningful_profile_value(candidate):
        return ""
    if field_name in {"controlling_shareholder", "actual_controller"}:
        if candidate in {"实际", "控制", "股东", "无", "不存在", "未知"}:
            return ""
        if any(token in candidate for token in ("证明", "公告", "报告", "情况", "原因", "无犯罪", "责任", "风险", "核验", "提供")):
            return ""
        company_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9（）()·&\-]{2,46}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司))", candidate)
        if company_match:
            return company_match.group(1)
        if re.fullmatch(r"[\u4e00-\u9fff·]{2,8}", candidate):
            return candidate
        if 2 <= len(candidate) <= 32 and any(token in candidate for token in ("集团", "公司", "委员会", "国资", "投资", "控股")):
            return candidate
        return ""
    if field_name == "industry" and (len(candidate) > 28 or any(token in candidate for token in ("情况", "原因", "分析", "变化", "业务经营", "说明"))):
        return ""
    if field_name == "company_status":
        candidate = re.split(r"[；。， ]", candidate, maxsplit=1)[0].strip()
        if candidate not in {"存续", "在业", "开业", "正常", "注销", "吊销", "迁出", "撤销", "停业", "清算", "歇业", "仍注册"} and len(candidate) > 8:
            return ""
    if field_name == "legal_person":
        candidate = re.split(r"[；。，, ]", candidate, maxsplit=1)[0].strip()
        if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]{2,20}", candidate):
            return ""
    if field_name == "registered_address" and not (5 <= len(candidate) <= 120):
        return ""
    return candidate


def _set_profile_field(profile: CompanyProfile, field_name: str, value: str | None, source_ids: Sequence[str], confidence: float = 0.88) -> None:
    candidate = _clean_profile_backfill(field_name, value)
    if not candidate:
        return
    current = getattr(profile, field_name)
    if _is_meaningful_profile_value(current):
        return
    setattr(profile, field_name, candidate)
    profile.field_evidence[field_name] = FieldEvidence(
        source_ids=list(dict.fromkeys(source_ids)),
        confidence=max(0.5, min(1.0, confidence)),
    )


def _relation_counterparty(profile: CompanyProfile, source_name: str, target_name: str) -> str:
    def norm(value: str | None) -> str:
        return re.sub(r"\s+", "", value or "").lower()
    aliases = {
        norm(profile.company_name), norm(profile.short_name), norm(profile.stock_code), norm(profile.credit_code)
    } - {""}
    source_norm, target_norm = norm(source_name), norm(target_name)
    if source_norm in aliases and target_norm not in aliases:
        return target_name
    if target_norm in aliases and source_norm not in aliases:
        return source_name
    return target_name or source_name


def _backfill_profile_from_relationships(extraction: ResearchExtraction) -> None:
    profile = extraction.profile
    for relation in sorted(extraction.relationships, key=lambda item: item.confidence, reverse=True):
        rel_text = f"{relation.relation_type} {relation.description}"
        candidate = _relation_counterparty(profile, relation.source_name, relation.target_name)
        if not profile.controlling_shareholder and any(key in rel_text for key in ("控股股东", "第一大股东", "控股关系", "控股")):
            _set_profile_field(profile, "controlling_shareholder", candidate, relation.source_ids, relation.confidence)
        if not profile.actual_controller and any(key in rel_text for key in ("实际控制人", "实际控制", "最终控制")):
            _set_profile_field(profile, "actual_controller", candidate, relation.source_ids, relation.confidence)
        if profile.controlling_shareholder and profile.actual_controller:
            break


def _backfill_profile_from_sources(extraction: ResearchExtraction, sources: Sequence[SearchSource]) -> None:
    profile = extraction.profile
    if not sources:
        return
    profile_rules = _profile_backfill_rules()
    ranked = sorted(
        sources,
        key=lambda item: (item.source_type.startswith("structured"), item.reliability, item.fetched, item.search_score),
        reverse=True,
    )
    for field_name, patterns in profile_rules.items():
        if _is_meaningful_profile_value(getattr(profile, field_name)):
            continue
        for source in ranked:
            source_text = f"{source.title}；{source.snippet or ''}；{source.text or ''}"
            for pattern in patterns:
                match = re.search(pattern, source_text, flags=re.I)
                if match:
                    _set_profile_field(profile, field_name, match.group(1), [source.id], source.reliability)
                    break
            if _is_meaningful_profile_value(getattr(profile, field_name)):
                break
    if not profile.main_products:
        for source in ranked:
            source_text = f"{source.snippet or ''}；{source.text or ''}"
            match = re.search(r"(?:主要产品|主营产品|主营业务)[:：]\s*([^。\n]{3,160})", source_text)
            if not match:
                continue
            values = [item.strip() for item in re.split(r"[、；;,，]", match.group(1)) if item.strip()]
            values = [item for item in values if len(item) <= 60 and _is_meaningful_profile_value(item)]
            if values:
                profile.main_products = list(dict.fromkeys(values))[:10]
                profile.field_evidence["main_products"] = FieldEvidence(
                    source_ids=[source.id], confidence=source.reliability
                )
                break


def _reconcile_missing_fields(extraction: ResearchExtraction) -> None:
    label_to_attr = {
        "企业全称": "company_name", "企业简称": "short_name", "统一社会信用代码": "credit_code",
        "股票代码": "stock_code", "法定代表人": "legal_person", "注册地址": "registered_address",
        "注册资本": "registered_capital", "企业状态": "company_status", "成立日期": "established_date",
        "所属行业": "industry", "经营范围": "business_scope", "控股股东": "controlling_shareholder",
        "实际控制人": "actual_controller", "主要产品": "main_products", "官方网站": "website",
    }
    cleaned: List[str] = []
    for label in _humanize_missing_fields(extraction.missing_fields):
        if label == "企业身份核验":
            if extraction.identity_verified:
                continue
        attr = label_to_attr.get(label)
        if attr:
            value = getattr(extraction.profile, attr)
            if isinstance(value, list):
                if value:
                    continue
            elif _is_meaningful_profile_value(value):
                continue
        if label not in cleaned:
            cleaned.append(label)
    extraction.missing_fields = cleaned


def _enrich_profile_fields(extraction: ResearchExtraction, sources: Sequence[SearchSource]) -> ResearchExtraction:
    _backfill_profile_from_relationships(extraction)
    _backfill_profile_from_sources(extraction, sources)
    sanitize_profile_object(extraction.profile)
    _reconcile_missing_fields(extraction)
    return extraction

def _critical_missing(extraction: ResearchExtraction) -> int:
    p = extraction.profile
    return sum(
        not value
        for value in (
            p.legal_person,
            p.registered_address,
            p.registered_capital,
            p.company_status,
            p.established_date,
            p.controlling_shareholder,
            p.actual_controller,
            p.business_scope,
            p.main_products,
        )
    )


async def _resolve_with_model(query: str, sources: Sequence[SearchSource]) -> IdentityResolution:
    identity = await deepseek_client.resolve_identity(query, sources)
    allowed = {source.id for source in sources}
    identity.source_ids = _valid_ids(identity.source_ids, allowed)
    identity.verified = bool(identity.verified and identity.company_name and identity.source_ids)
    return identity


def _identity_from_listed_profile(
    entity: EntityQuery,
    profile: ListedCompanyProfile,
    source: SearchSource,
) -> IdentityResolution:
    name = profile.company_name or profile.short_name
    aliases = [value for value in (profile.short_name, profile.company_name) if value and value != name]
    return IdentityResolution(
        company_name=name or None,
        short_name=profile.short_name or None,
        credit_code=profile.credit_code or None,
        stock_code=profile.stock_code or entity.normalized,
        aliases=aliases,
        source_ids=[source.id],
        confidence=profile.reliability,
        verified=bool(name and profile.stock_code == entity.normalized),
        reason="证券结构化档案确认股票代码与发行人主体",
    )


def _listed_profile_extraction(
    profile: ListedCompanyProfile,
    source: SearchSource,
    identity: IdentityResolution,
) -> ResearchExtraction:
    evidence = FieldEvidence(source_ids=[source.id], confidence=profile.reliability)
    values = {
        "company_name": profile.company_name or identity.company_name,
        "short_name": profile.short_name or identity.short_name,
        "credit_code": profile.credit_code or identity.credit_code,
        "stock_code": profile.stock_code or identity.stock_code,
        "legal_person": profile.legal_person,
        "registered_address": profile.registered_address,
        "registered_capital": profile.registered_capital,
        "company_status": profile.company_status,
        "established_date": profile.established_date,
        "industry": profile.industry,
        "business_scope": profile.business_scope,
        "controlling_shareholder": profile.controlling_shareholder,
        "actual_controller": profile.actual_controller,
        "website": profile.website,
    }
    field_evidence = {key: evidence for key, value in values.items() if value}
    company = CompanyProfile(
        **values,
        main_products=profile.main_products,
        field_evidence=field_evidence,
    )
    return ResearchExtraction(
        profile=company,
        overall_confidence=profile.reliability,
        identity_verified=True,
    )


def _theme_counts(sources: Sequence[SearchSource]) -> Dict[str, int]:
    counts = {"profile": 0, "relations": 0, "risk": 0}
    for source in sources:
        purpose = source.search_purpose or ""
        if any(term in purpose for term in ("主体", "工商", "档案", "年报", "经营披露", "证券")):
            counts["profile"] += 1
        if any(term in purpose for term in ("股权", "控制", "投资", "关联交易", "担保", "子公司")):
            counts["relations"] += 1
        if any(term in purpose for term in ("司法", "监管", "合规", "风险", "诉讼", "处罚", "质押", "冻结")):
            counts["risk"] += 1
    return counts


def _quality_signature(extraction: ResearchExtraction) -> tuple[int, int, int, int, int]:
    """Compact monotonic quality signal used to stop unproductive follow-ups."""
    p = extraction.profile
    known_profile = sum(bool(value) for value in (
        p.company_name, p.credit_code, p.stock_code, p.legal_person,
        p.registered_address, p.registered_capital, p.company_status,
        p.established_date, p.industry, p.controlling_shareholder,
        p.actual_controller, p.main_products,
    ))
    evidence_count = len({
        sid
        for field in p.field_evidence.values()
        for sid in field.source_ids
        if sid
    })
    return (
        known_profile,
        len(extraction.relationships),
        len(extraction.risk_signals) + len(extraction.judicial_events),
        len(extraction.annual_facts),
        evidence_count,
    )


def _followup_needs(
    extraction: ResearchExtraction,
    sources: Sequence[SearchSource],
    failed_lanes: set[str] | None = None,
) -> tuple[bool, bool, bool]:
    """Decide what to补查 from evidence gaps, not from a local credit ceiling.

    No risk event is a valid result when several credible risk searches were completed;
    therefore absence of a risk event alone must not trigger endless searching.
    """
    coverage = _theme_counts(sources)
    failed_lanes = failed_lanes or set()
    p = extraction.profile
    need_profile = "profile" in failed_lanes or _critical_missing(extraction) > 0 or coverage["profile"] < 3
    need_relationships = (
        "relations" in failed_lanes
        or not p.controlling_shareholder
        or not p.actual_controller
        or len(extraction.relationships) < 5
        or coverage["relations"] < 3
    )
    need_risk = "risk" in failed_lanes or coverage["risk"] < 3
    return need_profile, need_relationships, need_risk


def _compact_decision_packet(extraction: ResearchExtraction, risk: Any) -> Dict[str, Any]:
    return {
        "risk_score": risk.risk_score,
        "risk_level": risk.risk_level,
        "confidence": risk.confidence,
        "data_completeness": risk.data_completeness,
        "dimensions": [
            {
                "name": item.name,
                "score": item.score,
                "contribution": item.contribution,
                "evidence": item.evidence[:500],
                "source_ids": item.source_ids,
            }
            for item in sorted(risk.dimensions, key=lambda x: x.contribution, reverse=True)
        ],
        "risk_signals": [
            {
                "category": item.category,
                "title": item.title,
                "description": item.description[:700],
                "severity": item.severity,
                "event_date": item.event_date,
                "source_ids": item.source_ids,
            }
            for item in sorted(extraction.risk_signals, key=lambda x: (x.severity, x.confidence), reverse=True)[:12]
        ],
        "judicial_events": [
            {
                "event_type": item.event_type,
                "title": item.title,
                "description": item.description[:700],
                "severity": item.severity,
                "event_date": item.event_date,
                "amount": item.amount,
                "amount_unit": item.amount_unit,
                "counterparty": item.counterparty,
                "case_number": item.case_number,
                "source_ids": item.source_ids,
            }
            for item in sorted(extraction.judicial_events, key=lambda x: x.severity, reverse=True)[:12]
        ],
        "important_relationships": [
            {
                "source_name": item.source_name,
                "target_name": item.target_name,
                "relation_type": item.relation_type,
                "share_ratio": item.share_ratio,
                "amount": item.amount,
                "amount_unit": item.amount_unit,
                "description": item.description[:500],
                "risk_level": item.risk_level,
                "source_ids": item.source_ids,
            }
            for item in extraction.relationships[:18]
        ],
        "missing_fields": _humanize_missing_fields(extraction.missing_fields),
        "conflicts": extraction.conflicts[:8],
    }


def _sanitize_decision_cards(
    analysis: DecisionAnalysis,
    allowed_source_ids: set[str],
) -> List[Dict[str, Any]]:
    severity_order = {"high": 3, "medium": 2, "low": 1}
    cards: List[Dict[str, Any]] = []
    for card in analysis.cards:
        if not card.title.strip() or not card.action.strip():
            continue
        source_ids = [sid for sid in card.source_ids if sid in allowed_source_ids]
        normalized_severity = "high" if card.severity in {"critical", "high"} else "medium" if card.severity in {"medium", "info"} else "low"
        cards.append({
            "severity": normalized_severity,
            "label": card.label.strip() or "审查重点",
            "title": card.title.strip(),
            "summary": card.summary.strip(),
            "impact": card.impact.strip(),
            "action": card.action.strip(),
            "source_ids": source_ids,
        })
    cards.sort(key=lambda item: severity_order.get(item["severity"], 0), reverse=True)
    return cards[:8]


async def research_company(
    query: str,
    force_refresh: bool = False,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    if hasattr(deepseek_client, "reset_usage"):
        deepseek_client.reset_usage()
    entity = parse_entity_query(query)
    query = entity.normalized
    if not settings.deepseek_ready:
        raise DeepSeekConfigurationError("请先在项目根目录 .env 中配置 DEEPSEEK_API_KEY")
    if not settings.tavily_ready:
        raise TavilyConfigurationError("请先在项目根目录 .env 中配置 TAVILY_API_KEY")

    await _notify(progress_callback, 3, "缓存检查", "检查企业名称、股票代码和社会信用代码的可信缓存")
    if not force_refresh:
        cached = research_cache.get(query)
        if cached:
            cached["summary"]["cache_hit"] = True
            cached["summary"]["analysis_completed_at"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            await _notify(progress_callback, 100, "完成", "已从可信缓存返回完整结果")
            return cached

    trace: List[Dict[str, str]] = []
    search_run = SearchRunResult()
    seed_sources: List[SearchSource] = []
    identity = IdentityResolution()
    stock_hint: StockHint | None = None
    listed_profile: ListedCompanyProfile | None = None
    listed_relations: ListedRelationsBundle | None = None
    listed_relations_task: asyncio.Task | None = None
    name_hints: List[str] = []
    base_extraction = ResearchExtraction()

    # Resolve a listed issuer from multiple structured securities sources in parallel.
    # This is the identity anchor: web mentions of subsidiaries can never replace it.
    if entity.query_type == "stock_code":
        await _notify(progress_callback, 8, "证券主体解析", "并行读取证券主数据与上市公司结构化档案")
        hint_task = resolve_stock_hint(entity.normalized) if settings.stock_resolver_enabled else None
        profile_task = resolve_listed_profile(entity.normalized) if settings.listed_profile_resolver_enabled else None
        tasks = [task for task in (hint_task, profile_task) if task is not None]
        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        for item in results:
            if isinstance(item, StockHint):
                stock_hint = item
            elif isinstance(item, ListedCompanyProfile):
                listed_profile = item

        if stock_hint:
            hint_source = _stock_hint_source(stock_hint)
            seed_sources.append(hint_source)
            name_hints.extend(value for value in (stock_hint.company_name, stock_hint.short_name) if value)
        if listed_profile:
            listed_source = listed_profile.to_source()
            seed_sources.append(listed_source)
            name_hints.extend(value for value in (listed_profile.company_name, listed_profile.short_name) if value)
            identity = _identity_from_listed_profile(entity, listed_profile, listed_source)
            base_extraction = _listed_profile_extraction(listed_profile, listed_source, identity)
        if not identity.verified and seed_sources:
            identity = deterministic_identity(entity, seed_sources, name_hints)
        trace.append(_trace(
            "证券主体解析",
            f"结构化通道确认：{identity.company_name or (stock_hint.display_name if stock_hint else '待公开披露核验')}；获得{len(seed_sources)}个主数据来源",
        ))
        if identity.verified and identity.company_name:
            listed_relations_task = asyncio.create_task(
                resolve_listed_relations(entity.normalized, identity.company_name)
            )

    # Credit-code identities require an exact mapping source. Company-name input is
    # searched as entered, then upgraded to the法定全称 from evidence.
    identity_search_needed = entity.query_type == "credit_code" or (
        entity.query_type == "stock_code" and not identity.verified
    ) or entity.query_type == "company_name"
    if identity_search_needed:
        await _notify(progress_callback, 14, "主体核验", "Tavily正在核验查询条件与法定企业主体")
        try:
            identity_run = await tavily_search_client.search_identity(entity, advanced=False, name_hints=name_hints)
        except TavilySearchError as exc:
            if not identity.verified:
                raise ResearchUnavailableError(str(exc), code=exc.code) from exc
            identity_run = SearchRunResult(warnings=[str(exc)])
        search_run = search_run.merge(identity_run)
        seed_sources = merge_sources(seed_sources, identity_run.sources)
        upgraded = deterministic_identity(entity, seed_sources, name_hints)
        if upgraded.verified:
            identity = upgraded
        elif not identity.verified and seed_sources:
            try:
                identity = await _resolve_with_model(query, seed_sources)
            except Exception as exc:
                trace.append(_trace("主体核验", "模型主体核验未通过格式校验，已使用结构化来源继续核验", "自动修复"))
        trace.append(_trace("主体核验", f"身份来源{len(identity_run.sources)}个，当前主体：{identity.company_name or '未确认'}"))

    provisional_name = (
        (listed_profile.company_name if listed_profile else "")
        or identity.company_name
        or (stock_hint.display_name if stock_hint else "")
        or (entity.normalized if entity.query_type == "company_name" else "")
    )
    if not provisional_name:
        raise ResearchUnavailableError("未能确认企业主体，请检查输入代码或企业全称。", code="IDENTITY_NOT_VERIFIED")

    # Run five purpose-specific searches concurrently. This increases coverage without
    # serially multiplying the user's waiting time.
    await _notify(progress_callback, 24, "主题并行检索", "并行检索法定档案、年报、股权、关联交易和司法监管信息")
    try:
        detail_run = await tavily_search_client.search_details(
            entity,
            provisional_name,
            list(dict.fromkeys([*name_hints, *identity.aliases])),
        )
    except TavilySearchError as exc:
        detail_run = SearchRunResult(warnings=[str(exc)])
    search_run = search_run.merge(detail_run)
    seed_sources = merge_sources(seed_sources, detail_run.sources)

    if listed_relations_task is not None:
        try:
            listed_relations = await listed_relations_task
        except Exception as exc:
            logger.debug("Listed relations resolver failed: %s", exc)
            listed_relations = None
        if listed_relations:
            seed_sources = merge_sources(seed_sources, listed_relations.sources)
            base_extraction = merge_extractions(
                base_extraction,
                ResearchExtraction(
                    relationships=listed_relations.relationships,
                    overall_confidence=0.94,
                    identity_verified=True,
                ),
            )
            trace.append(_trace(
                "股东与子公司解析",
                f"结构化披露补充{len(listed_relations.relationships)}条股东、控制及参控股关系",
            ))

    # A structured stock master confirms the issuer, but it is not enough for a full
    # due-diligence report. If the focused first pass yields too few web sources, run
    # one simplified recovery pass before spending any model tokens.
    web_source_count = sum(not source.source_type.startswith("structured") for source in seed_sources)
    if web_source_count < 3:
        try:
            recovery_entity = (
                parse_entity_query(identity.stock_code)
                if re.fullmatch(r"\d{6}", identity.stock_code or "")
                else entity
            )
            recovery_run = await tavily_search_client.search_followup(
                recovery_entity,
                provisional_name,
                list(dict.fromkeys([*name_hints, *identity.aliases])),
                need_profile=True,
                need_relationships=True,
                need_risk=True,
            )
            search_run = search_run.merge(recovery_run)
            seed_sources = merge_sources(seed_sources, recovery_run.sources)
            trace.append(_trace("主题并行检索", f"首轮来源偏少，恢复检索新增{len(recovery_run.sources)}个候选来源"))
        except TavilySearchError as exc:
            trace.append(_trace("主题并行检索", f"恢复检索未完成：{exc}", "外部服务"))

    # Re-evaluate identity after official disclosure results. For stock codes, the
    # structured issuer remains immutable even when announcements mention subsidiaries.
    upgraded = deterministic_identity(entity, seed_sources, [provisional_name, *name_hints, *identity.aliases])
    if entity.query_type != "stock_code" and upgraded.verified:
        identity = upgraded
        provisional_name = upgraded.company_name or provisional_name
    elif entity.query_type == "stock_code" and identity.verified:
        # Keep the structured issuer immutable, but allow a short security name to
        # upgrade to its legal issuer name when the official disclosure evidence and
        # the same stock code support that mapping.  This prevents both failure modes:
        # leaving the report at “南钢股份”, and replacing the issuer with a subsidiary
        # mentioned inside an announcement.
        legal_upgrade = None
        if listed_profile and listed_profile.company_name:
            legal_upgrade = listed_profile.company_name
        elif upgraded.verified and upgraded.company_name and upgraded.company_name.endswith((
            "股份有限公司", "有限责任公司", "集团有限公司", "有限公司"
        )):
            legal_upgrade = upgraded.company_name
        if legal_upgrade:
            if identity.company_name and identity.company_name != legal_upgrade:
                identity.aliases = list(dict.fromkeys([*identity.aliases, identity.company_name]))
            identity.company_name = legal_upgrade
        identity.short_name = identity.short_name or upgraded.short_name
        identity.credit_code = identity.credit_code or upgraded.credit_code
        identity.source_ids = list(dict.fromkeys([*identity.source_ids, *upgraded.source_ids]))
        identity.confidence = max(identity.confidence, upgraded.confidence)
        identity.aliases = list(dict.fromkeys([*identity.aliases, *upgraded.aliases, *name_hints]))

    if not identity.verified:
        try:
            identity = await _resolve_with_model(query, seed_sources)
        except Exception as exc:
            raise ResearchUnavailableError(
                "企业主体无法通过公开证据一致性校验，请检查查询标识或外部模型状态",
                code="IDENTITY_NOT_VERIFIED",
            ) from exc
    if not identity.company_name:
        identity.company_name = provisional_name
    trace.append(_trace("主体锁定", f"已锁定法定主体：{identity.company_name}，后续子公司名称不得覆盖"))

    # Company-name queries often resolve to a listed issuer only after the identity
    # search.  Upgrade them through the structured securities profile at this point so
    # basic legal fields do not remain blank merely because the user typed a name.
    resolved_code = re.sub(r"\D", "", identity.stock_code or "")[-6:]
    if listed_profile is None and re.fullmatch(r"\d{6}", resolved_code or "") and settings.listed_profile_resolver_enabled:
        try:
            late_profile = await resolve_listed_profile(resolved_code)
        except Exception as exc:
            logger.debug("Late listed profile upgrade failed for %s: %s", resolved_code, exc)
            late_profile = None
        if late_profile:
            listed_profile = late_profile
            listed_source = listed_profile.to_source()
            seed_sources = merge_sources(seed_sources, [listed_source])
            base_extraction = merge_extractions(
                base_extraction, _listed_profile_extraction(listed_profile, listed_source, identity)
            )
            identity.stock_code = identity.stock_code or listed_profile.stock_code
            identity.credit_code = identity.credit_code or listed_profile.credit_code or None
            identity.short_name = identity.short_name or listed_profile.short_name or None
            identity.source_ids = list(dict.fromkeys([*identity.source_ids, listed_source.id]))
            trace.append(_trace("上市公司档案补全", f"结构化证券档案补充{listed_profile.field_count}项基础字段"))

    # Search first, then selectively Extract the best URLs in three themed lanes.
    sources = filter_relevant_sources(
        seed_sources,
        entity,
        [identity.company_name, *identity.aliases, *name_hints],
    )
    if not sources:
        raise ResearchUnavailableError("已确认企业主体，但未获得可用于尽调的公开证据。", code="NO_RELEVANT_SOURCES")
    if sum(not source.source_type.startswith("structured") for source in sources) < 2:
        raise ResearchUnavailableError(
            "企业主体已确认，但公开网页证据仍不足以生成完整尽调报告。请检查Tavily实际账户状态或稍后重试。",
            code="INSUFFICIENT_WEB_EVIDENCE",
        )

    await _notify(progress_callback, 39, "证据精读", "正在定向读取高价值年报、关系与风险页面")
    extract_run = await tavily_search_client.extract_research_sources(sources, company_name=identity.company_name)
    search_run = search_run.merge(extract_run)
    sources = filter_relevant_sources(
        merge_sources(sources, extract_run.sources),
        entity,
        [identity.company_name, *identity.aliases, *name_hints],
    )
    trace.append(_trace("证据精读", f"保留{len(sources)}个高相关来源，其中{sum(s.fetched for s in sources)}个已读取正文"))

    await _notify(progress_callback, 50, "规则解析", "先用主体专属证据提取基础字段并核对主体")
    profile_sources = select_profile_sources(entity, identity, sources)
    extraction = merge_extractions(
        base_extraction,
        parse_local_extraction(entity, identity, profile_sources, relation_sources=sources),
    )
    _inject_identity(extraction, identity)

    # Three bounded Flash calls run concurrently. Wall time is close to one call, but
    # each lane receives a focused context and therefore misses fewer relationships or
    # risks than the former single overloaded prompt.
    await _notify(progress_callback, 59, "并行结构化", "DeepSeek V4 Flash并行抽取企业档案、关系网络和风险事件")
    lane_results = await asyncio.gather(
        deepseek_client.extract_profile(identity.company_name, profile_sources or sources),
        deepseek_client.extract_relationships(identity.company_name, sources),
        deepseek_client.extract_risks(identity.company_name, sources),
        return_exceptions=True,
    )
    lane_specs = (("profile", "企业档案"), ("relations", "关系网络"), ("risk", "风险事件"))
    failed_lanes: set[str] = set()
    for (lane_key, lane_name), lane_result in zip(lane_specs, lane_results):
        if isinstance(lane_result, Exception):
            failed_lanes.add(lane_key)
            trace.append(_trace(
                "并行结构化",
                f"{lane_name}已切换规则解析，后续按证据缺口继续核验",
                "规则补偿",
            ))
            continue
        _inject_identity(lane_result, identity)
        lane_result = _sanitize_extraction(entity, lane_result, sources, identity)
        extraction = merge_extractions(extraction, lane_result)
    extraction = _sanitize_extraction(entity, extraction, sources, identity)
    extraction = _enrich_profile_fields(extraction, select_profile_sources(entity, identity, sources))

    # A name query may reveal the listed stock code only during profile extraction.
    # Upgrade immediately through the structured securities profile, then merge it as
    # the authoritative base for legal fields.  This avoids reports where the issuer
    # is correctly identified but the code, credit code, capital, status or address
    # remain “待核验”.
    extracted_code = re.sub(r"\D", "", extraction.profile.stock_code or identity.stock_code or "")[-6:]
    if listed_profile is None and re.fullmatch(r"\d{6}", extracted_code or "") and settings.listed_profile_resolver_enabled:
        try:
            extracted_profile = await resolve_listed_profile(extracted_code)
        except Exception as exc:
            logger.debug("Post-extraction listed profile upgrade failed for %s: %s", extracted_code, exc)
            extracted_profile = None
        if extracted_profile:
            listed_profile = extracted_profile
            listed_source = listed_profile.to_source()
            sources = merge_sources(sources, [listed_source])
            identity.stock_code = identity.stock_code or listed_profile.stock_code
            identity.credit_code = identity.credit_code or listed_profile.credit_code or None
            identity.short_name = identity.short_name or listed_profile.short_name or None
            identity.source_ids = list(dict.fromkeys([*identity.source_ids, listed_source.id]))
            structured_extraction = _listed_profile_extraction(listed_profile, listed_source, identity)
            extraction = merge_extractions(structured_extraction, extraction)
            _inject_identity(extraction, identity)
            extraction = _sanitize_extraction(entity, extraction, sources, identity)
            extraction = _enrich_profile_fields(extraction, select_profile_sources(entity, identity, sources))
            trace.append(_trace(
                "上市公司档案补全",
                f"根据抽取到的证券代码{extracted_code}补充{listed_profile.field_count}项结构化基础字段",
            ))

    # Adaptive follow-up is driven by unresolved evidence gaps. There is no local
    # credit ceiling and no “halfway budget exhausted” failure. At most a small
    # configured number of rounds is used, and a round stops when it adds no new
    # source or no measurable information gain.
    if settings.adaptive_followup_enabled:
        seen_urls = {source.url.split("#", 1)[0].rstrip("/") for source in sources}
        previous_quality = _quality_signature(extraction)
        for round_index in range(getattr(settings, "tavily_followup_rounds", 2)):
            need_profile, need_relationships, need_risk = _followup_needs(extraction, sources, failed_lanes)
            if not (need_profile or need_relationships or need_risk):
                break
            await _notify(
                progress_callback,
                73 + min(round_index * 5, 7),
                "证据补全",
                "正在补充尚未被公开来源覆盖的关键资料",
            )
            try:
                followup_entity = (
                    parse_entity_query(identity.stock_code)
                    if re.fullmatch(r"\d{6}", identity.stock_code or "")
                    else entity
                )
                followup = await tavily_search_client.search_followup(
                    followup_entity,
                    identity.company_name,
                    identity.aliases,
                    need_profile=need_profile,
                    need_relationships=need_relationships,
                    need_risk=need_risk,
                )
            except TavilySearchError as exc:
                # An actual Tavily account/rate/network error is recorded. It is not
                # confused with the removed internal credit-budget mechanism.
                trace.append(_trace("证据补全", f"补查服务异常：{exc}", "外部服务"))
                break
            search_run = search_run.merge(followup)
            candidate_sources = filter_relevant_sources(
                followup.sources,
                entity,
                [identity.company_name, *identity.aliases, *name_hints],
            )
            new_sources = [
                source for source in candidate_sources
                if source.url.split("#", 1)[0].rstrip("/") not in seen_urls
            ]
            if not new_sources:
                trace.append(_trace("证据补全", "本轮未发现新的高相关证据，停止重复检索", "完成"))
                break
            follow_extract = await tavily_search_client.extract_research_sources(
                new_sources, company_name=identity.company_name
            )
            search_run = search_run.merge(follow_extract)
            new_sources = filter_relevant_sources(
                merge_sources(new_sources, follow_extract.sources),
                entity,
                [identity.company_name, *identity.aliases, *name_hints],
            )
            for source in new_sources:
                seen_urls.add(source.url.split("#", 1)[0].rstrip("/"))
            sources = filter_relevant_sources(
                merge_sources(sources, new_sources),
                entity,
                [identity.company_name, *identity.aliases, *name_hints],
            )
            reruns = []
            if need_profile:
                reruns.append(("profile", deepseek_client.extract_profile(identity.company_name, select_profile_sources(entity, identity, new_sources) or new_sources)))
            if need_relationships:
                reruns.append(("relations", deepseek_client.extract_relationships(identity.company_name, new_sources)))
            if need_risk:
                reruns.append(("risk", deepseek_client.extract_risks(identity.company_name, new_sources)))
            rerun_results = await asyncio.gather(
                *(task for _, task in reruns), return_exceptions=True
            ) if reruns else []
            for (lane_key, _), result in zip(reruns, rerun_results):
                if isinstance(result, Exception):
                    continue
                failed_lanes.discard(lane_key)
                _inject_identity(result, identity)
                extraction = merge_extractions(extraction, _sanitize_extraction(entity, result, sources, identity))
            extraction = _sanitize_extraction(entity, extraction, sources, identity)
            extraction = _enrich_profile_fields(extraction, select_profile_sources(entity, identity, sources))
            current_quality = _quality_signature(extraction)
            trace.append(_trace(
                "证据补全",
                f"第{round_index + 1}轮新增{len(new_sources)}个来源，复跑{len(reruns)}个缺失通道",
            ))
            if current_quality <= previous_quality:
                trace.append(_trace("证据补全", "本轮未增加可核验字段或关系，停止重复调用", "完成"))
                break
            previous_quality = current_quality

    extraction = _sanitize_extraction(entity, extraction, sources, identity)
    extraction = _enrich_profile_fields(extraction, select_profile_sources(entity, identity, sources))
    # The verified identity is the authoritative anchor. A model lane drifting to a
    # subsidiary/related company is repaired rather than aborting the entire query.
    if identity.verified and identity.company_name:
        _inject_identity(extraction, identity)
        extraction.profile.company_name = identity.company_name
        extraction.identity_verified = True
        extraction.missing_fields = [item for item in extraction.missing_fields if item != "企业身份核验"]
    if not extraction.identity_verified:
        raise ResearchUnavailableError("无法从公开来源稳定核验该企业主体，请输入更完整的企业名称或代码。", code="IDENTITY_NOT_VERIFIED")

    await _notify(progress_callback, 86, "风险计算", "Python正在确定性计算风险维度、贡献值和多跳图谱")
    risk = calculate_risk(extraction)
    graph = build_graph(extraction, risk)

    # Decision analysis is the only Pro call and sees only a compact, validated packet.
    # It does not reread the web, so it improves wording and one-to-one actions without
    # repeating the expensive evidence context.
    await _notify(progress_callback, 92, "决策分析", "生成风险事实与一一对应的授信核验建议")
    decision_cards: List[Dict[str, Any]] = []
    decision_model = "确定性规则"
    if settings.enable_pro_decision_analysis:
        try:
            decision_analysis = await deepseek_client.analyze_decisions(
                identity.company_name,
                _compact_decision_packet(extraction, risk),
                [source.id for source in sources],
            )
            decision_cards = _sanitize_decision_cards(decision_analysis, {source.id for source in sources})
            decision_model = display_model_name(settings.review_model) + "（紧凑决策分析）"
        except Exception as exc:
            trace.append(_trace("决策分析", "已使用确定性规则完成风险与建议配对", "规则补偿"))
    if not decision_cards:
        decision_cards = _build_decision_cards(extraction, risk)
    decision_cards = _cap_decision_cards_to_overall(decision_cards, risk.risk_level)
    findings = [f"{card['title']}：{card['summary']}" for card in decision_cards]
    recommendations = [card["action"] for card in decision_cards]
    trace.append(_trace("决策分析", f"形成{len(decision_cards)}组一一对应的风险与建议"))

    duration = time.perf_counter() - started
    usage = deepseek_client.usage_snapshot() if hasattr(deepseek_client, "usage_snapshot") else {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    coverage = _theme_counts(sources)
    report_id = uuid4().hex[:16]
    summary = {
        "risk_score": risk.risk_score,
        "risk_level": risk.risk_level,
        "scorable": risk.scorable,
        "confidence": round(risk.confidence * 100, 1),
        "data_completeness": round(risk.data_completeness * 100, 1),
        "related_company_count": risk.related_company_count,
        "abnormal_transaction_count": risk.abnormal_transaction_count,
        "involved_amount_yi": risk.involved_amount_yi,
        "export_ratio": risk.export_ratio,
        "analysis_completed_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "analysis_seconds": round(duration, 2),
        "source_count": len(sources),
        "identity_verified": extraction.identity_verified,
        "query_type": entity.query_type,
        "cache_hit": False,
        "search_provider": search_run.provider,
        "search_queries": search_run.query_count,
        "search_credits": search_run.credits,
        "search_response_seconds": round(search_run.response_seconds, 2),
        "model_calls": usage.get("calls", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "coverage_profile_sources": coverage["profile"],
        "coverage_relation_sources": coverage["relations"],
        "coverage_risk_sources": coverage["risk"],
        "risk_brief": risk.brief,
        "credit_judgment": risk.credit_judgment,
        "calibration": risk.calibration,
        "scoring_version": getattr(settings, "cache_schema_version", "45"),
        "build_id": getattr(settings, "build_id", "20260802.3"),
    }

    payload = ReportPayload(
        report_id=report_id,
        query=query,
        company=_company_payload(extraction),
        summary=summary,
        risk_dimensions=[item.model_dump() for item in risk.dimensions],
        trend=risk.trend,
        graph=graph.model_dump(),
        findings=findings,
        recommendations=recommendations,
        decision_cards=decision_cards,
        shap=[item.model_dump() for item in risk.shap],
        roi={
            "traditional_days": 5.2,
            "system_seconds": round(duration, 2),
            "loan_amount": None,
            "annual_rate_percent": 4.0,
            "formula": "节省天数 × 贷款金额 × 年资金成本率 ÷ 365",
        },
        decision_trace=trace,
        sources=_source_payload(sources),
        missing_fields=_humanize_missing_fields(extraction.missing_fields),
        conflicts=extraction.conflicts,
        model={
            "provider": "DeepSeek",
            "model_label": "DeepSeek V4 + Tavily",
            "fast_model": display_model_name(settings.fast_model),
            "review_model": decision_model,
            "review_used": decision_model != "确定性规则",
            "review_note": "Pro仅分析紧凑结构化风险包，不重复读取网页正文",
            "identity_resolution_used": entity.query_type != "stock_code" or not bool(listed_profile),
            "extraction_used": usage.get("calls", 0) > 0,
            "narrative_mode": "风险—影响—建议一一配对",
            "api_key_count": len(settings.api_keys),
            "search_provider": search_run.provider,
            "search_credits": search_run.credits,
            "search_request_ids": search_run.request_ids,
            "search_warnings": search_run.warnings,
            "theme_coverage": coverage,
            "token_usage": usage,
        },
    ).model_dump()

    aliases = {
        query,
        extraction.profile.company_name or "",
        extraction.profile.short_name or "",
        extraction.profile.stock_code or "",
        extraction.profile.credit_code or "",
    }
    for alias in aliases:
        if alias:
            research_cache.set(alias, payload)

    await _notify(progress_callback, 100, "完成", f"公开信息尽调完成，用时{duration:.1f}秒")
    return payload

