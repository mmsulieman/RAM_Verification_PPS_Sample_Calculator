# app.py
# Streamlit app for kebele-level PPS village sampling + HH systematic sampling + Bulk Pack ZIP exporter
# v2: Robust roster upload, village matching validator, and option to sample all roster villages

from pathlib import Path
import io
import math
import hashlib
from typing import Tuple

import numpy as np
import pandas as pd
import streamlit as st

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# ---------------------------------------------------------
# Branding assets (logo + favicon)
# ---------------------------------------------------------
ASSETS_LOGO = Path("assets/wfp_logo.png")
ASSETS_FAVICON = Path("assets/favicon.png")
PAGE_ICON = (
    str(ASSETS_FAVICON) if ASSETS_FAVICON.exists() else (str(ASSETS_LOGO) if ASSETS_LOGO.exists() else None)
)

st.set_page_config(page_title="Kebele-level PPS Village & HH Sampler", page_icon=PAGE_ICON, layout="wide")

# ---------------------------------------------------------
# Header and Helper drawer
# ---------------------------------------------------------
def render_header():
    left, mid, right = st.columns([0.13, 0.74, 0.13])
    with left:
        if ASSETS_LOGO.exists():
            st.image(str(ASSETS_LOGO), width=90)
    with mid:
        st.markdown(
            """
            <div style="padding-top:6px;">
              <h1 style="margin-bottom:0;">Kebele-level PPS Village & Household Sampler</h1>
              <p style="margin-top:4px; color: gray;">WFP Ethiopia – Somali Region (Jijiga AO)</p>
            </div>
            """,
            unsafe_allow_html=True
        )
    with right:
        st.markdown("\n\n")
        st.button("📘 Help", key="help_button")

    st.markdown(
        """
        <style>.block-container { padding-top: 1.0rem !important; }</style>
        """,
        unsafe_allow_html=True
    )

render_header()

with st.sidebar:
    if ASSETS_LOGO.exists():
        st.image(str(ASSETS_LOGO), width=140)
    with st.expander("📘 Help & Guide", expanded=False):
        st.markdown(
            """
            **What this app does**  
            1) **PPS village selection** (Systematic/Independent)  
            2) **Systematic household sampling** per village for **Eligible**/**Non‑eligible**  
            3) **Bulk Pack export**: per‑village ZIP with CSV + PDF field sheets

            **Inputs**  
            • Villages (Excel/CSV): `Woreda | Kebele | Village | HHs`  
            • Roster (Excel/CSV): `Woreda | Kebele | Village | Eligibility | HH_ID | Household Head Name | Phone | Other ID`  
            • Quotas (optional): `Woreda | Kebele | Village | nE | nNE`

            **Outputs**  
            • `Sampled_Villages.(csv/xlsx)` with Diagnostics  
            • `HH_Sample.(csv/xlsx)` with HH_Summary  
            • `Bulk_Pack.zip` – folders per village, CSV + PDF

            **Tips**  
            • Use **Systematic PPS** for even spread and no duplicates  
            • Set a **Random seed** for reproducibility  
            • For big rosters, filter to target woredas/kebeles
            """
        )

# ---------------------------------------------------------
# Utilities
# ---------------------------------------------------------
REQUIRED_COLS = ["woreda", "kebele", "village", "hhs"]

def norm_colnames(cols):
    return [str(c).strip().lower().replace("\n"," ").replace("\t"," ") for c in cols]


