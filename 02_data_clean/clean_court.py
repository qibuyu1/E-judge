from typing import Dict, Any


def standardize_court_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "company_name": record.get("company_name", ""),
        "case_count": int(record.get("case_count", 0) or 0),
        "total_amount": float(record.get("total_amount", 0) or 0),
        "cases": record.get("cases", []),
    }
