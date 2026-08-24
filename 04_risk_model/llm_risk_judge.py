from __future__ import annotations

import asyncio
from typing import Any, Dict

from pydantic import BaseModel

from web_app.config import settings
from web_app.deepseek_client import deepseek_client


class _RiskLabel(BaseModel):
    label: str
    level: str
    reason: str


async def llm_risk_judge_async(text: str) -> Dict[str, Any]:
    """使用DeepSeek V4 Flash对已有文本做标签归纳，不负责联网检索。"""
    if not settings.deepseek_ready:
        raise RuntimeError("未配置DEEPSEEK_API_KEY")
    prompt = (
        "仅依据输入文本判断风险标签、风险等级和原因。不得补充文本外事实。"
        "风险等级只能输出低、中、高之一；证据不足时在reason中说明，不得创造第四档。"
        "返回JSON对象，字段为label、level、reason。\n输入文本：\n" + (text or "")
    )
    response = await deepseek_client._call_json_model(
        model=settings.fast_model,
        system="你是企业风险文本分类器。",
        prompt=prompt,
        response_type=_RiskLabel,
        max_tokens=500,
        thinking=False,
    )
    return response.model_dump()


def llm_risk_judge(text: str) -> Dict[str, Any]:
    return asyncio.run(llm_risk_judge_async(text))
