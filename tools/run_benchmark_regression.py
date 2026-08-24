from __future__ import annotations

"""Offline evidence-scenario regression for the three-tier credit model.

No external workbook or company-name label is bundled or looked up.  These cases
exercise the deterministic inputs that actually drive a bank/enterprise decision.
"""

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_app.risk_engine import calculate_risk
from web_app.schemas import (
    AnnualFact,
    CompanyProfile,
    FieldEvidence,
    JudicialEvent,
    Relationship,
    ResearchExtraction,
    RiskSignal,
)


@dataclass(frozen=True)
class Scenario:
    name: str
    expected: str
    build: Callable[[], ResearchExtraction]


def base() -> ResearchExtraction:
    return ResearchExtraction(
        profile=CompanyProfile(
            company_name="场景测试企业",
            credit_code="91300000000000000X",
            legal_person="测试负责人",
            registered_address="测试地址",
            registered_capital="1亿元",
            company_status="存续",
            industry="制造业",
            controlling_shareholder="测试股东",
            actual_controller="测试控制人",
            field_evidence={"company_name": FieldEvidence(source_ids=["S1"], confidence=0.95)},
        ),
        annual_facts=[AnnualFact(year=2025, metric="审计意见", value="标准无保留意见", confidence=0.9)],
        overall_confidence=0.9,
        identity_verified=True,
    )


def normal_guarantee() -> ResearchExtraction:
    item = base()
    item.relationships.append(
        Relationship(
            source_name="场景测试企业",
            target_name="全资子公司",
            relation_type="对外担保",
            description="合并范围内正常经营担保，正常履行，未触发代偿责任",
            confidence=0.95,
            risk_level="medium",
            source_ids=["S2"],
        )
    )
    return item


def negated_and_resolved() -> ResearchExtraction:
    item = base()
    item.risk_signals.extend(
        [
            RiskSignal(category="偿债", title="未发生债务违约", description="不存在逾期债务", severity=95, confidence=0.95),
            RiskSignal(category="股权", title="股权冻结已解除", description="法院已解除冻结，事项已结案", severity=85, confidence=0.95),
        ]
    )
    return item


def material_litigation() -> ResearchExtraction:
    item = base()
    item.judicial_events.append(
        JudicialEvent(
            event_type="民事诉讼",
            title="重大合同纠纷诉讼",
            description="案件正在审理，尚未解决",
            amount=3000,
            amount_unit="万元",
            severity=72,
            source_ids=["S3"],
        )
    )
    return item


def debt_and_execution() -> ResearchExtraction:
    item = base()
    item.risk_signals.append(
        RiskSignal(category="偿债", title="债务违约", description="到期债务尚未清偿", severity=92, confidence=0.95, source_ids=["S4"])
    )
    item.judicial_events.append(
        JudicialEvent(event_type="执行", title="被执行及限制高消费", description="仍处于执行状态", severity=88, source_ids=["S5"])
    )
    return item


def financial_distress() -> ResearchExtraction:
    item = base()
    item.profile.industry = "光伏玻璃制造"
    item.annual_facts = [
        AnnualFact(year=2024, metric="归母净利润", value="亏损5.2亿元", confidence=0.95),
        AnnualFact(year=2025, metric="归母净利润", value="亏损7.1亿元", confidence=0.95),
        AnnualFact(year=2026, metric="资产负债率", value="76.10%", confidence=0.95),
        AnnualFact(year=2026, metric="货币资金", value="1.76亿元", confidence=0.95),
        AnnualFact(year=2026, metric="有息负债", value="66.13亿元", confidence=0.95),
    ]
    return item


def active_dishonesty() -> ResearchExtraction:
    item = base()
    item.risk_signals.append(
        RiskSignal(category="司法", title="列入失信名单", description="当前仍为失信被执行状态", severity=92, confidence=0.95, source_ids=["S6"])
    )
    return item


def active_equity_freeze() -> ResearchExtraction:
    item = base()
    item.risk_signals.append(
        RiskSignal(category="股权", title="股权冻结", description="控股股东股份仍处于冻结状态", severity=76, confidence=0.95, source_ids=["S7"])
    )
    return item


def adverse_audit_opinion() -> ResearchExtraction:
    item = base()
    item.annual_facts.append(
        AnnualFact(year=2025, metric="审计意见", value="会计师出具无法表示意见", confidence=0.98, source_ids=["S8"])
    )
    return item


SCENARIOS = [
    Scenario("正常担保不误判", "低风险", normal_guarantee),
    Scenario("否定与已解除事项降权", "低风险", negated_and_resolved),
    Scenario("重大有金额诉讼进入审慎审查", "中风险", material_litigation),
    Scenario("债务违约叠加执行", "高风险", debt_and_execution),
    Scenario("持续亏损高杠杆与现金缺口", "高风险", financial_distress),
    Scenario("单一失信不得被加权稀释", "高风险", active_dishonesty),
    Scenario("持续股权冻结进入中风险", "中风险", active_equity_freeze),
    Scenario("无法表示意见进入高风险", "高风险", adverse_audit_opinion),
]


def main() -> int:
    failures = []
    for scenario in SCENARIOS:
        result = calculate_risk(scenario.build())
        actual = result.risk_level
        score = result.risk_score
        ok = actual == scenario.expected
        print(f"{'PASS' if ok else 'FAIL'} | {scenario.name} | expected={scenario.expected} actual={actual} score={score}")
        if not ok:
            failures.append((scenario.name, scenario.expected, actual, score))

    if failures:
        print(f"FAILED: {len(failures)}/{len(SCENARIOS)} scenario(s)")
        return 1
    print(f"PASS: {len(SCENARIOS)}/{len(SCENARIOS)} evidence scenarios match the three-tier policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
