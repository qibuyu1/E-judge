from __future__ import annotations

import logging
import re
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .cache import research_cache
from .config import ROOT, settings
from .query_utils import QueryValidationError, display_model_name, parse_entity_query
from .report_service import research_company
from .schemas import AnalyzeRequest
from .search_service import TavilyConfigurationError, TavilySearchError, tavily_search_client
from .task_manager import task_manager
from .stock_resolver import resolve_stock_hint

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

FRONTEND = ROOT / "frontend"
app = FastAPI(
    title="链网E判",
    description="基于Tavily聚焦检索、DeepSeek V4 Flash非思考抽取和Python确定性计算的企业尽调系统",
    version=settings.app_version,
)
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.middleware("http")
async def disable_stale_frontend_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/report"} or request.url.path.startswith("/static/") or request.url.path.startswith("/api/config"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    response.headers["X-Chain-Risk-Build"] = settings.build_id
    return response


@app.exception_handler(QueryValidationError)
async def query_validation_error(_: Request, exc: QueryValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc), "code": "INVALID_QUERY"})


def _flatten_validation_errors(errors: Any) -> str:
    """把 pydantic 的结构化校验错误摊平成一句可读中文。"""
    messages: list[str] = []
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        message = str(error.get("msg") or "").strip()
        if not message:
            continue
        message = re.sub(r"^value error[,:]\s*", "", message, flags=re.IGNORECASE).strip()
        if message and message not in messages:
            messages.append(message)
    return "；".join(messages) or "请求参数校验失败"


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """前端只接受字符串 detail；模型层校验失败也要返回同样的形状，避免弹窗显示 [object Object]。"""
    return JSONResponse(
        status_code=422,
        content={"detail": _flatten_validation_errors(exc.errors()), "code": "INVALID_QUERY"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/report")
def report_page() -> FileResponse:
    return FileResponse(FRONTEND / "report.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok" if settings.api_ready else "configuration_required",
        "app": settings.app_name,
        "version": settings.app_version,
        "build_id": settings.build_id,
        "system_ready": settings.api_ready,
        "deepseek_ready": settings.deepseek_ready,
        "tavily_ready": settings.tavily_ready,
        "search_provider": "Tavily 五主题并行检索 + 自适应证据补全",
        "fast_model": settings.fast_model,
        "review_model": settings.review_model if settings.enable_pro_decision_analysis else None,
        "pro_review_enabled": settings.enable_pro_decision_analysis,
        "stock_resolver_enabled": settings.stock_resolver_enabled,
        "extract_enabled": settings.tavily_extract_enabled,
        "deepseek_key_count": len(settings.api_keys),
    }


@app.get("/api/config")
def public_config() -> Dict[str, Any]:
    return {
        "app_name": settings.app_name,
        "version": settings.app_version,
        "build_id": settings.build_id,
        "model_label": "DeepSeek V4 + Tavily",
        "fast_model": display_model_name(settings.fast_model),
        "review_model": display_model_name(settings.review_model) if settings.enable_pro_decision_analysis else "未启用",
        "search_provider": "Tavily 五主题并行检索 + 自适应证据补全",
        "deepseek_ready": settings.deepseek_ready,
        "search_ready": settings.tavily_ready,
        "api_ready": settings.api_ready,
        "cache_ttl_hours": settings.cache_ttl_hours,
        "pipeline_strategy": "证券结构化档案 + Tavily五主题并行检索/定向Extract + 证据缺口补全 + Flash三通道抽取 + 紧凑Pro决策分析",
        "cost_control": "缓存、查询去重、定向Extract和无增益停止；不设置会中断调查的本地信用点上限",
        "followup_rounds": settings.tavily_followup_rounds,
    }


@app.post("/api/validate-query")
def validate_query(request: AnalyzeRequest) -> Dict[str, Any]:
    entity = parse_entity_query(request.query)
    return {"valid": True, "normalized": entity.normalized, "query_type": entity.query_type}


@app.post("/api/research")
async def start_research(request: AnalyzeRequest) -> Dict[str, Any]:
    if not settings.deepseek_ready:
        raise HTTPException(status_code=503, detail="未配置DeepSeek API，请先填写项目根目录 .env 中的 DEEPSEEK_API_KEY")
    if not settings.tavily_ready:
        raise HTTPException(status_code=503, detail="未配置Tavily API，请先填写项目根目录 .env 中的 TAVILY_API_KEY")
    entity = parse_entity_query(request.query)
    state = task_manager.create(entity.normalized, request.force_refresh)
    return state.model_dump()


@app.get("/api/research/{task_id}")
def research_status(task_id: str) -> Dict[str, Any]:
    state = task_manager.get_state(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return state.model_dump()


@app.get("/api/reports/{report_id}")
def read_report(report_id: str) -> Dict[str, Any]:
    report = task_manager.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在或服务已重启")
    return report


@app.post("/api/analyze")
async def analyze_blocking(request: AnalyzeRequest) -> Dict[str, Any]:
    try:
        return await research_company(request.query, request.force_refresh)
    except (QueryValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (TavilyConfigurationError, TavilySearchError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/search/debug")
async def search_debug(q: str = Query(min_length=2, max_length=120)) -> Dict[str, Any]:
    """Runs one real Tavily identity search. It consumes one Tavily credit."""
    if not settings.tavily_ready:
        raise HTTPException(status_code=503, detail="未配置 TAVILY_API_KEY")
    try:
        run = await tavily_search_client.debug_search(q)
    except (QueryValidationError, TavilyConfigurationError, TavilySearchError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "query": q,
        "provider": run.provider,
        "result_count": len(run.sources),
        "credits": run.credits,
        "response_seconds": run.response_seconds,
        "warnings": run.warnings,
        "results": [
            {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "score": source.search_score,
                "reliability": source.reliability,
                "purpose": source.search_purpose,
                "snippet": source.snippet[:300],
            }
            for source in run.sources
        ],
    }


@app.get("/api/tavily/usage")
async def tavily_usage() -> Dict[str, Any]:
    """Read the real Tavily account usage. This endpoint does not consume search credits."""
    if not settings.tavily_ready:
        raise HTTPException(status_code=503, detail="未配置 TAVILY_API_KEY")
    try:
        return await tavily_search_client.get_usage()
    except (TavilyConfigurationError, TavilySearchError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/stock/debug")
async def stock_debug(q: str = Query(min_length=6, max_length=32)) -> Dict[str, Any]:
    """Resolve a stock code without consuming Tavily credits or DeepSeek tokens."""
    try:
        entity = parse_entity_query(q)
    except QueryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if entity.query_type != "stock_code":
        raise HTTPException(status_code=422, detail="请输入6位股票代码")
    hint = await resolve_stock_hint(entity.normalized)
    if hint is None:
        return {
            "query": q,
            "normalized": entity.normalized,
            "resolved": False,
            "short_name": None,
            "company_name": None,
            "exchange": None,
            "provider": None,
            "source_url": None,
            "reliability": 0,
            "message": "证券主数据暂未返回；正式查询会转入Tavily代码核验，不会生成所谓部分报告。",
        }
    return {
        "query": q,
        "normalized": entity.normalized,
        "resolved": True,
        "short_name": hint.short_name,
        "company_name": hint.company_name or hint.short_name,
        "exchange": hint.exchange,
        "provider": hint.provider,
        "source_url": hint.source_url,
        "reliability": hint.reliability,
        "message": "已确认股票代码对应上市主体，可直接进入完整尽调。",
    }


@app.get("/api/history")
def history(limit: int = Query(default=30, ge=1, le=100)) -> Dict[str, Any]:
    items = research_cache.list_history(limit)
    return {"items": items, "count": len(items)}


@app.get("/api/history/{report_id}")
def history_report(report_id: str) -> Dict[str, Any]:
    report = research_cache.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="历史报告不存在或已被清理")
    return report


@app.delete("/api/history/{report_id}")
def delete_history(report_id: str) -> Dict[str, Any]:
    return {"deleted": research_cache.delete_history(report_id)}


@app.delete("/api/cache")
def clear_cache() -> Dict[str, Any]:
    return {"cleared": research_cache.clear(), "build_id": settings.build_id}
