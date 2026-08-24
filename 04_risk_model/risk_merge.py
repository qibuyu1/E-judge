from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载{filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_rule = _load("chain_risk_rule", "risk_rule_score.py")
_llm = _load("chain_llm_judge", "llm_risk_judge.py")


def calculate_risk(data: Dict[str, Any]) -> Dict[str, Any]:
    rule = _rule.calculate_rule_score(data)
    llm = _llm.llm_risk_judge(data.get("text", "")) if data.get("text") else {
        "label": "未执行语义归纳",
        "level": "未定",
        "reason": "输入未包含可归纳文本",
    }
    # Compatibility module follows the same public three-tier policy as web_app.
    extra = 25 if "高" in str(llm["level"]) else 10 if "中" in str(llm["level"]) else 0
    score = min(100, float(rule["score"]) + extra)
    level = "高" if score >= 60 else "中" if score >= 40 else "低"
    return {
        "company_name": data.get("company_name", ""),
        "risk_score": round(score, 2),
        "risk_level": level,
        "label": llm["label"],
        "reason": llm["reason"],
        "rule_reasons": rule["reasons"],
    }
