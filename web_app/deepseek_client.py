from __future__ import annotations

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Type, TypeVar
from uuid import uuid4

try:
    from openai import AsyncOpenAI
except ImportError:  # installed by install.bat in normal use
    AsyncOpenAI = None  # type: ignore
from pydantic import BaseModel, ValidationError

from .config import settings
from .schemas import DecisionAnalysis, IdentityResolution, ResearchExtraction, SearchSource

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class DeepSeekConfigurationError(RuntimeError):
    pass


class DeepSeekResponseError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()




def _extract_json_object(text: str) -> Dict[str, Any]:
    """Extract the first complete JSON object from a model response.

    DeepSeek JSON mode can occasionally return an empty body, markdown fences or
    explanatory text around the object. This parser is deliberately strict about the
    final value being an object, but tolerant about those transport-level wrappers.
    """
    raw = _strip_code_fence(text)
    if not raw:
        raise DeepSeekResponseError("模型返回空内容")
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start < 0:
        raise DeepSeekResponseError("模型响应中未找到JSON对象")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        ch = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start:index + 1]
                try:
                    value = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise DeepSeekResponseError(f"JSON解析失败: {exc}") from exc
                if not isinstance(value, dict):
                    raise DeepSeekResponseError("模型JSON顶层必须是对象")
                return value
    raise DeepSeekResponseError("模型JSON对象未闭合，可能因输出截断")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [item.strip() for item in re.split(r"[、,，；;\n]", text) if item.strip()]
    return [value]


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    number = float(match.group())
    if "%" in text:
        return number
    return number


def _as_int(value: Any, default: int = 0) -> int:
    number = _as_float(value, None)
    return default if number is None else int(round(number))


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    number = _as_float(value, default)
    if isinstance(value, str) and "%" in value and high <= 1 and number is not None:
        number = number / 100.0
    return max(low, min(high, float(number if number is not None else default)))




def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "是", "已核验", "verified"}:
        return True
    if text in {"false", "0", "no", "否", "未核验", "unverified", ""}:
        return False
    return default


def _source_ids(value: Any) -> List[str]:
    return list(dict.fromkeys(_as_text(item) for item in _as_list(value) if _as_text(item)))


def _risk_band(value: Any) -> str:
    text = _as_text(value, "unknown").lower()
    mapping = {
        "低": "low", "低风险": "low", "一般": "low", "一般关注": "low",
        "中": "medium", "中风险": "medium", "重点关注": "medium",
        "高": "high", "高风险": "high", "重大": "critical", "重大风险": "critical",
        "核心": "core", "核心企业": "core", "未知": "unknown", "不确定": "unknown",
    }
    return mapping.get(text, text if text in {"low", "medium", "high", "critical", "unknown", "core"} else "unknown")


