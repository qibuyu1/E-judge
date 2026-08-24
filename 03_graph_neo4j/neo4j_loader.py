from __future__ import annotations

import os
import re
from typing import Any, Dict, List

try:
    from neo4j import GraphDatabase
except Exception:  # pragma: no cover
    GraphDatabase = None


def create_neo4j_connection_config() -> Dict[str, Any]:
    return {
        "uri": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        "user": os.getenv("NEO4J_USER", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", ""),
    }


def _relationship_type(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value or "RELATED_TO").upper()
    return cleaned or "RELATED_TO"


def import_graph_payload(graph_payload: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    if GraphDatabase is None and not dry_run:
        return {"status": "unavailable", "reason": "未安装neo4j驱动"}
    statements: List[Dict[str, Any]] = []
    for node in graph_payload.get("nodes", []):
        statements.append(
            {
                "cypher": "MERGE (n:Entity {id: $id}) SET n.name=$name, n.label=$label",
                "params": {"id": str(node.get("id", "")), "name": str(node.get("name", "")), "label": str(node.get("label", "Entity"))},
            }
        )
    for edge in graph_payload.get("edges", []):
        relation = _relationship_type(str(edge.get("type", "RELATED_TO")))
        statements.append(
            {
                "cypher": f"MATCH (a:Entity {{id:$source}}),(b:Entity {{id:$target}}) MERGE (a)-[r:{relation}]->(b) SET r.amount=$amount, r.share_ratio=$share_ratio",
                "params": {
                    "source": str(edge.get("source", "")),
                    "target": str(edge.get("target", "")),
                    "amount": edge.get("amount"),
                    "share_ratio": edge.get("share_ratio"),
                },
            }
        )
    if dry_run:
        return {"status": "dry_run", "statements": statements, "count": len(statements)}
    config = create_neo4j_connection_config()
    if not config["password"]:
        return {"status": "unavailable", "reason": "未配置NEO4J_PASSWORD"}
    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        with driver.session() as session:
            for statement in statements:
                session.run(statement["cypher"], **statement["params"])
        return {"status": "ok", "count": len(statements)}
    finally:
        driver.close()