def read_input(file) -> pd.DataFrame:
    name = getattr(file, "name", "uploaded").lower()
    ext = name.split(".")[-1]
    try:
        if ext in ["xlsx", "xls"]:
            df = pd.read_excel(file, engine=None)
        else:
            try: df = pd.read_csv(file)
            except UnicodeDecodeError: df = pd.read_csv(file, encoding="cp1252")
    except Exception as e:
        raise ValueError(f"Could not read village file: {e}")

    df.columns = norm_colnames(df.columns)
    rename_map = {}
    for c in df.columns:
        if c in ["woreda"]: rename_map[c] = "woreda"
        if c in ["kebele"]: rename_map[c] = "kebele"
        if c in ["village", "village / ea", "ea", "enumeration area"]: rename_map[c] = "village"
        if c in ["hhs", "hh", "households", "# households (hh)", "households (hh)", "households (no)"]:
            rename_map[c] = "hhs"
    df = df.rename(columns=rename_map)

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Village frame missing columns: {missing}. Found: {list(df.columns)}")

    df = df[REQUIRED_COLS].copy()
    for c in ["woreda", "kebele", "village"]:
        df[c] = df[c].astype(str).str.strip()
    df["hhs"] = pd.to_numeric(df["hhs"], errors="coerce").fillna(0).astype(float)
    df = df[(df["hhs"] > 0) & (df["kebele"].str.len() > 0) & (df["village"].str.len() > 0)].copy()
    return df


def rng_for_group(seed_base, woreda, kebele):
    if not seed_base:
        return np.random.default_rng()
    s = f"{seed_base}::{woreda}::{kebele}"
    h = int(hashlib.blake2b(s.encode("utf-8"), digest_size=8).hexdigest(), 16)
    return np.random.default_rng(h)


def pps_select_independent(cum_high, rng, m, avoid_duplicates=True, max_redraws=1000):
    n = len(cum_high)
    if n == 0: return []
    def pick(u):
        idx = np.searchsorted(cum_high, u, side="left")
        if idx >= n: idx = n-1
        return int(idx)
    if not avoid_duplicates:
        return [pick(u) for u in rng.random(m)]
    selected = set(); attempts = 0
    while len(selected) < min(m,n) and attempts < max_redraws:
        selected.add(pick(rng.random())); attempts += 1
    if len(selected) < min(m,n):
        interval = 1.0 / max(min(m,n), 1)
        start = rng.random() * interval
        k = 0
        while len(selected) < min(m,n) and k < m*3:
            u = start + (k % m) * interval
            u = u - math.floor(u)
            selected.add(pick(u)); k += 1
    return sorted(selected)


def pps_select_systematic(cum_high, rng, m):
    n = len(cum_high)
    if n == 0: return []
    interval = 1.0 / max(m, 1)
    start = rng.random() * interval
    indices = []
    for k in range(m):
        u = start + k*interval
        if u >= 1.0: u -= math.floor(u)
        idx = int(np.searchsorted(cum_high, u, side="left"))
        if idx >= n: idx = n-1
        indices.append(idx)
    seen, uniq = set(), []
    for i in indices:
        if i not in seen:
            seen.add(i); uniq.append(i)
    return uniq


def sample_kebele(df_k: pd.DataFrame, m: int, method: str, rng, avoid_duplicates=True) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df_k = df_k.copy()
    total = df_k["hhs"].sum()
    if total <= 0:
        df_k["p"], df_k["cum_high"], df_k["cum_low"] = 0.0, 0.0, 0.0
        diag = df_k.copy(); diag["method"], diag["m_requested"], diag["m_final"], diag["selected"] = method, m, 0, False
        return df_k.iloc[0:0], diag
    df_k["p"] = df_k["hhs"] / total
    df_k["cum_high"] = df_k["p"].cumsum(); df_k["cum_low"] = df_k["cum_high"] - df_k["p"]
    nvill = len(df_k); m_eff = min(int(m), nvill) if nvill > 0 else 0
    if m_eff <= 0:
        diag = df_k.copy(); diag["method"], diag["m_requested"], diag["m_final"], diag["selected"] = method, m, 0, False
        return df_k.iloc[0:0], diag
    if method == "Independent":
        idxs = pps_select_independent(df_k["cum_high"].to_numpy(), rng, m_eff, avoid_duplicates=avoid_duplicates)
    else:
        idxs = pps_select_systematic(df_k["cum_high"].to_numpy(), rng, m_eff)
    idxs = list(dict.fromkeys(idxs))
    sel = df_k.iloc[idxs].copy()[["woreda", "kebele", "village", "hhs"]]
    diag = df_k.copy(); diag["method"], diag["m_requested"], diag["m_final"], diag["selected"] = method, m, len(idxs), False
    diag.loc[diag.index.isin(df_k.index[idxs]), "selected"] = True
    return sel, diag


