import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from typing import List, Dict, Any

ROOT = os.path.dirname(__file__)


def _load_module(module_name: str, file_name: str):
    path = os.path.join(ROOT, file_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


clean_business = _load_module("clean_business", "clean_business.py")
clean_annual = _load_module("clean_annual", "clean_annual.py")
clean_court = _load_module("clean_court", "clean_court.py")

standardize_business_record = clean_business.standardize_business_record
standardize_annual_record = clean_annual.standardize_annual_record
standardize_court_record = clean_court.standardize_court_record


def export_standard_data(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    cleaned = []
    for item in records:
        business = standardize_business_record(item.get("business", {}))
        annual = standardize_annual_record(item.get("annual", {}))
        court = standardize_court_record(item.get("court", {}))
        cleaned.append(
            {
                "company_name": business.get("company_name", ""),
                "credit_code": business.get("credit_code", ""),
                "legal_person": business.get("legal_person", ""),
                "registered_cap_num": business.get("registered_cap_num", 0),
                "status": business.get("status", "正常"),
                "annual_revenue": annual.get("annual_revenue", 0),
                "employee_count": annual.get("employee_count", 0),
                "asset_total": annual.get("asset_total", 0),
                "case_count": court.get("case_count", 0),
                "total_amount": court.get("total_amount", 0),
            }
        )
    output_dir = Path(__file__).resolve().parents[1] / "test_data"
    output_dir.mkdir(exist_ok=True)
    json_path = output_dir / "standard_company.json"
    json_path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    db_path = output_dir / "company.db"
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS company")
    conn.execute(
        "CREATE TABLE company (company_name TEXT, credit_code TEXT, legal_person TEXT, registered_cap_num REAL, status TEXT, annual_revenue REAL, employee_count INTEGER, asset_total REAL, case_count INTEGER, total_amount REAL)"
    )
    conn.executemany(
        "INSERT INTO company VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row["company_name"],
                row["credit_code"],
                row["legal_person"],
                row["registered_cap_num"],
                row["status"],
                row["annual_revenue"],
                row["employee_count"],
                row["asset_total"],
                row["case_count"],
                row["total_amount"],
            )
            for row in cleaned
        ],
    )
    conn.commit()
    conn.close()
    return {"count": len(cleaned), "json_path": str(json_path), "db_path": str(db_path)}
