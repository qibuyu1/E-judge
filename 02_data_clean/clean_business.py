import re
from typing import Dict, Any


def standardize_business_record(record: Dict[str, Any]) -> Dict[str, Any]:
    registered_cap = record.get("registered_cap", "") or ""
    number_match = re.search(r"(\d+(?:\.\d+)?)", str(registered_cap))
    cap_num = float(number_match.group(1)) if number_match else 0.0
    status = str(record.get("status", "正常")).strip() or "正常"
    return {
        "company_name": record.get("company_name", ""),
        "credit_code": record.get("credit_code", ""),
        "legal_person": record.get("legal_person", ""),
        "registered_cap": registered_cap,
        "registered_cap_num": cap_num,
        "status": status,
        "industry": record.get("industry", ""),
        "found_date": record.get("found_date", ""),
        "shareholders": record.get("shareholders", []),
    }
