"""OPD Financial Outcome Agent — Streamlit UI (presentation layer only).

All business logic lives in src/; this file renders results and collects input.
Run:  streamlit run app.py
"""
from __future__ import annotations

import calendar
import re

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.application.orchestrator import Orchestrator
from src.config import CONFIG

st.set_page_config(page_title="OPD Financial Outcome Agent", page_icon="🏥",
                   layout="wide")



SEVERITY_COLORS = {"Critical": "#A32D2D", "High": "#BA7517",
                   "Medium": "#185FA5", "Low": "#5F5E5A"}


# --------------------------------------------------------------------- setup
@st.cache_resource(show_spinner="Loading dataset and knowledge base...")
def get_orchestrator_base() -> Orchestrator:
    """Load dataset + KB once (expensive). LLM key is set separately."""
    return Orchestrator(CONFIG, api_key=None)


def severity_badge(sev: str) -> str:
    return (f"<span style='background:{SEVERITY_COLORS.get(sev, '#5F5E5A')};color:white;"
            f"padding:2px 10px;border-radius:10px;font-size:0.8em'>{sev}</span>")


with st.sidebar:
    st.title("🏥 OPD Financial Outcome Agent")

    orch = get_orchestrator_base()

    periods = orch.data.periods()
    years = sorted({y for y, _ in periods})
    year = st.selectbox("Year", years, index=len(years) - 1)
    months = [m for y, m in periods if y == year]
    month = st.selectbox("Month", months, index=len(months) - 1,
                         format_func=lambda m: calendar.month_name[m])

    include_doctor = st.toggle("Doctor-level scanning", value=True)

    st.divider()
    llm_ok = orch.llm.available
    st.markdown(f"**LLM:** {'🟢 Groq / ' + CONFIG.llm.investigator_model if llm_ok else '🔴 not configured'}")

period_label = f"{calendar.month_name[month]} {year}"
st.title(f"OPD Financial Outcome — {period_label}")

tab_scan, tab_actions, tab_brief, tab_chat = st.tabs(
    ["📊 Performance scanner", "✅ Action plan", "📄 Executive brief", "💬 Ask the agent"])


# ---------------------------------------------------------------- tab: scan
with tab_scan:
    summary = orch.bu_summary(year, month)
    cols = st.columns(len(summary))
    for col, b in zip(cols, summary):
        ach = b["achievement"] or 0
        col.metric(f"{b['bu']} revenue", f"EGP {b['total_revenue']:,.0f}",
                   f"{ach - 1:+.1%} vs target",
                   delta_color="inverse")
        col.caption(f"Cases {b['cases']:,.0f}/{b['target_cases']:,.0f} · "
                    f"Leakage EGP {b['leakage']:,.0f} · No-show {b['no_show']:.1%}")

    trend_kpi = st.selectbox("Trend", ["Total Revenue", "No. Cases", "Charge per case",
                                       "Total Leakage Revenue Losses", "No-Show %",
                                       "Patient Retention %", "Digital Actual CR%"])
    fig = go.Figure()
    for bu in orch.data.business_units():
        t = orch.engine.kpi_trend(trend_kpi, n_months=18, bu=bu,
                                  end_year=year, end_month=month)
        xs = [f"{p['year']}-{p['month']:02d}" for p in t["series"]]
        fig.add_trace(go.Scatter(x=xs, y=[p["value"] for p in t["series"]],
                                 mode="lines+markers", name=bu))
    fig.update_layout(height=340, margin=dict(t=24, b=0), yaxis_title=trend_kpi)
    st.plotly_chart(fig, use_container_width=True)

    # Auto-scan on first load or when period/doctor toggle changes.
    scan_key = (year, month, include_doctor)
    if st.session_state.get("flags_period") != scan_key:
        with st.spinner("Scanning all BUs and doctors..."):
            st.session_state["flags"] = orch.scan(year, month,
                                                  include_doctor_level=include_doctor)
            st.session_state["flags_period"] = scan_key
            st.session_state.pop("investigations", None)
            st.session_state.pop("brief", None)

    if st.button("🔄 Re-run scan", type="secondary"):
        with st.spinner("Scanning all BUs and doctors..."):
            st.session_state["flags"] = orch.scan(year, month,
                                                  include_doctor_level=include_doctor)
            st.session_state["flags_period"] = scan_key
            st.session_state.pop("investigations", None)
            st.session_state.pop("brief", None)

    flags = st.session_state.get("flags", [])
    if flags and st.session_state.get("flags_period") == scan_key:
        st.subheader(f"{len(flags)} flags — ranked by severity, then EGP impact")
        df = pd.DataFrame([{
            "Severity": f.severity.value, "KPI": f.kpi_name, "Scenario": f.scenario,
            "BU": f.scope.get("bu") or "ALL", "Doctor": f.scope.get("doctor") or "—",
            "Actual": round(f.actual, 2), "Reference": round(f.reference, 2),
            "Est. impact (EGP)": round(f.estimated_impact_egp, 0), "Detail": f.detail,
        } for f in flags])
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={"Est. impact (EGP)": st.column_config.NumberColumn(
                         format="EGP %,.0f")})

        impact = (df[df["Est. impact (EGP)"] > 0]
                  .groupby("KPI")["Est. impact (EGP)"].sum()
                  .sort_values(ascending=False).head(8))
        if not impact.empty:
            st.plotly_chart(
                px.bar(impact, orientation="h",
                       labels={"value": "Estimated EGP impact", "KPI": ""},
                       title="Indicators decreasing revenue (estimated EGP impact)")
                .update_layout(height=320, showlegend=False, margin=dict(t=40, b=0)),
                use_container_width=True)



