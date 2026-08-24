from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

from .config import settings


def _report_is_current(report: Dict[str, Any]) -> bool:
    summary = report.get("summary") or {}
    return str(summary.get("scoring_version") or "") == str(settings.cache_schema_version)

def _sanitize_company_dict(report: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .profile_quality import clean_profile_value
        company = report.get("company") or {}
        mapping = {
            "name":"company_name", "short_name":"short_name", "stock_code":"stock_code",
            "credit_code":"credit_code", "legal_person":"legal_person",
            "registered_address":"registered_address", "registered_capital":"registered_capital",
            "company_status":"company_status", "established_date":"established_date",
            "industry":"industry", "controlling_shareholder":"controlling_shareholder",
            "actual_controller":"actual_controller", "website":"website",
        }
        for key, field in mapping.items():
            if key in company:
                company[key] = clean_profile_value(field, company.get(key)) or None
        report["company"] = company
    except Exception:
        pass
    return report


class ResearchCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reports (
                    cache_key TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_history (
                    report_id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_history_created ON report_history(created_at DESC)")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            previous = row["value"] if row else None
            if previous != settings.cache_schema_version:
                connection.execute("DELETE FROM reports")
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (settings.cache_schema_version,),
                )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_reports_expires ON reports(expires_at)")

    @staticmethod
    def _key(query: str) -> str:
        normalized = " ".join(query.strip().lower().split())
        material = f"{settings.cache_schema_version}|{normalized}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        key = self._key(query)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT report_json, expires_at FROM reports WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                connection.execute("DELETE FROM reports WHERE cache_key = ?", (key,))
                return None
            report = _sanitize_company_dict(json.loads(row["report_json"]))
            if not _report_is_current(report):
                connection.execute("DELETE FROM reports WHERE cache_key = ?", (key,))
                return None
            model_label = str(report.get("model", {}).get("model_label") or "")
            if not model_label.startswith("DeepSeek V4"):
                connection.execute("DELETE FROM reports WHERE cache_key = ?", (key,))
                return None
            return report

    def set(self, query: str, report: Dict[str, Any]) -> None:
        key = self._key(query)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=settings.cache_ttl_hours)
        payload = json.dumps(report, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reports(cache_key, query, report_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    query=excluded.query,
                    report_json=excluded.report_json,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                (key, query, payload, now.isoformat(), expires.isoformat()),
            )

    def archive(self, report: Dict[str, Any]) -> None:
        report_id = str(report.get("report_id") or "").strip()
        if not report_id:
            return
        query = str(report.get("query") or "").strip()
        company = report.get("company") or {}
        company_name = str(company.get("name") or company.get("short_name") or query or "未命名企业").strip()
        created_at = str((report.get("summary") or {}).get("analysis_completed_at") or datetime.now(timezone.utc).isoformat())
        payload = json.dumps(report, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO report_history(report_id, query, company_name, report_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    query=excluded.query,
                    company_name=excluded.company_name,
                    report_json=excluded.report_json,
                    created_at=excluded.created_at
                """,
                (report_id, query, company_name, payload, created_at),
            )

    def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM report_history WHERE report_id = ?", (report_id,)
            ).fetchone()
            if not row:
                return None
            report = _sanitize_company_dict(json.loads(row["report_json"]))
            return report if _report_is_current(report) else None

    def list_history(self, limit: int = 30) -> list[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT report_id, query, company_name, created_at, report_json FROM report_history ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        items: list[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["report_json"])
            except Exception:
                payload = {}
            payload = _sanitize_company_dict(payload)
            if not _report_is_current(payload):
                continue
            summary = payload.get("summary") or {}
            company = payload.get("company") or {}
            items.append({
                "report_id": row["report_id"],
                "query": row["query"],
                "company_name": row["company_name"],
                "short_name": company.get("short_name"),
                "stock_code": company.get("stock_code"),
                "credit_code": company.get("credit_code"),
                "risk_level": summary.get("risk_level"),
                "risk_score": summary.get("risk_score"),
                "created_at": row["created_at"],
            })
        return items

    def delete_history(self, report_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM report_history WHERE report_id = ?", (report_id,))
            return bool(cursor.rowcount)

    def purge_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM reports WHERE expires_at <= ?", (now,))
            return int(cursor.rowcount or 0)

    def clear(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM reports")
            history_cursor = connection.execute("DELETE FROM report_history")
            return int(cursor.rowcount or 0) + int(history_cursor.rowcount or 0)


research_cache = ResearchCache(settings.cache_db_path)
