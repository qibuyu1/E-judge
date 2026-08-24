from typing import Dict, Any


def standardize_annual_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "company_name": record.get("company_name", ""),
        "credit_code": record.get("credit_code", ""),
        "annual_revenue": float(record.get("annual_revenue", 0) or 0),
        "employee_count": int(record.get("employee_count", 0) or 0),
        "asset_total": float(record.get("asset_total", 0) or 0),
        "annual_status": str(record.get("annual_status", "正常")),
    }