# -------------------------------------------------------------- tab: actions
with tab_actions:
    flags = st.session_state.get("flags", [])
    if not flags:
        st.info("Run the performance scan first.")
    else:
        actions = orch.recommender.recommend(
            flags, st.session_state.get("investigations", {}))
        st.subheader("Leadership action plan")
        st.caption("Actions, owners and escalation levels come verbatim from the "
                   "KPI knowledge base — never generated by the model.")

        all_sevs = ["Critical", "High", "Medium", "Low"]
        present_sevs = [s for s in all_sevs if any(a.severity.value == s for a in actions)]
        sel_sevs = st.multiselect("Severity", present_sevs, default=present_sevs,
                                  key="action_sev_filter")

        adf = pd.DataFrame([{
            "Severity": a.severity.value,
            "BU": a.affected_scope,
            "Cause KPI": a.cause_kpi,
            "Recommended action": a.recommended_action,
            "Action owner": a.action_owner,
            "Escalation": a.escalation_level,
            "Est. impact (EGP)": round(a.estimated_impact_egp, 0),
            "Detail": a.evidence,
        } for a in actions if a.severity.value in sel_sevs])
        st.dataframe(adf, use_container_width=True, hide_index=True,
                     column_config={"Est. impact (EGP)": st.column_config.NumberColumn(
                         format="EGP %,.0f")})
        st.download_button("Download action plan (CSV)",
                           adf.to_csv(index=False).encode("utf-8-sig"),
                           f"opd_action_plan_{year}_{month:02d}.csv", "text/csv")


# ---------------------------------------------------------------- tab: brief
with tab_brief:
    if st.button("📄 Generate executive brief", type="primary"):
        with st.spinner("Running full pipeline (scan → investigate → actions → narrate)..."):
            try:
                st.session_state["brief"] = orch.run_full_pipeline(
                    year, month, max_investigations=3)
            except Exception as exc:
                if "invalid" in str(exc).lower() or "authentication" in str(exc).lower():
                    st.error("🔑 " + str(exc))
                else:
                    st.error(f"LLM error: {exc}")
    brief = st.session_state.get("brief")
    if brief:
        md = brief.markdown
        m4 = re.search(r"^##\s*4\.", md, re.MULTILINE | re.IGNORECASE)
        m5 = re.search(r"^##\s*5\.", md, re.MULTILINE | re.IGNORECASE)

        # Sections 1–3
        st.markdown(md[: m4.start()] if m4 else md)

        # Section 4 — deterministic Streamlit table (Critical & High only),
        # sourced from the same flags/investigations as the Action Plan tab.
        st.markdown("## 4. Recommended actions")
        _src_flags = st.session_state.get("flags", brief.flags)
        _src_invs  = st.session_state.get("investigations", {})
        _all_acts  = orch.recommender.recommend(_src_flags, _src_invs)
        _priority  = [a for a in _all_acts if a.severity.value in ("Critical", "High")]
        if _priority:
            _pdf = pd.DataFrame([{
                "Severity":          a.severity.value,
                "BU":                a.affected_scope,
                "Cause KPI":         a.cause_kpi,
                "Recommended action": a.recommended_action,
                "Action owner":      a.action_owner,
                "Escalation":        a.escalation_level,
                "Est. impact (EGP)": round(a.estimated_impact_egp, 0),
                "Detail":            a.evidence,
            } for a in _priority])
            st.dataframe(_pdf, use_container_width=True, hide_index=True,
                         column_config={"Est. impact (EGP)": st.column_config.NumberColumn(
                             format="EGP %,.0f")})
        else:
            st.info("No Critical or High actions for this period.")


        st.download_button("Download brief (Markdown)",
                           brief.markdown.encode("utf-8"),
                           f"opd_executive_brief_{year}_{month:02d}.md")


# ----------------------------------------------------------------- tab: chat
with tab_chat:
    st.caption('Ask anything, e.g. "Why did HJH revenue drop in March 2025?" or '
               '"Which doctors are behind the ASH case shortfall?"')
    if not orch.llm.available:
        st.warning("LLM not configured — check that GROQ_API_KEY is set in .env.")
    else:
        st.session_state.setdefault("chat", [])
        for m in st.session_state["chat"]:
            st.chat_message(m["role"]).markdown(m["content"])
        if q := st.chat_input("Ask the OPD agent..."):
            st.chat_message("user").markdown(q)
            with st.chat_message("assistant"):
                live = st.status("Working...", expanded=False)
                try:
                    answer, _ = orch.ask(
                        q, history=st.session_state["chat"],
                        on_event=lambda e: live.write(f"🛠 `{e['tool']}` ← `{e['input']}`"))
                    live.update(state="complete")
                    st.markdown(answer)
                    st.session_state["chat"] += [{"role": "user", "content": q},
                                                 {"role": "assistant", "content": answer}]
                except Exception as exc:
                    live.update(state="error")
                    if "invalid" in str(exc).lower() or "authentication" in str(exc).lower():
                        st.error("🔑 " + str(exc))
                    else:
                        st.error(f"LLM error: {exc}")