def _normalize_research_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    profile = dict(profile)
    for key in (
        "company_name", "short_name", "credit_code", "stock_code", "legal_person",
        "registered_address", "registered_capital", "company_status", "established_date",
        "industry", "business_scope", "controlling_shareholder", "actual_controller", "website",
    ):
        value = profile.get(key)
        profile[key] = _as_text(value) or None
    profile["main_products"] = [_as_text(item) for item in _as_list(profile.get("main_products")) if _as_text(item)][:20]
    evidence = profile.get("field_evidence") if isinstance(profile.get("field_evidence"), dict) else {}
    normalized_evidence: Dict[str, Dict[str, Any]] = {}
    for key, item in evidence.items():
        item = item if isinstance(item, dict) else {"source_ids": item}
        normalized_evidence[_as_text(key)] = {
            "source_ids": _source_ids(item.get("source_ids")),
            "confidence": _clamp(item.get("confidence"), 0, 1, 0.5),
        }
    profile["field_evidence"] = normalized_evidence
    data["profile"] = profile

    relationships = []
    for raw in _as_list(data.get("relationships")):
        if not isinstance(raw, dict):
            continue
        source_name = _as_text(raw.get("source_name") or raw.get("source") or raw.get("from"))
        target_name = _as_text(raw.get("target_name") or raw.get("target") or raw.get("to"))
        relation_type = _as_text(raw.get("relation_type") or raw.get("relation") or raw.get("type"))
        if not source_name or not target_name or not relation_type:
            continue
        relationships.append({
            "source_name": source_name,
            "target_name": target_name,
            "relation_type": relation_type,
            "share_ratio": _as_float(raw.get("share_ratio"), None),
            "amount": _as_float(raw.get("amount"), None),
            "amount_unit": _as_text(raw.get("amount_unit")) or None,
            "description": _as_text(raw.get("description")),
            "source_ids": _source_ids(raw.get("source_ids")),
            "confidence": _clamp(raw.get("confidence"), 0, 1, 0.5),
            "risk_level": _risk_band(raw.get("risk_level")),
        })
    data["relationships"] = relationships

    events = []
    for raw in _as_list(data.get("judicial_events")):
        if not isinstance(raw, dict):
            continue
        event_type = _as_text(raw.get("event_type") or raw.get("type") or "司法事项")
        title = _as_text(raw.get("title") or raw.get("name") or event_type)
        if not title:
            continue
        events.append({
            "event_type": event_type,
            "title": title,
            "event_date": _as_text(raw.get("event_date") or raw.get("date")) or None,
            "amount": _as_float(raw.get("amount"), None),
            "amount_unit": _as_text(raw.get("amount_unit")) or None,
            "counterparty": _as_text(raw.get("counterparty")) or None,
            "case_number": _as_text(raw.get("case_number")) or None,
            "description": _as_text(raw.get("description")),
            "source_ids": _source_ids(raw.get("source_ids")),
            "severity": int(_clamp(raw.get("severity"), 0, 100, 40)),
        })
    data["judicial_events"] = events

    annual = []
    for raw in _as_list(data.get("annual_facts")):
        if not isinstance(raw, dict):
            continue
        metric = _as_text(raw.get("metric") or raw.get("name"))
        value = _as_text(raw.get("value"))
        if not metric or not value:
            continue
        year = _as_int(raw.get("year"), 0)
        annual.append({
            "year": year if 1900 <= year <= 2200 else None,
            "metric": metric,
            "value": value,
            "source_ids": _source_ids(raw.get("source_ids")),
            "confidence": _clamp(raw.get("confidence"), 0, 1, 0.5),
        })
    data["annual_facts"] = annual

    signals = []
    for raw in _as_list(data.get("risk_signals")):
        if not isinstance(raw, dict):
            continue
        title = _as_text(raw.get("title") or raw.get("name"))
        if not title:
            continue
        signals.append({
            "category": _as_text(raw.get("category") or raw.get("type") or "风险信号"),
            "title": title,
            "description": _as_text(raw.get("description")),
            "event_date": _as_text(raw.get("event_date") or raw.get("date")) or None,
            "severity": int(_clamp(raw.get("severity"), 0, 100, 40)),
            "source_ids": _source_ids(raw.get("source_ids")),
            "confidence": _clamp(raw.get("confidence"), 0, 1, 0.5),
        })
    data["risk_signals"] = signals
    data["missing_fields"] = [_as_text(item) for item in _as_list(data.get("missing_fields")) if _as_text(item)]
    data["conflicts"] = [_as_text(item) for item in _as_list(data.get("conflicts")) if _as_text(item)]
    data["overall_confidence"] = _clamp(data.get("overall_confidence"), 0, 1, 0.0)
    data["identity_verified"] = _as_bool(data.get("identity_verified"), True)
    return data


def _normalize_identity_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    data["aliases"] = [_as_text(item) for item in _as_list(data.get("aliases")) if _as_text(item)]
    data["source_ids"] = _source_ids(data.get("source_ids"))
    data["confidence"] = _clamp(data.get("confidence"), 0, 1, 0.0)
    data["verified"] = _as_bool(data.get("verified"), False)
    data["reason"] = _as_text(data.get("reason"))
    return data


