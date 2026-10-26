from __future__ import annotations

import re
from typing import Any, Dict, List


def _safe_id(value: str, index: int) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value)[:24]
    return f"node_{index}_{cleaned or index}"


def build_graph_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """从真实结构化字段构图；无关系时只保留目标企业节点。"""
    company_name = str(record.get("company_name") or record.get("credit_code") or "待核验企业")
    credit_code = str(record.get("credit_code") or company_name)
    nodes: List[Dict[str, Any]] = [
        {"id": credit_code, "label": "Company", "name": company_name, "status": record.get("status")}
    ]
    edges: List[Dict[str, Any]] = []
    node_ids = {company_name: credit_code}

    legal_person = record.get("legal_person")
    if legal_person:
        person_id = f"person:{legal_person}"
        nodes.append({"id": person_id, "label": "Person", "name": legal_person})
        edges.append({"source": credit_code, "target": person_id, "type": "LEGAL_REPRESENTATIVE"})

    for index, relation in enumerate(record.get("relationships", []) or [], start=1):
        target = str(relation.get("target_name") or "").strip()
        if not target:
            continue
        target_id = node_ids.get(target)
        if target_id is None:
            target_id = _safe_id(target, index)
            node_ids[target] = target_id
            nodes.append({"id": target_id, "label": "Company", "name": target})
        edges.append(
            {
                "source": credit_code,
                "target": target_id,
                "type": str(relation.get("relation_type") or "RELATED_TO").upper(),
                "share_ratio": relation.get("share_ratio"),
                "amount": relation.get("amount"),
            }
        )
    return {"nodes": nodes, "edges": edges}
