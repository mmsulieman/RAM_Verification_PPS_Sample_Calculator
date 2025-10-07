
# PPS Village & Household Sampler (Streamlit) — v2

**New in v2**
- Robust roster/quotas readers with clearer errors
- Village **matching diagnostics** (sampled ↔ roster)
- Option to sample **Only sampled villages** or **All villages in roster**
- Bulk Pack ZIP (per-village CSV + PDF)

## Run locally
```bash
python -m venv .venv
.venv\Scriptsctivate  # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Cloud
- Push all files to GitHub → Cloud: New app → file = `app.py` → Deploy

## Inputs & Outputs
- Villages: `Woreda|Kebele|Village|HHs` → `Sampled_Villages.(csv/xlsx)`
- Roster: `Woreda|Kebele|Village|Eligibility|HH_ID|Head Name|Phone|Other ID` (+ Quotas) → `HH_Sample.(csv/xlsx)` + Bulk ZIP