def _normalize_decision_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    allowed = {"critical", "high", "medium", "low", "info"}
    cards = []
    for raw in _as_list(data.get("cards")):
        if not isinstance(raw, dict):
            continue
        title = _as_text(raw.get("title"))
        action = _as_text(raw.get("action"))
        if not title or not action:
            continue
        severity = _as_text(raw.get("severity"), "info").lower()
        cards.append({
            "severity": severity if severity in allowed else "info",
            "label": _as_text(raw.get("label"), "审查重点"),
            "title": title,
            "summary": _as_text(raw.get("summary")),
            "impact": _as_text(raw.get("impact")),
            "action": action,
            "source_ids": _source_ids(raw.get("source_ids")),
        })
    data["cards"] = cards
    data["overall_view"] = _as_text(data.get("overall_view"))
    data["review_notes"] = [_as_text(item) for item in _as_list(data.get("review_notes")) if _as_text(item)]
    return data


def _normalize_payload(response_type: Type[T], payload: Dict[str, Any]) -> Dict[str, Any]:
    if response_type is ResearchExtraction:
        return _normalize_research_payload(payload)
    if response_type is IdentityResolution:
        return _normalize_identity_payload(payload)
    if response_type is DecisionAnalysis:
        return _normalize_decision_payload(payload)
    return payload


IDENTITY_FORMAT = {
    "company_name": None,
    "short_name": None,
    "credit_code": None,
    "stock_code": None,
    "aliases": [],
    "source_ids": [],
    "confidence": 0.0,
    "verified": False,
    "reason": "",
}

PROFILE_FORMAT = {
    "profile": {
        "company_name": None, "short_name": None, "credit_code": None, "stock_code": None,
        "legal_person": None, "registered_address": None, "registered_capital": None,
        "company_status": None, "established_date": None, "industry": None,
        "business_scope": None, "controlling_shareholder": None, "actual_controller": None,
        "main_products": [], "website": None,
        "field_evidence": {},
    },
    "relationships": [], "judicial_events": [], "annual_facts": [], "risk_signals": [],
    "missing_fields": [], "conflicts": [], "overall_confidence": 0.0, "identity_verified": True,
}

RELATION_FORMAT = {
    "profile": {"field_evidence": {}},
    "relationships": [{
        "source_name": "", "target_name": "", "relation_type": "",
        "share_ratio": None, "amount": None, "amount_unit": None, "description": "",
        "source_ids": [], "confidence": 0.0, "risk_level": "unknown",
    }],
    "judicial_events": [], "annual_facts": [], "risk_signals": [],
    "missing_fields": [], "conflicts": [], "overall_confidence": 0.0, "identity_verified": True,
}

RISK_FORMAT = {
    "profile": {"field_evidence": {}}, "relationships": [],
    "judicial_events": [{
        "event_type": "", "title": "", "event_date": None, "amount": None,
        "amount_unit": None, "counterparty": None, "case_number": None,
        "description": "", "source_ids": [], "severity": 0,
    }],
    "annual_facts": [{"year": None, "metric": "", "value": "", "source_ids": [], "confidence": 0.0}],
    "risk_signals": [{
        "category": "", "title": "", "description": "", "event_date": None,
        "severity": 0, "source_ids": [], "confidence": 0.0,
    }],
    "missing_fields": [], "conflicts": [], "overall_confidence": 0.0, "identity_verified": True,
}

DECISION_FORMAT = {
    "cards": [{
        "severity": "high", "label": "风险类别", "title": "风险事实标题",
        "summary": "已核验事实及关键数字", "impact": "对授信判断的具体影响",
        "action": "与该风险一一对应、可执行的核验或处置动作", "source_ids": [],
    }],
    "overall_view": "综合判断",
    "review_notes": [],
}