def run_pps(df: pd.DataFrame, method: str, m_default: int, threshold_n: int, m_large: int,
            use_fixed_m: bool, fixed_m: int, seed_base: str, avoid_dups_indep: bool):
    grp_cols = ["woreda", "kebele"]
    sampled_rows, diag_rows, kebele_summary = [], [], []
    for (w, k), g in df.groupby(grp_cols, dropna=False, sort=False):
        g = g.reset_index(drop=True)
        nvill = len(g)
        m = max(int(fixed_m if use_fixed_m else (m_large if nvill >= int(threshold_n) else m_default)), 1)
        rng = rng_for_group(seed_base, w, k)
        sel, diag = sample_kebele(g, m=m, method=("Independent" if method == "Independent" else "Systematic"), rng=rng, avoid_duplicates=bool(avoid_dups_indep))
        sampled_rows.append(sel); diag_rows.append(diag)
        kebele_summary.append({"Woreda": w, "Kebele": k, "#Villages": nvill, "Method": method, "m_used": int(m), "Total HHs": int(g["hhs"].sum())})
    sampled = pd.concat(sampled_rows, ignore_index=True) if sampled_rows else df.iloc[0:0]
    diagnostics = pd.concat(diag_rows, ignore_index=True) if diag_rows else df.iloc[0:0]
    summary = pd.DataFrame(kebele_summary)
    return sampled, diagnostics, summary


