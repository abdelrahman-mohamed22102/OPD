"""Central configuration for the OPD Financial Outcome Agent.

All tunables live here so the application layer stays free of magic numbers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


@dataclass(frozen=True)
class LLMConfig:
    """Model routing: a capable model for diagnosis/narrative, a fast one for routing.

    Uses Groq's free tier (console.groq.com) running open-source models on LPU
    hardware.  Free tier: 30 RPM, 14 400 RPD, 6 000 TPM per model.
    """

    api_key_env: str = "GROQ_API_KEY"
    investigator_model: str = "llama-3.3-70b-versatile"   # best quality + tool use
    narrator_model: str = "llama-3.3-70b-versatile"
    router_model: str = "llama-3.1-8b-instant"            # fast, cheap triage
    max_tool_iterations: int = 7
    max_tokens: int = 4096

    @property
    def api_key(self) -> str | None:
        # Streamlit Cloud secrets take priority; fall back to environment variable.
        try:
            import streamlit as st
            val = st.secrets.get(self.api_key_env)
            if val:
                return str(val)
        except Exception:
            pass
        return os.environ.get(self.api_key_env)


@dataclass(frozen=True)
class ScannerThresholds:
    """Deterministic flagging rules. Aligned with adx_kpi_investigation_playbook
    where the playbook states explicit thresholds; defaults cover the rest."""

    # --- Effect KPIs ---
    revenue_achievement_critical: float = 0.90   # playbook: < 90% of target -> Critical
    revenue_mom_drop_high: float = 0.10          # playbook: MoM decrease > 10% -> High
    cash_revenue_drop_medium: float = 0.10       # playbook: Cash revenue drop > 10% -> Medium

    # --- Volume KPIs ---
    cases_achievement_high: float = 0.85         # playbook: < 85% of target -> High
    booking_achievement_high: float = 0.85       # playbook: No. Booking < 85% of planned slots -> High
    no_show_rate_critical: float = 0.25          # playbook: No-show > 25% -> Critical
    slot_utilization_low: float = 0.70           # playbook: Cases/Planned slots < 70% -> Medium
    cases_mom_drop_critical: float = 0.15        # playbook: Sudden drop > 15% MoM -> Critical (row 3)

    # --- Revenue quality KPIs ---
    charge_per_case_drop: float = 0.10           # playbook: below trailing avg by > 10% -> High
    leakage_pct_of_revenue_critical: float = 0.10  # playbook: > 10% of revenue -> Critical
    leakage_mom_increase_high: float = 0.15       # playbook: Missed services > 15% MoM increase -> High (row 10)
    service_leakage_pct_critical: float = 0.08   # playbook: Service Leakage % > 8% -> Critical
    cancellation_loss_pct_critical: float = 0.08  # playbook: > 8% of revenue -> Critical
    missed_opportunity_increase: float = 0.10    # playbook: > 10% MoM increase -> High
    cancelled_clinics_pct: float = 0.05          # playbook: > 5% of planned slots -> High

    # --- Quality / compliance KPIs ---
    coe_compliance_low: float = 0.80             # playbook: < 80% -> Critical
    digital_cr_gap: float = 0.10                 # playbook: actual CR < target by > 10pp -> High
    patient_retention_low: float = 0.60          # playbook: < 60% -> High
    cross_referral_low: float = 0.10             # playbook: < 10% -> Medium
    cross_referral_drop_high: float = 0.20       # playbook: Drop > 20% MoM -> High (row 22)
    follow_up_drop: float = 0.15                 # playbook: drop > 15% MoM -> Medium
    follow_up_compliance_low: float = 0.70       # playbook: follow-up/cases < 70% -> High (row 20)
    coe_compliance_drop_high: float = 0.15       # playbook: missed COE referrals increase > 15% -> High (row 28)
    doctor_pms_high: float = 0.75                # playbook: Doctor PMS < 75% -> High
    doctor_pms_medium: float = 0.80              # playbook: Doctor PMS < 80% -> Medium


@dataclass(frozen=True)
class AppConfig:
    dataset_path: Path = DATA_DIR / "OPD_dataset.xlsx"
    knowledge_base_path: Path = DATA_DIR / "Knowledge_base.xlsx"
    llm: LLMConfig = field(default_factory=LLMConfig)
    thresholds: ScannerThresholds = field(default_factory=ScannerThresholds)


# The relationship map references some KPIs under names that differ slightly
# from dataset columns. Resolve them here; anything unresolved is treated as
# NOT measurable in the dataset and routed to manual investigation.
KPI_NAME_ALIASES: dict[str, str] = {
    "No-show %": "No-Show %",
    "COE Compliance %": "Actual COE Compliance %",
    "Follow-up Visits": "No. follow-up visits",
    "No. follow-up visits": "No. follow-up visits",
    "Retention %": "Patient Retention %",
    "Digital CR%": "Digital Actual CR%",
    "Digital Actual CR%": "Digital Actual CR%",
    "Leakage %": "Service Leakage %",
}

CONFIG = AppConfig()