class DeepSeekClient:
    """Quality-first DeepSeek client with bounded, theme-specific contexts.

    Search and evidence retrieval happen outside the model. Flash performs three
    parallel evidence extraction lanes. Pro is used at most once on a compact,
    already-structured risk packet to improve the decision wording; it never reads
    raw web pages, so quality increases without re-burning the research context.
    """

    def __init__(self) -> None:
        self._keys = settings.api_keys
        self._lock = Lock()
        self._index = 0
        self._usage_lock = Lock()
        self._usage_sessions: Dict[str, Dict[str, int]] = {}
        self._usage_session_id: ContextVar[str | None] = ContextVar("deepseek_usage_session_id", default=None)
        self._request_semaphore = asyncio.Semaphore(2)

    @property
    def ready(self) -> bool:
        return bool(self._keys)

    def reset_usage(self) -> None:
        # Each research task gets a session id. Child asyncio tasks inherit the
        # ContextVar and update the same session, so concurrent user queries no
        # longer reset or mix one another's token statistics.
        session_id = uuid4().hex
        self._usage_session_id.set(session_id)
        with self._usage_lock:
            self._usage_sessions[session_id] = {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            }
            # Avoid unbounded growth if the process serves many reports.
            if len(self._usage_sessions) > 256:
                for stale in list(self._usage_sessions)[:-128]:
                    self._usage_sessions.pop(stale, None)

    def usage_snapshot(self) -> Dict[str, int]:
        session_id = self._usage_session_id.get()
        if not session_id:
            return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        with self._usage_lock:
            return dict(self._usage_sessions.get(session_id, {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            }))

    def _next_key(self) -> str:
        if not self._keys:
            raise DeepSeekConfigurationError("未配置 DEEPSEEK_API_KEY")
        with self._lock:
            key = self._keys[self._index % len(self._keys)]
            self._index += 1
        return key

    def _client(self):
        if AsyncOpenAI is None:
            raise DeepSeekConfigurationError("缺少openai依赖，请先运行 install.bat")
        return AsyncOpenAI(
            api_key=self._next_key(),
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout_seconds,
            max_retries=0,
        )

    async def resolve_identity(self, query: str, sources: Sequence[SearchSource]) -> IdentityResolution:
        selected = self._select_sources(sources, lane="identity", limit=7)
        context = self._build_source_context(selected, settings.deepseek_identity_context_chars, 1500)
        prompt = f"""
查询条件：{query}
仅依据编号证据确认中国企业主体。股票代码必须对应上市发行人，不得把公告中出现的子公司、供应商或担保对象当成目标公司。
统一社会信用代码与企业全称必须有同一来源支持。无法确认时verified=false。
source_ids只能引用证据中的编号。仅输出JSON，结构如下：
{json.dumps(IDENTITY_FORMAT, ensure_ascii=False)}

证据：
{context}
""".strip()
        return await self._call_json_model(
            model=settings.fast_model,
            system="你是保守的中国企业主体核验器。禁止使用模型记忆补造企业事实。",
            prompt=prompt,
            response_type=IdentityResolution,
            max_tokens=800,
            thinking=False,
        )

    async def extract_profile(self, query: str, sources: Sequence[SearchSource]) -> ResearchExtraction:
        selected = self._select_sources(sources, lane="profile", limit=12)
        context = self._build_source_context(selected, settings.deepseek_profile_context_chars, 2600)
        prompt = f"""
目标企业：{query}
抽取企业法定主体与经营档案：企业全称、简称、统一社会信用代码、股票代码、法定代表人、注册地址、注册资本、存续状态、精确成立日期、行业、经营范围、主要产品、官网、控股股东、实际控制人，以及年报中可直接核验的关键经营事实。
规则：
1. 目标企业名称不可被子公司或公告相对方替换。
2. 每个非空字段必须在field_evidence中列出source_ids和confidence。
3. 不得用常识或模型记忆填空；来源冲突写入conflicts。
4. 成立日期须是明确日期；主要产品只保留证据明示内容。
5. annual_facts最多20条，优先最新3个年度的净利润/扣非净利润、毛利率、资产负债率、经营活动现金流、货币资金、有息负债、流动比率、审计意见等可量化指标；同一指标尽量保留连续年度，便于识别趋势。
6. 仅输出JSON：
{json.dumps(PROFILE_FORMAT, ensure_ascii=False)}

证据：
{context}
""".strip()
        return await self._call_json_model(
            model=settings.fast_model,
            system="你是银行尽调中的企业档案抽取专家，重视法定主体一致性和字段证据。",
            prompt=prompt,
            response_type=ResearchExtraction,
            max_tokens=settings.deepseek_profile_max_tokens,
            thinking=False,
        )

    async def extract_relationships(self, query: str, sources: Sequence[SearchSource]) -> ResearchExtraction:
        selected = self._select_sources(sources, lane="relations", limit=14)
        context = self._build_source_context(selected, settings.deepseek_relation_context_chars, 3000)
        prompt = f"""
目标企业：{query}
抽取有公开证据支持的关系网络：控股股东、实际控制人、直接/间接股东、主要子公司、参股企业、对外投资、共同控制、关联方、关联交易、销售/采购关系、担保与被担保、资金占用关系。
规则：
1. 必须保留真实 source_name → target_name，禁止把所有关系强制改成核心企业直连。
2. 明确区分“目标企业的子公司”与“目标企业本身”；公告中提到的公司不能自动视为关联方。
3. 持股比例、金额和单位必须来自证据；不确定填null。
4. 每条关系必须有source_ids；同一关系去重，优先最新法定披露。
5. 最多28条，优先控制链、重要子公司、重大关联交易和担保链。
6. 仅输出JSON：
{json.dumps(RELATION_FORMAT, ensure_ascii=False)}

证据：
{context}
""".strip()
        return await self._call_json_model(
            model=settings.fast_model,
            system="你是企业股权穿透和关联关系抽取专家，只保留有证据的方向性关系。",
            prompt=prompt,
            response_type=ResearchExtraction,
            max_tokens=settings.deepseek_relation_max_tokens,
            thinking=False,
        )

    async def extract_risks(self, query: str, sources: Sequence[SearchSource]) -> ResearchExtraction:
        selected = self._select_sources(sources, lane="risk", limit=14)
        context = self._build_source_context(selected, settings.deepseek_risk_context_chars, 3000)
        prompt = f"""
目标企业：{query}
抽取与对公信贷相关的公开风险事实：重大诉讼/仲裁、被执行/失信/限高、行政处罚/监管措施、股权冻结/质押、对外担保与或有负债、资金占用、关联交易异常、审计非标或内控缺陷、重大经营异常、债务与偿付风险，以及持续亏损、高资产负债率、经营现金流恶化、有息负债显著高于货币资金等财务压力。
规则：
1. 普通业务新闻、正常担保或正常关联交易不能直接标为高风险。
2. 每个事件/信号必须有source_ids，写清日期、金额、对象、案号（证据未给则null）。
3. severity按“当前状态×金额/比例×直接偿债影响×证据强度”评估；模型不得仅凭关键词给高分。普通关联交易、正常经营担保、一般股权变动、已结案诉讼、已解除冻结必须为低或不输出。
4. 只有当前失信/限高、债务违约或逾期未清偿、破产、重大未决执行、担保代偿、非经营性资金占用、严重审计异常等，才可给80分以上。监管关注、一般处罚、一般诉讼通常不超过60分，除非证据明确“重大/持续/高金额/未整改”。
5. 对失信、违约、破产、重大执行等高风险事实，必须满足至少一项：两个独立来源相互印证；或单一法定/官方来源给出明确案号、公告编号、金额或日期。仅有一句搜索摘要、未说明案号金额或当前状态时，severity不得超过58。
6. 尚未结案、已解除、已履行、已整改等状态必须写入title或description；不得把历史风险写成当前风险。连续亏损、高杠杆、现金流持续为负应作为经营偿债风险，不因国资背景而忽略；国资背景只能作为缓释因素。
7. 不得把子公司的风险自动归到母公司；只有证据明确存在控制、担保代偿或风险传导时才能关联。
8. 同一事项被多个网页重复报道只保留一条，judicial_events最多16条，risk_signals最多16条，优先最新、重大、可量化事项。
9. 仅输出JSON：
{json.dumps(RISK_FORMAT, ensure_ascii=False)}

证据：
{context}
""".strip()
        return await self._call_json_model(
            model=settings.fast_model,
            system="你是银行对公信贷风险证据抽取专家，区分事实、状态与推断，避免夸大风险。",
            prompt=prompt,
            response_type=ResearchExtraction,
            max_tokens=settings.deepseek_risk_max_tokens,
            thinking=False,
        )

    async def extract_research(self, query: str, sources: Sequence[SearchSource]) -> ResearchExtraction:
        """Backward-compatible aggregate method used by old tests/integrations."""
        profile, relations, risks = await asyncio.gather(
            self.extract_profile(query, sources),
            self.extract_relationships(query, sources),
            self.extract_risks(query, sources),
        )
        # Imported lazily to avoid an import cycle at module import time.
        from .evidence_parser import merge_extractions
        return merge_extractions(merge_extractions(profile, relations), risks)

    async def analyze_decisions(
        self,
        company_name: str,
        compact_packet: Dict[str, Any],
        allowed_source_ids: Sequence[str],
    ) -> DecisionAnalysis:
        prompt = f"""
企业：{company_name}
根据下面已经完成证据校验和数值计算的结构化数据，生成适合银行客户经理阅读的“风险事实—影响—对应建议”决策对。
硬性规则：
1. 风险与建议必须一一对应；每张卡只处理一个风险主题。
2. 不得创造新事实、金额或主体；source_ids只能从允许编号中选。
3. risk title与summary写事实，不写空泛结论；impact说明为什么影响授信；action写具体核验步骤和需要取得的材料。
4. severity只能是high/medium/low；数据缺口按medium表示需核验，不得创造第四档。至少覆盖有实质贡献的风险维度、重大事件和数据缺口。
5. 不要输出“建议关注”“加强管理”等空话；每条action应可执行。
6. cards按严重程度从高到低，最多8条；没有实质风险时可给low/info，但必须说明公开信息边界。
7. 仅输出JSON：
{json.dumps(DECISION_FORMAT, ensure_ascii=False)}

允许的证据编号：{json.dumps(list(allowed_source_ids), ensure_ascii=False)}
结构化数据：
{json.dumps(compact_packet, ensure_ascii=False, separators=(',', ':'))}
""".strip()
        model = settings.review_model if settings.enable_pro_decision_analysis else settings.fast_model
        return await self._call_json_model(
            model=model,
            system="你是资深银行对公信贷审查员。输出直接、准确、可执行的风险与建议配对。",
            prompt=prompt,
            response_type=DecisionAnalysis,
            max_tokens=settings.deepseek_decision_max_tokens,
            thinking=False,
        )

    async def extract_followup(self, query: str, sources: Sequence[SearchSource], focus: str) -> ResearchExtraction:
        focus_lower = focus.lower()
        if any(word in focus_lower for word in ("股东", "控制", "子公司", "关联", "担保", "投资")):
            return await self.extract_relationships(query, sources)
        if any(word in focus_lower for word in ("诉讼", "处罚", "风险", "质押", "冻结", "审计")):
            return await self.extract_risks(query, sources)
        return await self.extract_profile(query, sources)

    async def review_extraction(self, query, extraction, sources):
        return extraction

    async def _call_json_model(
        self,
        model: str,
        system: str,
        prompt: str,
        response_type: Type[T],
        max_tokens: int,
        *,
        thinking: bool = False,
    ) -> T:
        last_error: Optional[Exception] = None
        model_candidates = [model]
        if (
            model == settings.fast_model
            and settings.deepseek_structured_fallback_to_pro
            and settings.review_model not in model_candidates
        ):
            # The fast model remains the normal path. Pro is used only when the
            # complete Flash response repeatedly fails JSON/schema validation, so
            # quality is recovered without making every query expensive.
            model_candidates.append(settings.review_model)

        for model_index, selected_model in enumerate(model_candidates):
            attempts = max(2, settings.deepseek_max_retries + 1) if model_index == 0 else 1
            repair_note = ""
            for attempt in range(attempts):
                client = self._client()
                try:
                    current_prompt = prompt + repair_note
                    token_limit = min(8000, int(max_tokens * (1.2 if attempt else 1)))
                    async with self._request_semaphore:
                        async with asyncio.timeout(settings.deepseek_timeout_seconds + 3):
                            kwargs: Dict[str, Any] = {
                                "model": selected_model,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": current_prompt},
                                ],
                                "response_format": {"type": "json_object"},
                                "stream": False,
                                "max_tokens": token_limit,
                                "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
                            }
                            if not thinking:
                                kwargs["temperature"] = 0
                            response = await client.chat.completions.create(**kwargs)
                    self._record_usage(response)
                    choice = response.choices[0]
                    content = choice.message.content or ""
                    payload = _extract_json_object(content)
                    normalized = _normalize_payload(response_type, payload)
                    try:
                        return response_type.model_validate(normalized)
                    except ValidationError as exc:
                        raise DeepSeekResponseError(f"JSON字段校验失败: {exc}") from exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "DeepSeek JSON调用失败 model=%s attempt=%s: %s",
                        selected_model, attempt + 1, exc,
                    )
                    compact_error = re.sub(r"\s+", " ", str(exc))[:500]
                    repair_note = (
                        "\n\n上一响应未通过JSON校验。请重新输出一个完整、可解析的JSON对象，"
                        "不要使用Markdown代码块，不要解释，不要省略顶层键。"
                        f"校验问题：{compact_error}"
                    )
                finally:
                    try:
                        await client.close()
                    except Exception:
                        pass
        raise DeepSeekResponseError(f"模型结构化结果连续未通过校验：{last_error or '未知错误'}")

    def _record_usage(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        session_id = self._usage_session_id.get()
        if not session_id:
            # Defensive fallback for direct client use outside research_company().
            self.reset_usage()
            session_id = self._usage_session_id.get()
        with self._usage_lock:
            bucket = self._usage_sessions.setdefault(session_id or "default", {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            })
            bucket["calls"] += 1
            if usage is not None:
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
                bucket["prompt_tokens"] += prompt_tokens
                bucket["completion_tokens"] += completion_tokens
                bucket["total_tokens"] += total_tokens

    @staticmethod
    def _select_sources(sources: Sequence[SearchSource], *, lane: str, limit: int) -> List[SearchSource]:
        lane_terms = {
            "identity": ("身份", "证券", "结构化", "法定", "工商"),
            "profile": ("结构化", "工商", "法定", "年报", "主营", "披露", "档案"),
            "relations": ("股权", "控制", "投资", "子公司", "关联交易", "担保"),
            "risk": ("司法", "合规", "风险", "处罚", "诉讼", "质押", "冻结", "审计"),
        }.get(lane, ())
        def lane_score(source: SearchSource) -> tuple:
            purpose = source.search_purpose or ""
            match = sum(term in purpose or term in source.title for term in lane_terms)
            structured = source.source_type.startswith("structured")
            official = source.reliability >= 0.9
            return (match, structured, official, source.reliability, source.search_score, len(source.text or source.snippet))
        ranked = sorted(sources, key=lane_score, reverse=True)
        selected: List[SearchSource] = []
        seen_hosts: Dict[str, int] = {}
        for source in ranked:
            host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", source.url).split("/", 1)[0])
            if seen_hosts.get(host, 0) >= 3 and not source.source_type.startswith("structured"):
                continue
            selected.append(source)
            seen_hosts[host] = seen_hosts.get(host, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def _build_source_context(sources: Sequence[SearchSource], max_chars: int, per_source: int) -> str:
        parts: List[str] = []
        used = 0
        for source in sources:
            body = (source.text or source.snippet or "")[:per_source]
            if not body:
                continue
            block = (
                f"\n[{source.id}] {source.title}\n"
                f"URL: {source.url}\n"
                f"主题: {source.search_purpose} | 可靠度: {source.reliability:.2f}\n"
                f"内容: {body}\n"
            )
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 360:
                    parts.append(block[:remaining])
                break
            parts.append(block)
            used += len(block)
        return "".join(parts) if parts else "没有可用公开证据。"


deepseek_client = DeepSeekClient()
