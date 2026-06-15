"""Domain layer: pure entities shared across the application.

No pandas, no Anthropic, no Streamlit imports here — only the language of the
business problem (KPIs, flags, findings, actions, briefs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"

    @property
    def rank(self) -> int:
        return {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}[self.value]


class KPILayer(str, Enum):
    EFFECT = "Effect"
    DRIVER = "Driver"
    CAUSE = "Cause"


@dataclass(frozen=True)
class KPIDefinition:
    """One row of adx_kpi_knowledge_map, enriched with formula/scope sheets."""

    kpi_id: str
    name: str
    layer: KPILayer
    owner_role: str
    function_owner: str
    business_question: str
    financial_impact_formula: str
    primary_driver: str
    secondary_driver: str
    investigation_steps: tuple[str, ...]
    action_owner: str
    escalation_level: str
    recommended_action: str
    
    ############################################
    formula_logic: str = ""
    lowest_granularity: str = ""
    not_available_message: str = ""


@dataclass(frozen=True)
class KPIRelationship:
    """One edge of adx_kpi_relationship_map (parent -> child)."""

    parent: str
    child: str
    relationship_type: str   # Driver | Cause | Variance | Influence
    weight: str              # High | Medium | Low
    investigation_order: int
    
    ############################################
    measurable: bool         # child resolvable to a dataset column?
    resolved_column: str | None = None


@dataclass(frozen=True)
class PlaybookRule:
    """One row of adx_kpi_investigation_playbook."""

    kpi_id: str
    kpi_name: str
    scenario: str
    threshold: str
    severity: Severity
    root_cause_focus: str
    recommended_investigation: str
    recommended_action: str
    escalation: str


@dataclass
class Flag:
    """Output of the deterministic performance scanner."""

    kpi_id: str
    kpi_name: str
    scenario: str
    severity: Severity
    scope: dict          # {"year":..., "month":..., "bu":..., "doctor":...}
    actual: float
    reference: float     # target / baseline being compared against
    variance: float
    estimated_impact_egp: float
    detail: str

    def key(self) -> str:
        bu = self.scope.get("bu") or "ALL"
        doc = self.scope.get("doctor") or "ALL"
        return f"{self.kpi_id}|{bu}|{doc}|{self.scenario}"


@dataclass
class CauseFinding:
    """One confirmed (or ruled-out) hypothesis from the investigator."""

    kpi: str
    verdict: str                 # confirmed | ruled_out | needs_manual_check
    evidence: str
    estimated_impact_egp: float | None = None


@dataclass
class Investigation:
    """The investigator agent's structured output for one Flag."""

    flag_key: str
    summary: str
    confirmed_causes: list[CauseFinding] = field(default_factory=list)
    ruled_out: list[CauseFinding] = field(default_factory=list)
    manual_checks: list[CauseFinding] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)   # tool-call audit trail


@dataclass
class ActionItem:
    """Leadership-ready action mapped from the knowledge base."""

    cause_kpi: str
    affected_scope: str
    recommended_action: str
    action_owner: str
    escalation_level: str
    severity: Severity
    estimated_impact_egp: float
    evidence: str


@dataclass
class ExecutiveBrief:
    period_label: str
    markdown: str
    flags: list[Flag]
    investigations: list[Investigation]
    actions: list[ActionItem]
