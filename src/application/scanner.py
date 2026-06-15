"""Performance scanner — Stage 1, fully deterministic.

Applies the playbook-aligned threshold rules to every BU (and optionally every
doctor) for the selected period and emits ranked Flags. No LLM involved, so
every flag is reproducible and auditable.
"""
from __future__ import annotations

from src.config import ScannerThresholds
from src.domain.models import Flag, Severity
from src.application.kpi_engine import KPIEngine


class PerformanceScanner:
    def __init__(self, engine: KPIEngine, thresholds: ScannerThresholds):
        self.engine = engine
        self.t = thresholds

    # ------------------------------------------------------------------ public
    def scan(self, year: int, month: int, bus: list[str] | None = None,
             include_doctor_level: bool = True) -> list[Flag]:
        bus = bus or self.engine.data.business_units()

        flags: list[Flag] = []
        for bu in bus:
            flags += self._scan_scope(year, month, bu=bu)
            if include_doctor_level:
                for doctor in self.engine.data.doctors(bu):
                    flags += self._scan_scope(year, month, bu=bu, doctor=doctor,
                                              doctor_level=True)
        flags.sort(key=lambda f: (f.severity.rank, -f.estimated_impact_egp))
        return flags

    # ------------------------------------------------------------------ rules
    def _scan_scope(self, year: int, month: int, bu: str,
                    doctor: str | None = None, doctor_level: bool = False) -> list[Flag]:
        snap = self.engine.kpi_snapshot(year, month, bu, doctor)
        cur, prev = snap["current"], snap["previous_month"]
        scope = {"year": year, "month": month, "bu": bu, "doctor": doctor}
        charge = cur.get("Charge per case", 0.0)
        act_rev = cur.get("Total Revenue", 0.0)
        flags: list[Flag] = []

        def add(kpi_id: str, kpi: str, scenario: str, severity: Severity,
                actual: float, reference: float, impact: float, detail: str) -> None:
            flags.append(Flag(kpi_id=kpi_id, kpi_name=kpi, scenario=scenario,
                              severity=severity, scope=scope, actual=actual,
                              reference=reference, variance=actual - reference,
                              estimated_impact_egp=max(impact, 0.0), detail=detail))

        # ── 1. Revenue below target (playbook: <90% → Critical) ──────────────
        tgt_rev = cur.get("Target Revenue", 0.0)
        if tgt_rev > 0:
            ach = act_rev / tgt_rev
            if ach < self.t.revenue_achievement_critical:
                add("OPD_KPI_003", "Total Revenue", "Actual Revenue below Target",
                    Severity.CRITICAL, act_rev, tgt_rev, tgt_rev - act_rev,
                    f"Achievement {ach:.1%} (threshold {self.t.revenue_achievement_critical:.0%}).")

        # ── 2. Revenue declining MoM (playbook: drop >10% → High) ────────────
        prev_rev = prev.get("Total Revenue", 0.0)
        if prev_rev > 0 and act_rev < prev_rev * (1 - self.t.revenue_mom_drop_high):
            add("OPD_KPI_003", "Total Revenue", "Revenue declining MoM",
                Severity.HIGH, act_rev, prev_rev, prev_rev - act_rev,
                f"MoM change {(act_rev / prev_rev - 1):+.1%}.")

        # ── 3. Cases below target (playbook: <85% → High) ────────────────────
        tgt_cases = cur.get("Target No. cases", 0.0)
        act_cases = cur.get("No. Cases", 0.0)
        if tgt_cases > 0:
            ach = act_cases / tgt_cases
            if ach < self.t.cases_achievement_high:
                add("OPD_KPI_008", "No. Cases", "Cases below target",
                    Severity.HIGH, act_cases, tgt_cases,
                    (tgt_cases - act_cases) * charge,
                    f"Achievement {ach:.1%}; shortfall {tgt_cases - act_cases:.0f} cases "
                    f"x EGP {charge:,.0f}/case.")

        # ── 4. Leakage losses material vs revenue (playbook: >10% → Critical) ─
        leak = cur.get("Total Leakage Revenue Losses", 0.0)
        if act_rev > 0 and leak / act_rev > self.t.leakage_pct_of_revenue_critical:
            add("OPD_KPI_006", "Total Leakage Revenue Losses", "Leakage losses increasing",
                Severity.CRITICAL, leak,
                act_rev * self.t.leakage_pct_of_revenue_critical, leak,
                f"Leakage is {leak / act_rev:.1%} of revenue "
                f"(threshold {self.t.leakage_pct_of_revenue_critical:.0%}).")

        # ── 5. Cancellation/modification losses (playbook: >8% → Critical) ────
        canc = cur.get("Total Losses Revenue_Cancellation_Modification", 0.0)
        if act_rev > 0 and canc / act_rev > self.t.cancellation_loss_pct_critical:
            add("OPD_KPI_023", "Total Losses Revenue_Cancellation_Modification",
                "Revenue losses increasing", Severity.CRITICAL, canc,
                act_rev * self.t.cancellation_loss_pct_critical, canc,
                f"Cancellation/modification losses are {canc / act_rev:.1%} of revenue "
                f"(threshold {self.t.cancellation_loss_pct_critical:.0%}).")

        # ── 6. No-show: absolute threshold (playbook: >25% → Critical) ────────
        ns = cur.get("No-Show %", 0.0)
        if ns > self.t.no_show_rate_critical:
            impact = cur.get("No. Booking", 0.0) * ns * charge
            add("OPD_KPI_024", "No-Show %", "No-show rate high",
                Severity.CRITICAL, ns, self.t.no_show_rate_critical, impact,
                f"No-show {ns:.1%} exceeds critical threshold {self.t.no_show_rate_critical:.0%}.")

        # ── 8. Charge per case eroding (playbook: <10% below avg → High) ──────
        cpc_base = self._trailing_mean("Charge per case", year, month, bu, doctor)
        if cpc_base and charge < cpc_base * (1 - self.t.charge_per_case_drop):
            add("OPD_KPI_010", "Charge per case", "Avg revenue per case low",
                Severity.HIGH, charge, cpc_base, (cpc_base - charge) * act_cases,
                f"Charge/case EGP {charge:,.0f} vs trailing avg EGP {cpc_base:,.0f}.")

        # BU-level-only rules (noisy / inapplicable per doctor)
        if not doctor_level:
            # ── 3b. Cases sudden MoM drop (playbook: Drop > 15% → Critical) ───
            prev_cases = prev.get("No. Cases", 0.0)
            if prev_cases > 0 and act_cases > 0:
                cases_mom = act_cases / prev_cases - 1
                if cases_mom < -self.t.cases_mom_drop_critical:
                    add("OPD_KPI_008", "No. Cases", "Sudden drop in patient volume",
                        Severity.CRITICAL, act_cases, prev_cases,
                        (prev_cases - act_cases) * charge,
                        f"Cases dropped {cases_mom:+.1%} MoM "
                        f"(threshold -{self.t.cases_mom_drop_critical:.0%}).")

            # ── 4b. Leakage losses increasing MoM (playbook: >15% → High) ─────
            prev_leak = prev.get("Total Leakage Revenue Losses", 0.0)
            if prev_leak > 0 and leak > prev_leak * (1 + self.t.leakage_mom_increase_high):
                add("OPD_KPI_006", "Total Leakage Revenue Losses", "Missed services increasing",
                    Severity.HIGH, leak, prev_leak, leak - prev_leak,
                    f"Leakage rose {(leak / prev_leak - 1):+.1%} MoM "
                    f"(threshold +{self.t.leakage_mom_increase_high:.0%}).")

            # ── 9. Digital conversion below target (playbook: <10pp → High) ───
            dcr = cur.get("Digital Actual CR%", 0.0)
            dcr_t = cur.get("Digital Target CR%", 0.0)
            if dcr_t and dcr < dcr_t - self.t.digital_cr_gap:
                # KB formula: No. Cases ÷ Leads → missed conversions × avg revenue
                _bookings = cur.get("No. Booking", 0.0)
                add("OPD_KPI_019", "Digital Actual CR%", "Actual CR below target",
                    Severity.HIGH, dcr, dcr_t,
                    _bookings * max(dcr_t - dcr, 0.0) * charge,
                    f"Actual CR {dcr:.1%} vs target {dcr_t:.1%} "
                    f"(gap {dcr_t - dcr:.1%} > threshold {self.t.digital_cr_gap:.0%}).")

            # ── 9b. Digital leads high but bookings low (CR < Planned CR → Critical)
            if dcr_t > 0 and dcr > 0 and dcr < dcr_t:
                _bk = cur.get("No. Booking", 0.0)
                if _bk > 0:
                    # Implied leads = bookings / actual_CR; shortfall = leads × CR_gap × charge
                    _implied_leads = _bk / dcr
                    add("OPD_KPI_011", "No. Booking", "Digital leads high but bookings low",
                        Severity.CRITICAL, dcr, dcr_t,
                        _implied_leads * (dcr_t - dcr) * charge,
                        f"Actual CR {dcr:.1%} below planned CR {dcr_t:.1%}; "
                        f"~{_implied_leads:.0f} leads but only {_bk:.0f} bookings. "
                        f"Analyze funnel leakage.")

            # ── 10. COE compliance (playbook: <80% → Critical) ─────────────────
            coe = cur.get("Actual COE Compliance %", 0.0)
            if coe and coe < self.t.coe_compliance_low:
                add("OPD_KPI_018", "Actual COE Compliance %", "COE compliance low",
                    Severity.CRITICAL, coe, self.t.coe_compliance_low, 0.0,
                    f"COE compliance {coe:.1%} below critical threshold {self.t.coe_compliance_low:.0%}.")

            # ── 10b. COE referrals declining (playbook: missed increase >15% → High)
            prev_coe = prev.get("Actual COE Compliance %", 0.0)
            if coe and prev_coe > 0 and coe < prev_coe * (1 - self.t.coe_compliance_drop_high):
                add("OPD_KPI_018", "Actual COE Compliance %", "Missed COE referrals increasing",
                    Severity.HIGH, coe, prev_coe, 0.0,
                    f"COE compliance dropped {(coe / prev_coe - 1):+.1%} MoM "
                    f"(threshold -{self.t.coe_compliance_drop_high:.0%}).")

            # ── 11. Service Leakage % (playbook: >8% → Critical) ───────────────
            svc_leak = cur.get("Service Leakage %", 0.0)
            if svc_leak > self.t.service_leakage_pct_critical:
                # KB formula: Missed Services ÷ Planned Services → act_rev × leakage %
                add("OPD_KPI_014", "Service Leakage %", "Leakage percentage high",
                    Severity.CRITICAL, svc_leak, self.t.service_leakage_pct_critical,
                    act_rev * svc_leak,
                    f"Service leakage {svc_leak:.1%} exceeds critical threshold "
                    f"{self.t.service_leakage_pct_critical:.0%}.")

            # ── 12. Patient Retention % (playbook: <60% → High) ────────────────
            ret = cur.get("Patient Retention %", 0.0)
            if ret and ret < self.t.patient_retention_low:
                # KB formula: Returning Patients ÷ Total Patients → shortfall × avg revenue
                add("OPD_KPI_016", "Patient Retention %", "Retention below target",
                    Severity.HIGH, ret, self.t.patient_retention_low,
                    act_cases * max(self.t.patient_retention_low - ret, 0.0) * charge,
                    f"Patient retention {ret:.1%} below threshold {self.t.patient_retention_low:.0%}.")

            # ── 12b. No-show affecting retention (playbook: No-show >25% → Medium) ─
            if ns > self.t.no_show_rate_critical:
                add("OPD_KPI_016", "Patient Retention %", "No-show affecting retention",
                    Severity.MEDIUM, ns, self.t.no_show_rate_critical,
                    act_cases * ns * charge,
                    f"No-show rate {ns:.1%} exceeds {self.t.no_show_rate_critical:.0%}; "
                    f"high no-shows likely reducing patient retention. "
                    f"Review reminder and engagement process.")

            # ── 13. Cross Referral % (playbook: <10% → Medium) ─────────────────
            xref = cur.get("Cross Referral %", 0.0)
            _dcr_guard = cur.get("Digital Actual CR%", 0.0)
            if xref and xref < self.t.cross_referral_low and _dcr_guard < 0.10:
                add("OPD_KPI_015", "Cross Referral %", "Cross referral rate low",
                    Severity.MEDIUM, xref, self.t.cross_referral_low, 0.0,
                    f"Cross referral {xref:.1%} below threshold {self.t.cross_referral_low:.0%}.")

            # ── 13b. Cross Referral % declining (playbook: Drop >20% → High) ───
            prev_xref = prev.get("Cross Referral %", 0.0)
            if xref and prev_xref > 0 and xref < prev_xref * (1 - self.t.cross_referral_drop_high):
                add("OPD_KPI_015", "Cross Referral %", "Referral decline in COE pathways",
                    Severity.HIGH, xref, prev_xref, 0.0,
                    f"Cross referral dropped {(xref / prev_xref - 1):+.1%} MoM "
                    f"(threshold -{self.t.cross_referral_drop_high:.0%}).")

            # ── 14. No. follow-up visits declining MoM (playbook: >15% drop → Medium)
            fuv = cur.get("No. follow-up visits", 0.0)
            prev_fuv = prev.get("No. follow-up visits", 0.0)
            if prev_fuv > 0 and fuv < prev_fuv * (1 - self.t.follow_up_drop):
                # KB formula: Follow-up Visits × Avg Revenue → lost visits × charge
                add("OPD_KPI_013", "No. follow-up visits", "Follow-up visits declining",
                    Severity.MEDIUM, fuv, prev_fuv,
                    (prev_fuv - fuv) * charge,
                    f"Follow-up visits dropped {(fuv / prev_fuv - 1):+.1%} MoM "
                    f"(threshold -{self.t.follow_up_drop:.0%}).")

            # ── 14b. Follow-up engagement (playbook: <70% compliance → High) ───
            if act_cases > 0 and fuv > 0:
                fuv_ratio = fuv / act_cases
                if fuv_ratio < self.t.follow_up_compliance_low:
                    # KB formula: Follow-up Visits × Avg Revenue → missing visits × charge
                    add("OPD_KPI_013", "No. follow-up visits", "Low doctor follow-up engagement",
                        Severity.HIGH, fuv_ratio, self.t.follow_up_compliance_low,
                        max(act_cases * self.t.follow_up_compliance_low - fuv, 0.0) * charge,
                        f"Follow-up visits are {fuv_ratio:.1%} of cases "
                        f"(threshold {self.t.follow_up_compliance_low:.0%}).")

            # ── 15. Booking slot utilisation (playbook: <70% → Medium) ─────────
            planned = cur.get("No. Planned booking Slots", 0.0)
            if planned > 0 and act_cases > 0:
                utilisation = act_cases / planned
                if utilisation < self.t.slot_utilization_low:
                    # KB formula: Planned Slots × Utilization % → unused capacity × charge
                    add("OPD_KPI_012", "No. Planned booking Slots", "Slot utilization low",
                        Severity.MEDIUM, utilisation, self.t.slot_utilization_low,
                        max(planned * self.t.slot_utilization_low - act_cases, 0.0) * charge,
                        f"Slot utilization {utilisation:.1%} (cases/planned) below "
                        f"threshold {self.t.slot_utilization_low:.0%}.")

            # ── 15b. Booking below expected (playbook: <85% of planned slots → High)
            _bookings = cur.get("No. Booking", 0.0)
            if planned > 0 and _bookings > 0:
                bk_ach = _bookings / planned
                if bk_ach < self.t.booking_achievement_high:
                    add("OPD_KPI_011", "No. Booking", "Booking below expected",
                        Severity.HIGH, _bookings, planned,
                        max(planned * self.t.booking_achievement_high - _bookings, 0.0) * charge,
                        f"Booking achievement {bk_ach:.1%} vs planned slots "
                        f"(threshold {self.t.booking_achievement_high:.0%}). "
                        f"Review lead quality and call center response.")

            # ── 15c. Insufficient capacity (planned slots < actual bookings → High) ──
            _act_bk = cur.get("No. Booking", 0.0)
            if planned > 0 and _act_bk > planned:
                add("OPD_KPI_012", "No. Planned booking Slots", "Insufficient capacity",
                    Severity.HIGH, planned, _act_bk,
                    (_act_bk - planned) * charge,
                    f"Planned slots {planned:.0f} below actual bookings {_act_bk:.0f}; "
                    f"demand exceeds capacity by {_act_bk - planned:.0f} slots. "
                    f"Compare demand vs available slots.")

            # ── 16. No. Missed Opportunity increasing MoM (playbook: >10% → High)
            missed = cur.get("No. Missed Opportunity", 0.0)
            prev_missed = prev.get("No. Missed Opportunity", 0.0)
            if prev_missed > 0 and missed > prev_missed * (1 + self.t.missed_opportunity_increase):
                add("OPD_KPI_021", "No. Missed Opportunity", "Missed opportunities increasing",
                    Severity.HIGH, missed, prev_missed, missed * charge,
                    f"Missed opportunities rose {(missed / prev_missed - 1):+.1%} MoM "
                    f"(threshold +{self.t.missed_opportunity_increase:.0%}).")

            # ── 17. No. Cancelled Clinics (playbook: >5% of planned slots → High)
            cancelled = cur.get("No. Cancelled Clinics", 0.0)
            if planned > 0 and cancelled / planned > self.t.cancelled_clinics_pct:
                add("OPD_KPI_022", "No. Cancelled Clinics", "Clinic cancellations increasing",
                    Severity.HIGH, cancelled, planned * self.t.cancelled_clinics_pct,
                    cancelled * charge,
                    f"Cancelled clinics {cancelled:.0f} = {cancelled / planned:.1%} of planned "
                    f"slots (threshold {self.t.cancelled_clinics_pct:.0%}).")

            # ── 17b. High revenue impact from cancellations (Revenue loss > threshold → Critical)
            if act_rev > 0 and canc > 0 and canc / act_rev > self.t.cancellation_loss_pct_critical:
                add("OPD_KPI_022", "No. Cancelled Clinics", "High revenue impact from cancellations",
                    Severity.CRITICAL, cancelled, act_rev * self.t.cancellation_loss_pct_critical,
                    canc,
                    f"Cancellation losses EGP {canc:,.0f} ({canc / act_rev:.1%} of revenue) exceed "
                    f"threshold {self.t.cancellation_loss_pct_critical:.0%}. "
                    f"Analyze affected bookings and services.")

            # ── 18. Cash Revenue declining MoM (playbook: drop >10% → Medium) ──
            cash = cur.get("Cash Revenue", 0.0)
            prev_cash = prev.get("Cash Revenue", 0.0)
            if prev_cash > 0 and cash < prev_cash * (1 - self.t.cash_revenue_drop_medium):
                add("OPD_KPI_005", "Cash Revenue", "Cash revenue declining",
                    Severity.MEDIUM, cash, prev_cash, prev_cash - cash,
                    f"Cash revenue MoM change {(cash / prev_cash - 1):+.1%} "
                    f"(threshold -{self.t.cash_revenue_drop_medium:.0%}).")

        # Doctor-level-only rules
        if doctor_level:
            # ── 19. Doctor PMS % (playbook: <75% → High; <80% → Medium) ────────
            pms = cur.get("Doctor PMS %", 0.0)
            if pms and pms < self.t.doctor_pms_high:
                add("OPD_KPI_007", "Doctor PMS %", "Compliance metrics low",
                    Severity.HIGH, pms, self.t.doctor_pms_high, 0.0,
                    f"Doctor PMS {pms:.1%} below compliance threshold "
                    f"{self.t.doctor_pms_high:.0%}.")
            elif pms and pms < self.t.doctor_pms_medium:
                add("OPD_KPI_007", "Doctor PMS %", "PMS score below target",
                    Severity.MEDIUM, pms, self.t.doctor_pms_medium, 0.0,
                    f"Doctor PMS {pms:.1%} below target {self.t.doctor_pms_medium:.0%}.")

        return flags

    def _trailing_mean(self, kpi: str, year: int, month: int,
                       bu: str | None, doctor: str | None, n: int = 12) -> float | None:
        py, pm = self.engine._prev_period(year, month)
        trend = self.engine.kpi_trend(kpi, n_months=n, bu=bu, doctor=doctor,
                                      end_year=py, end_month=pm)
        return trend.get("mean")
