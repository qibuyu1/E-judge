from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .query_utils import parse_entity_query


RiskBand = Literal["low", "medium", "high", "critical", "unknown", "core"]


class SearchSource(BaseModel):
    id: str
    title: str = ""
    url: str = ""
    snippet: str = ""
    text: str = ""
    source_type: str = "tavily_search"
    provider: str = "tavily"
    search_query: str = ""
    search_purpose: str = ""
    search_score: float = Field(default=0, ge=0, le=1)
    reliability: float = Field(default=0.5, ge=0, le=1)
    fetched: bool = False
    fetched_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class FieldEvidence(BaseModel):
    source_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)


class CompanyProfile(BaseModel):
    company_name: Optional[str] = None
    short_name: Optional[str] = None
    credit_code: Optional[str] = None
    stock_code: Optional[str] = None
    legal_person: Optional[str] = None
    registered_address: Optional[str] = None
    registered_capital: Optional[str] = None
    company_status: Optional[str] = None
    established_date: Optional[str] = None
    industry: Optional[str] = None
    business_scope: Optional[str] = None
    controlling_shareholder: Optional[str] = None
    actual_controller: Optional[str] = None
    main_products: List[str] = Field(default_factory=list)
    website: Optional[str] = None
    field_evidence: Dict[str, FieldEvidence] = Field(default_factory=dict)


class Relationship(BaseModel):
    source_name: str
    target_name: str
    relation_type: str
    share_ratio: Optional[float] = Field(default=None, ge=0, le=100)
    amount: Optional[float] = Field(default=None, ge=0)
    amount_unit: Optional[str] = None
    description: str = ""
    source_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    risk_level: RiskBand = "unknown"


class JudicialEvent(BaseModel):
    event_type: str
    title: str
    event_date: Optional[str] = None
    amount: Optional[float] = Field(default=None, ge=0)
    amount_unit: Optional[str] = None
    counterparty: Optional[str] = None
    case_number: Optional[str] = None
    description: str = ""
    source_ids: List[str] = Field(default_factory=list)
    severity: int = Field(default=40, ge=0, le=100)


class AnnualFact(BaseModel):
    year: Optional[int] = Field(default=None, ge=1900, le=2200)
    metric: str
    value: str
    source_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class RiskSignal(BaseModel):
    category: str
    title: str
    description: str = ""
    event_date: Optional[str] = None
    severity: int = Field(default=40, ge=0, le=100)
    source_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class ResearchExtraction(BaseModel):
    profile: CompanyProfile = Field(default_factory=CompanyProfile)
    relationships: List[Relationship] = Field(default_factory=list)
    judicial_events: List[JudicialEvent] = Field(default_factory=list)
    annual_facts: List[AnnualFact] = Field(default_factory=list)
    risk_signals: List[RiskSignal] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    overall_confidence: float = Field(default=0, ge=0, le=1)
    identity_verified: bool = False


class IdentityResolution(BaseModel):
    company_name: Optional[str] = None
    short_name: Optional[str] = None
    credit_code: Optional[str] = None
    stock_code: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    verified: bool = False
    reason: str = ""


class NarrativeItem(BaseModel):
    text: str
    source_ids: List[str] = Field(default_factory=list)


class NarrativeResult(BaseModel):
    findings: List[NarrativeItem] = Field(default_factory=list)
    recommendations: List[NarrativeItem] = Field(default_factory=list)
    review_notes: List[str] = Field(default_factory=list)


class RiskDimension(BaseModel):
    name: str
    key: str
    score: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    contribution: float
    evidence: str = ""
    source_ids: List[str] = Field(default_factory=list)


class ShapRow(BaseModel):
    feature: str
    contribution: float
    direction: str
    evidence: str
    source_ids: List[str] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str
    name: str
    risk: RiskBand = "unknown"
    category: str = "企业"
    description: str = ""
    company_type: str = "企业"
    relation_type: str = ""
    source_ids: List[str] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str
    weight: float = Field(default=0.5, ge=0, le=1)
    amount: str = ""
    source_ids: List[str] = Field(default_factory=list)


class GraphPayload(BaseModel):
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)


class RiskResult(BaseModel):
    scorable: bool = False
    risk_score: Optional[float] = Field(default=None, ge=0, le=100)
    risk_level: str = "证据不足"
    confidence: float = Field(default=0, ge=0, le=1)
    data_completeness: float = Field(default=0, ge=0, le=1)
    dimensions: List[RiskDimension] = Field(default_factory=list)
    shap: List[ShapRow] = Field(default_factory=list)
    related_company_count: int = 0
    abnormal_transaction_count: int = 0
    involved_amount_yi: Optional[float] = None
    export_ratio: Optional[float] = None
    trend: Dict[str, List[Any]] = Field(default_factory=lambda: {"labels": [], "values": []})
    brief: List[str] = Field(default_factory=list)
    credit_judgment: Dict[str, Any] = Field(default_factory=dict)
    calibration: Dict[str, Any] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=120)
    force_refresh: bool = False

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        return parse_entity_query(value).normalized


class TaskState(BaseModel):
    task_id: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    stage: str = "等待处理"
    message: str = ""
    report_id: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None




class DecisionCard(BaseModel):
    severity: Literal["critical", "high", "medium", "low", "info"] = "info"
    label: str = "审查重点"
    title: str
    summary: str = ""
    impact: str = ""
    action: str = ""
    source_ids: List[str] = Field(default_factory=list)


class DecisionAnalysis(BaseModel):
    cards: List[DecisionCard] = Field(default_factory=list)
    overall_view: str = ""
    review_notes: List[str] = Field(default_factory=list)


class ReportPayload(BaseModel):
    report_id: str
    query: str
    data_mode: str = "live_public_web"
    company: Dict[str, Any]
    summary: Dict[str, Any]
    risk_dimensions: List[Dict[str, Any]]
    trend: Dict[str, List[Any]]
    graph: Dict[str, Any]
    findings: List[str]
    recommendations: List[str]
    decision_cards: List[Dict[str, Any]] = Field(default_factory=list)
    shap: List[Dict[str, Any]]
    roi: Dict[str, Any]
    decision_trace: List[Dict[str, Any]]
    sources: List[Dict[str, Any]]
    missing_fields: List[str]
    conflicts: List[str]
    model: Dict[str, Any]
