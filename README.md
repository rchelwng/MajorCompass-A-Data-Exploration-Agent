# Major Decision Assistant

A **Deep Research** agent that tests whether college majors deliver the earnings and jobs students expect, and helps a student build a data-backed shortlist of majors.

**Target user:** high-school and college students (and their families or advisors) choosing a major, who want evidence instead of folklore. US, bachelor's degrees.

**Live demo:** see `submission.json` ([https://majorcompass.cloud.run/](https://majorcompass.cloud.run/)). **Path:** `research`.

## What it does

Ask a question ("Which majors pay well but have higher unemployment?") or state priorities ("I want $80k+ by my late 20s with low unemployment risk"). Then:

1. **Lead** decides if the question is specific enough. If not (e.g. "is engineering good?", or "recommend a major" with no priorities) it asks a clarifying question and runs nothing.
2. Otherwise the Lead splits the question into 2-5 self-contained sub-questions and calls **`research` several times in one turn**. Each **Researcher** runs in parallel in a *fresh context* with only the sub-question, and queries the database through `describe_data`, `find_majors` and `run_sql`.
3. The Lead reviews findings. If one is `partial`/`cannot_answer`, conflicts, or leaves a comparison missing, it goes back for **one more targeted round**. Hard caps: 6 Lead turns, 8 Researcher runs, 10 turns per Researcher.
4. When covered, the Lead replies `DONE: <the student's full question, with priorities>`. Code (not the Lead) then hands the **Writer** the question, the Researchers' findings *exactly as they wrote them*, and an evidence ledger of the exact query results. The Writer never sees the Lead's retelling, so numbers can't drift in transit.

Researchers run on the cheaper `gemini-3.5-flash-lite`; Lead and Writer on `gemini-3.5-flash`. Every model call has a 90 s timeout and 2 retries.

**Context panel.** The right-hand panel (`context_panel.py` / `context-panel.js`, reused from the course's Wikipedia deep-research template) draws a live timeline, one column per agent (Lead, Researcher 1..n, Writer), showing the exact messages each model received. It makes the parallel fan-out and the fresh Researcher contexts visible. The page polls `/context` during `/chat` using an optional `run_id` request field; the response shape is unchanged.

**Accurate citations.** Every successful `run_sql` gets an evidence id (`E123`) stored with its SQL and rows. The Writer cites `[E123]` after each number. After generation the server checks two things: (1) every cited id exists in that request's ledger (unknown ids become `[unverified]`); (2) every dollar amount, percentage and large or decimal number appears in the rows of the evidence cited on the same line, within rounding (`$126k` matches 125,810; years, ages, small counts and numbers the student typed are exempt). Figures that fail, or that have no citation, are listed under "Not verified against the cited query results" (they may be derived, such as a difference or ratio, or misquoted). A Sources list then gives the dataset and exact SQL for each cited id. Limit: the check proves a number exists in the cited result, not that it is attached to the right major.

**Says when data can't answer.** Researchers return `STATUS: answered | partial | cannot_answer` plus `GAPS`; the Writer adds a "What the data can't tell you" section (satisfaction, specific schools, causation, years after 2024, non-bachelor's degrees, etc.).

**Show your work.** `POST /chat` keeps Project 1's shape: `{response, session_id, tool_calls}`. Every entry in `tool_calls` (Lead's `research` / `write_report` and each Researcher's SQL) has an `agent` field. The page shows them under "Show the work".

**Errors never leak internals.** Tool failures, SQL errors, model/auth failures and storage errors return a fixed message with a next step to the model or the chat; the real exception goes to the server log only. (Sources footers show SQL on purpose: that is the evidence.)

**Decision-assistant feature.** For "which major should I pick?" questions the Writer returns a 3-5 option shortlist with trade-offs (median, p25-p75 spread, unemployment, growth with age, typical jobs and BLS outlook) tied to the student's stated priorities. It never declares a single "best" major.

## Data sources

| Source | Version used | What we use | Where to get it | Tables |
|---|---|---|---|---|
| **US Census ACS PUMS** | **2024 1-year** (released Dec 4, 2025; at the time of this creation, the 2025 1-year PUMS has no release date) | Earnings, percentiles, gender gaps, unemployment, earnings by age, occupations, by field of degree (bachelor's holders) | Auto-downloaded by `build_data.py` from <https://www2.census.gov/programs-surveys/acs/data/pums/2024/1-Year/csv_pus.zip> (~575 MB); labels from `https://api.census.gov/data/2024/acs/acs1/pums/variables/{FOD1P,SOCP}.json`, with a bundled fallback in `data/labels/` (174 majors, 530 occupation codes parsed from the official 2020-2024 PUMS data dictionary) | `major_outcomes`, `earnings_by_age`, `earnings_growth`, `major_occupations` |
| **BLS Employment Projections** | **2025-35** (Table 1.2, released Aug-Sep 2026) | Projected growth and median wage by occupation, joined to ACS occupation codes | Download <https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx> to `data/raw/bls_occupation.xlsx` (page: <https://www.bls.gov/emp/tables/occupational-projections-and-characteristics.htm>) | `occupation_outlook`, columns in `major_occupations` |
| **College Scorecard, Field of Study** | Most Recent file, refreshed Jun 10, 2026 (a later bulk file dated 10/15/2026 also exists) | National earnings by field (CIP) **1, 4, 5 and 7 years after completion** (median, P25, P75) and debt | Button *Most Recent Data by Field of Study* on <https://collegescorecard.ed.gov/data/>; unzip the CSV into `data/raw/`. Direct file names seen: `https://ed-public-download.scorecard.network/downloads/Most-Recent-Cohorts-Field-of-Study_06102026.zip` and `..._10152026.zip`. Dictionary: <https://collegescorecard.ed.gov/assets/CollegeScorecardDataDictionary.xlsx> | `scorecard_programs` |
| **FRED** | live at build time | Unemployment by education level (`LNS14027659/60/89/62` (CSVs you already downloaded in `data/raw/` are used as-is, any filename starting with the series ID)), bachelor's-only grads 20-24 and 25-34 (`CGBD2024`, `CGBD2534`), `UNRATE`, `CPIAUCSL` | Auto-downloaded from `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>` | `fred_series` |

Suggested split for the pair: *A* owns ACS + BLS (microdata, occupation join), *B* owns Scorecard + FRED and the Lead/Writer prompts. Edit to match who did what.

`build_data.py` is the offline build step: it computes survey-weighted medians and percentiles (full-time, year-round wage earners, 2024 dollars), unemployment rates, gender gaps (only with 30+ men and 30+ women), age-band earnings, and writes a `data_dictionary` table with definitions and caveats that Researchers read first. The output `data/majors.duckdb` is small (a few MB) and is committed with the repo, so Cloud Run needs no data download.

```bash
uv sync --group build
# 1. unzip the Scorecard field-of-study zip into data/raw/
# 2. save the BLS workbook as data/raw/bls_occupation.xlsx
uv run --group build build_data.py       # ~10-20 min, mostly the 575 MB PUMS download
# quick, smaller test build (NOT national): uv run --group build build_data.py --states ny,ca
```

### Known limitations (stated to the agent and to users)

- ACS is a sample: small majors have wide error. Rankings require 100+ sampled earners; `low_sample_warning` flags the rest.
- **"Five years after graduation"** has two lenses that don't measure the same thing. *Scorecard* `national_median_earnings_5yr` (with p25/p75) follows real graduates but only federal-aid recipients, includes self-employment, is noise-perturbed, and each horizon is a different graduating cohort. *ACS* `earnings_by_age` is a cross-section of different people: the 25-29 band is roughly 3-7 years after a typical age-22 graduation. The agent is told to cross-check one against the other and say when they disagree.
- Scorecard uses CIP codes; ACS uses its own major list. They are matched by description, never exactly.
- Gender gaps are raw (not adjusted for occupation, hours, experience). ACS earnings exclude self-employment income.
- BLS projections are forecasts, and they describe occupations, not majors; the link is the ACS occupation each major's graduates actually work in.

## Run locally

```bash
gcloud auth application-default login
uv run app.py            # http://127.0.0.1:8000
```

Env vars: `MODEL_NAME` (Lead/Writer, default `vertex_ai/gemini-3.5-flash`), `RESEARCHER_MODEL_NAME` (default `vertex_ai/gemini-3.5-flash-lite`), `VERTEXAI_PROJECT` (if ADC has no default project).

## Three sample queries

Each question has one answer computable from one table. **Expected** comes from the SQL shown; the agent answer and the no-tools answer come from `scripts/sample_queries.py`, which prints this whole section in markdown.

> **TODO before submitting:** run `uv run python scripts/sample_queries.py > samples.md` after building the real data and paste the output below. It was deliberately not pre-filled: the numbers depend on the real PUMS build and no results were fabricated.

| # | Question | Expected answer comes from |
|---|---|---|
| 1 | Among majors with at least 100 sampled full-time earners, which has the highest median earnings, and what is it? | `major_outcomes.median_earnings` (ACS) |
| 2 | Among majors with at least 30 full-time men and 30 full-time women, which has the largest gender earnings gap, as a percent of men's median? | `major_outcomes.gender_gap_pct` (ACS) |
| 3 | Among majors with at least 100 sampled earners in both age bands, which has the largest percent increase in median earnings from ages 25-29 to 35-44? | `earnings_growth.pct_growth_25_29_to_35_44` (ACS) |

<!-- PASTE samples.md HERE: for each sample, Expected | Agent answer | Same model, no tools, no system prompt -->

## Repo layout

`app.py` (agents + API) · `context_panel.py`, `context-panel.js` (template) · `index.html` · `build_data.py` (offline build) · `data/majors.duckdb` · `scripts/sample_queries.py` · `Dockerfile` · `pyproject.toml` / `uv.lock` · `submission.json`