def to_excel_bytes(sampled: pd.DataFrame, diagnostics: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        sampled.to_excel(writer, index=False, sheet_name="Sampled_Villages")
        diagnostics.to_excel(writer, index=False, sheet_name="Diagnostics")
    return output.getvalue()

# ---------------------------------------------------------
# PPS Stage UI
# ---------------------------------------------------------
with st.sidebar:
    st.header("PPS Settings")
    method = st.selectbox("PPS Method", ["Systematic", "Independent"], index=0)
    avoid_dups_indep = st.checkbox("(Independent) Avoid duplicates", value=True)
    st.markdown("---")
    use_fixed_m = st.checkbox("Use fixed m for all kebeles", value=False)
    if use_fixed_m:
        fixed_m = st.number_input("Fixed m", min_value=1, max_value=30, value=2, step=1)
        m_default, threshold_n, m_large = 2, 7, 4
    else:
        m_default = st.number_input("Default m", min_value=1, max_value=30, value=2, step=1)
        threshold_n = st.number_input("If kebele has ≥ (villages)", min_value=2, max_value=1000, value=7, step=1)
        m_large = st.number_input("Use m =", min_value=1, max_value=30, value=4, step=1)
        fixed_m = m_default
    st.markdown("---")
    seed_base = st.text_input("Random seed (optional)", value="")

st.caption("Upload a 4-column frame — **Woreda | Kebele | Village | HHs** — to run kebele-level PPS.")
uploaded = st.file_uploader("Upload Excel/CSV (Woreda | Kebele | Village | HHs)", type=["xlsx", "xls", "csv"], key="vill_upl")

sampled = None
if uploaded is not None:
    try:
        df = read_input(uploaded)
        st.success(f"Loaded {len(df):,} rows across {df[['woreda','kebele']].drop_duplicates().shape[0]} kebele(s).")
        with st.expander("Preview (top 25 rows)"):
            st.dataframe(df.head(25), use_container_width=True)
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Kebele → #Villages**")
            st.dataframe(df.groupby(["woreda", "kebele"], as_index=False)["village"].nunique().rename(columns={"village":"#Villages"}), use_container_width=True, height=240)
        with col2:
            st.markdown("**Kebele → Total HHs**")
            st.dataframe(df.groupby(["woreda", "kebele"], as_index=False)["hhs"].sum().rename(columns={"hhs":"Total HHs"}), use_container_width=True, height=240)
        st.markdown("---")
        if st.button("🔁 Run PPS Sampling", type="primary"):
            sampled, diagnostics, summary = run_pps(df, method, m_default, threshold_n, m_large, use_fixed_m, fixed_m, seed_base, avoid_dups_indep)
            st.subheader("✅ Sampled Villages (all kebeles)"); st.dataframe(sampled, use_container_width=True, height=320)
            st.subheader("📋 Kebele Summary (method & m used)"); st.dataframe(summary, use_container_width=True, height=240)
            st.markdown("### ⬇️ Download Results")
            st.download_button("Download Sampled_Villages.csv", data=sampled.to_csv(index=False).encode("utf-8"), file_name="Sampled_Villages.csv", mime="text/csv")
            st.download_button("Download Sampled_Villages.xlsx (with Diagnostics)", data=to_excel_bytes(sampled, diagnostics), file_name="Sampled_Villages.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        st.error(f"Error: {e}")
else:
    st.info("Upload a file to begin PPS sampling.")

# ---------------------------------------------------------
# HH Systematic Sampling — robust readers and validator
# ---------------------------------------------------------

def read_roster(file) -> pd.DataFrame:
    name = getattr(file, "name", "uploaded").lower()
    ext = name.split(".")[-1]
    try:
        if ext in ["xlsx", "xls"]:
            r = pd.read_excel(file, engine=None)
        else:
            try: r = pd.read_csv(file)
            except UnicodeDecodeError: r = pd.read_csv(file, encoding="cp1252")
    except Exception as e:
        raise ValueError(f"Could not read roster file: {e}")

    r.columns = norm_colnames(r.columns)
    rename = {}
    for c in r.columns:
        if c in ["woreda"]: rename[c] = "woreda"
        if c in ["kebele"]: rename[c] = "kebele"
        if c in ["village", "village / ea", "ea", "enumeration area"]: rename[c] = "village"
        if c in ["hh_id", "hh id", "household id", "registration #", "registration", "id"]: rename[c] = "hh_id"
        if c in ["household head name", "hh head name", "head name", "hh_name"]: rename[c] = "head_name"
        if c in ["eligibility", "eligible_flag", "status"]: rename[c] = "eligibility"
        if c in ["phone", "phone (optional)", "phone_number"]: rename[c] = "phone"
        if c in ["other id", "other_id", "alt id", "alt_id"]: rename[c] = "other_id"
    r = r.rename(columns=rename)

    required = ["woreda", "kebele", "village", "eligibility"]
    missing = [c for c in required if c not in r.columns]
    if missing:
        raise ValueError(f"Roster missing required columns: {missing}. Ensure at least Woreda, Kebele, Village, Eligibility.")

    for c in ["woreda", "kebele", "village", "eligibility"]:
        r[c] = r[c].astype(str).str.strip()

    # Normalize eligibility labels
    r["eligibility"] = r["eligibility"].str.strip().str.lower().map({
        "eligible":"Eligible","non-eligible":"Non-eligible","non eligible":"Non-eligible","noneligible":"Non-eligible","ineligible":"Non-eligible","e":"Eligible","ne":"Non-eligible"
    }).fillna(r["eligibility"].str.title())

    # Ensure optional columns exist
    for col in ["hh_id","head_name","phone","other_id"]:
        if col not in r.columns: r[col] = ""

    return r[["woreda","kebele","village","eligibility","hh_id","head_name","phone","other_id"]]


def read_quotas(file) -> pd.DataFrame:
    name = getattr(file, "name", "uploaded").lower()
    ext = name.split(".")[-1]
    try:
        if ext in ["xlsx", "xls"]:
            q = pd.read_excel(file, engine=None)
        else:
            try: q = pd.read_csv(file)
            except UnicodeDecodeError: q = pd.read_csv(file, encoding="cp1252")
    except Exception as e:
        raise ValueError(f"Could not read quotas file: {e}")

    q.columns = norm_colnames(q.columns)
    rename = {}
    for c in q.columns:
        if c in ["woreda"]: rename[c] = "woreda"
        if c in ["kebele"]: rename[c] = "kebele"
        if c in ["village", "village / ea", "ea"]: rename[c] = "village"
        if c in ["ne","n_eligible","eligible_n","eligible"]: rename[c] = "nE"
        if c in ["nne","n_noneligible","non-eligible_n","noneligible","non_eligible"]: rename[c] = "nNE"
    q = q.rename(columns=rename)

    missing = [c for c in ["woreda","kebele","village","nE","nNE"] if c not in q.columns]
    if missing:
        raise ValueError(f"Quotas missing columns: {missing}. Expected Woreda, Kebele, Village, nE, nNE")

    for c in ["woreda","kebele","village"]: q[c] = q[c].astype(str).str.strip()
    for c in ["nE","nNE"]: q[c] = pd.to_numeric(q[c], errors="coerce").fillna(0).astype(int).clip(lower=0)

    return q[["woreda","kebele","village","nE","nNE"]]

# Validator — match sampled villages to roster

def match_diagnostics(sampled_villages: pd.DataFrame, roster: pd.DataFrame):
    sv = sampled_villages.copy()
    sv = sv.rename(columns={c: c.lower() for c in sv.columns})
    if 'village / ea' in sv.columns: sv = sv.rename(columns={'village / ea':'village'})
    for c in ['woreda','kebele','village']:
        if c in sv.columns: sv[c] = sv[c].astype(str).strip()

    r = roster.copy()
    for c in ['woreda','kebele','village']:
        r[c] = r[c].astype(str).str.strip()

    sv_keys = sv[['woreda','kebele','village']].drop_duplicates()
    r_keys = r[['woreda','kebele','village']].drop_duplicates()

    merged = sv_keys.merge(r_keys, on=['woreda','kebele','village'], how='left', indicator=True)
    missing_in_roster = merged[merged['_merge']=='left_only'].drop(columns=['_merge'])
    extra_in_roster = r_keys.merge(sv_keys, on=['woreda','kebele','village'], how='left', indicator=True)
    extra_in_roster = extra_in_roster[extra_in_roster['_merge']=='left_only'].drop(columns=['_merge'])

    return missing_in_roster, extra_in_roster

# ---------------------------------------------------------
# HH Systematic core
# ---------------------------------------------------------

def to_excel_bytes_hh(hh_sample: pd.DataFrame, hh_summary: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        hh_sample.to_excel(writer, index=False, sheet_name="HH_Sample")
        hh_summary.to_excel(writer, index=False, sheet_name="HH_Summary")
    return output.getvalue()


def rng_for_group4(seed_base, woreda, kebele, village, group_label):
    if not seed_base:
        return np.random.default_rng()
    s = f"{seed_base}::{woreda}::{kebele}::{village}::{group_label}"
    h = int(hashlib.blake2b(s.encode("utf-8"), digest_size=8).hexdigest(), 16)
    return np.random.default_rng(h)


def _systematic_indices(N: int, n: int, rng: np.random.Generator):
    if N <= 0 or n <= 0:
        return [], None, None
    n_eff = min(int(n), int(N))
    interval = N / n_eff
    start = rng.uniform(0.0, interval)
    idxs = [int(np.floor(start + k*interval)) for k in range(n_eff)]
    seen, out = set(), []
    for i in idxs:
        j = min(max(i, 0), N-1)
        if j not in seen:
            seen.add(j); out.append(j)
        if len(out) == n_eff: break
    return out, interval, start


def sample_households_systematic(
    roster: pd.DataFrame,
    sampled_villages: pd.DataFrame,
    default_nE: int,
    default_nNE: int,
    order_by: str,
    quotas: pd.DataFrame | None,
    seed_base: str,
    village_source: str = 'sampled' # 'sampled' or 'roster'
):
    # Determine village list
    if village_source == 'roster':
        sv = roster[['woreda','kebele','village']].drop_duplicates().copy()
    else:
        sv = sampled_villages.copy()
        sv.columns = [c.lower() for c in sv.columns]
        sv = sv.rename(columns={'village / ea':'village'})
        sv = sv[['woreda','kebele','village']].drop_duplicates()

    for c in ['woreda','kebele','village']: sv[c] = sv[c].astype(str).str.strip()

    q = quotas.copy() if quotas is not None else pd.DataFrame(columns=['woreda','kebele','village','nE','nNE'])
    quota_map = {(row.woreda, row.kebele, row.village): (int(row.nE), int(row.nNE)) for _, row in q.iterrows()}

    out_rows, sum_rows = [], []
    for _, vrow in sv.iterrows():
        w, k, v = vrow.woreda, vrow.kebele, vrow.village
        sub = roster[(roster['woreda']==w) & (roster['kebele']==k) & (roster['village']==v)].copy()
        if sub.empty:
            for g in ['Eligible','Non-eligible']:
                sum_rows.append({'Woreda':w,'Kebele':k,'Village':v,'Group':g,'N':0,'n':0,'Interval':None,'Start':None,'Picked':0})
            continue
        nE, nNE = quota_map.get((w,k,v), (default_nE, default_nNE))
        for g, n_target in [('Eligible', nE), ('Non-eligible', nNE)]:
            gdf = sub[sub['eligibility']==g].copy(); N = len(gdf)
            if N==0 or n_target<=0:
                sum_rows.append({'Woreda':w,'Kebele':k,'Village':v,'Group':g,'N':int(N),'n':int(n_target),'Interval':None,'Start':None,'Picked':0}); continue
            if order_by in gdf.columns:
                gdf = gdf.sort_values(order_by, kind='mergesort')
            else:
                gdf = gdf.sort_values(['head_name','hh_id'], na_position='last', kind='mergesort')
            rng = rng_for_group4(seed_base, w, k, v, g)
            idxs, interval, start = _systematic_indices(N, int(n_target), rng)
            pick = gdf.iloc[idxs].copy()
            pick.insert(0, 'Sample_Order', range(1, len(pick)+1))
            pick.insert(0, 'Eligibility', g)
            pick.insert(0, 'Village', v)
            pick.insert(0, 'Kebele', k)
            pick.insert(0, 'Woreda', w)
            out_rows.append(pick[['Woreda','Kebele','Village','Eligibility','Sample_Order','hh_id','head_name','phone','other_id']])
            sum_rows.append({'Woreda':w,'Kebele':k,'Village':v,'Group':g,'N':int(N),'n':int(n_target),'Interval':round(interval,3),'Start':round(float(start),3),'Picked':len(pick)})

    hh_sample = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame(columns=['Woreda','Kebele','Village','Eligibility','Sample_Order','hh_id','head_name','phone','other_id'])
    hh_summary = pd.DataFrame(sum_rows)
    return hh_sample, hh_summary

# PDF per-village (field sheet)

def _safe_name(s: str) -> str:
    return ''.join([c if c.isalnum() or c in [' ','_','-'] else '_' for c in str(s)]).strip().replace(' ','_')


def _village_pdf_bytes(w, k, v, hh_df: pd.DataFrame, sum_df: pd.DataFrame, app_title: str = "PPS & HH Sampler") -> bytes:
    buff = io.BytesIO()
    doc = SimpleDocTemplate(buff, pagesize=A4, leftMargin=1.6*cm, rightMargin=1.6*cm, topMargin=1.6*cm, bottomMargin=1.6*cm)
    styles = getSampleStyleSheet(); title = styles['Title']; title.textColor = colors.HexColor('#1F77B4')
    h2 = styles['Heading2']; h2.textColor = colors.HexColor('#1F77B4'); body = styles['BodyText']
    elems = []
    elems.append(Paragraph(f"<b>{app_title}</b>", title)); elems.append(Spacer(1, 8))
    elems.append(Paragraph(f"<b>Woreda:</b> {w} &nbsp;&nbsp; <b>Kebele:</b> {k} &nbsp;&nbsp; <b>Village:</b> {v}", body))
    sums = sum_df[(sum_df['Woreda']==w) & (sum_df['Kebele']==k) & (sum_df['Village']==v)].copy()
    data = [["Group","N (frame)","n (target)","Interval","Start","Picked"]]
    for _, row in sums.iterrows():
        data.append([row.get('Group',''), row.get('N',''), row.get('n',''), row.get('Interval',''), row.get('Start',''), row.get('Picked','')])
    t = Table(data, hAlign='LEFT'); t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0), colors.HexColor('#E2F0D9')),
        ('GRID',(0,0),(-1,-1), 0.4, colors.grey),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('ALIGN',(1,1),(-1,-1),'CENTER')
    ]))
    elems.append(Spacer(1,6)); elems.append(Paragraph("Household sampling summary", h2)); elems.append(t)
    head = hh_df[(hh_df['Woreda']==w) & (hh_df['Kebele']==k) & (hh_df['Village']==v)].head(12)
    if not head.empty:
        td = [["Elig","Order","HH_ID","Head Name","Phone","Other ID"]]
        for _, r in head.iterrows():
            td.append([r.get('Eligibility',''), r.get('Sample_Order',''), r.get('hh_id',''), r.get('head_name',''), r.get('phone',''), r.get('other_id','')])
        t2 = Table(td, hAlign='LEFT', colWidths=[2*cm,2*cm,3*cm,6*cm,3.5*cm,3.5*cm])
        t2.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0), colors.HexColor('#D9E1F2')),
            ('GRID',(0,0),(-1,-1), 0.4, colors.grey),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold')
        ]))
        elems.append(Spacer(1,6)); elems.append(Paragraph("Sample preview (first 12)", h2)); elems.append(t2)
    elems.append(Spacer(1,12)); elems.append(Paragraph("Field notes: _______________________________________________", body))
    elems.append(Spacer(1,6)); elems.append(Paragraph("Enumerator: _____________________   Team Lead: _____________________", body))
    doc.build(elems); return buff.getvalue()


def build_bulk_pack_zip(hh_sample: pd.DataFrame, hh_summary: pd.DataFrame, include_pdfs: bool = True, include_csvs: bool = True,
                         folder_by: str = 'kebele', app_title: str = 'PPS & HH Sampler') -> bytes:
    if hh_sample is None or hh_sample.empty:
        raise ValueError("No household sample available. Run HH sampling first.")
    bytestream = io.BytesIO()
    with pd.option_context('mode.chained_assignment', None):
        with zipfile.ZipFile(bytestream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            villages = hh_sample[['Woreda','Kebele','Village']].drop_duplicates().values.tolist()
            for w,k,v in villages:
                safe = {'w': _safe_name(w), 'k': _safe_name(k), 'v': _safe_name(v)}
                if folder_by == 'woreda': folder = f"{safe['w']}/{safe['k']}/{safe['v']}"
                elif folder_by == 'kebele': folder = f"{safe['k']}/{safe['v']}"
                else: folder = f"{safe['w']}_{safe['k']}_{safe['v']}"
                sub = hh_sample[(hh_sample['Woreda']==w) & (hh_sample['Kebele']==k) & (hh_sample['Village']==v)]
                if include_csvs:
                    z.writestr(f"{folder}/{safe['v']}_HH_Sample.csv", sub.to_csv(index=False).encode('utf-8'))
                if include_pdfs:
                    z.writestr(f"{folder}/{safe['v']}_FieldSheet.pdf", _village_pdf_bytes(w,k,v, hh_sample, hh_summary, app_title=app_title))
    bytestream.seek(0); return bytestream.getvalue()

# ---------------------------------------------------------
# HH UI
# ---------------------------------------------------------
st.markdown("---")
st.subheader("🏠 Household Systematic Sampling (within villages)")

if 'sampled' not in locals() or sampled is None or len(sampled)==0:
    st.info("Run PPS village sampling above first; this section uses the selected villages.")
else:
    colA, colB = st.columns([0.6, 0.4])
    with colA:
        roster_file = st.file_uploader("Upload household roster (Excel/CSV)", type=["xlsx","xls","csv"], key="roster_upl")
        quotas_file = st.file_uploader("Optional: per-village quotas (Woreda, Kebele, Village, nE, nNE)", type=["xlsx","xls","csv"], key="quota_upl")
    with colB:
        st.write("**Defaults (if no quotas)**")
        default_nE = st.number_input("Eligible per village (default)", min_value=0, max_value=9999, value=15, step=1)
        default_nNE = st.number_input("Non-eligible per village (default)", min_value=0, max_value=9999, value=15, step=1)
        order_by = st.text_input("Order by column (in roster)", value="hh_id", help="Used to sort before systematic skip; fallback = head_name → hh_id")
        hh_seed = st.text_input("Random seed (optional)", value="")
    if roster_file is not None:
        try:
            roster_df = read_roster(roster_file)
            st.success(f"Roster loaded: {len(roster_df):,} rows")
            with st.expander("Roster preview (top 20 rows)"):
                st.dataframe(roster_df.head(20), use_container_width=True)

            # Matching diagnostics
            missing_in_roster, extra_in_roster = match_diagnostics(sampled, roster_df)
            with st.expander("🔎 Village matching diagnostics"):
                st.write("**Sampled villages not found in roster**")
                st.dataframe(missing_in_roster if not missing_in_roster.empty else pd.DataFrame({"info":["None – all sampled villages found in roster"]}))
                st.write("**Roster villages not in sampled set** (OK if you include non-sampled ones)")
                st.dataframe(extra_in_roster if not extra_in_roster.empty else pd.DataFrame({"info":["None – roster fully aligns with sampled set"]}))

            quotas_df = None
            if quotas_file is not None:
                quotas_df = read_quotas(quotas_file)
                st.success(f"Quotas loaded: {len(quotas_df):,} rows")
                with st.expander("Quotas preview (top 20 rows)"):
                    st.dataframe(quotas_df.head(20), use_container_width=True)

            st.markdown("---")
            st.write("**Village source for HH sampling**")
            village_source = st.radio("Choose which villages to sample:", ["Only sampled villages (recommended)", "All villages in roster"], index=0)
            source_key = 'sampled' if village_source.startswith('Only') else 'roster'

            run_hh = st.button("▶️ Run Household Systematic Sampling", type="primary")
            if run_hh:
                hh_sample, hh_summary = sample_households_systematic(
                    roster=roster_df,
                    sampled_villages=sampled,
                    default_nE=default_nE,
                    default_nNE=default_nNE,
                    order_by=order_by.strip().lower(),
                    quotas=quotas_df,
                    seed_base=hh_seed,
                    village_source=source_key
                )
                if hh_sample.empty:
                    st.warning("No households were selected. Check quotas vs. roster availability and village matching diagnostics above.")
                st.subheader("✅ Household Sample"); st.dataframe(hh_sample, use_container_width=True, height=360)
                st.subheader("📊 HH Summary / Diagnostics"); st.dataframe(hh_summary, use_container_width=True, height=240)
                st.markdown("### ⬇️ Download Household Samples")
                st.download_button("Download HH_Sample.csv", data=hh_sample.to_csv(index=False).encode("utf-8"), file_name="HH_Sample.csv", mime="text/csv")
                st.download_button("Download HH_Sample.xlsx (with HH_Summary)", data=to_excel_bytes_hh(hh_sample, hh_summary), file_name="HH_Sample.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

                # Bulk pack
                st.markdown("---")
                st.subheader("📦 One‑click Bulk Pack (ZIP)")
                colx, coly, colz = st.columns(3)
                with colx: include_pdfs = st.checkbox("Include PDFs", value=True)
                with coly: include_csvs = st.checkbox("Include CSVs", value=True)
                with colz: folder_by = st.selectbox("Folder structure", ["kebele","woreda","flat"], index=0)
                app_title = st.text_input("PDF header title", value="WFP – PPS & Household Sampler")
                if st.button("📦 Build Bulk Pack ZIP", type="secondary"):
                    try:
                        zip_bytes = build_bulk_pack_zip(hh_sample, hh_summary, include_pdfs=include_pdfs, include_csvs=include_csvs, folder_by=folder_by, app_title=app_title)
                        st.download_button("Download Bulk_Pack.zip", data=zip_bytes, file_name="Bulk_Pack.zip", mime="application/zip")
                        st.success("Bulk pack prepared. Click the button to download.")
                    except Exception as e:
                        st.error(f"Bulk pack error: {e}")
        except Exception as e:
            st.error(f"Roster/Quotas error: {e}")
    else:
        st.info("Upload a household roster to enable this section.")

st.markdown("<hr style='margin-top:2rem;margin-bottom:0.5rem;'><div style='color:gray;font-size:0.9em;'>© WFP Ethiopia – Somali Region (Jijiga AO) | PPS village & household sampling utility</div>", unsafe_allow_html=True)
