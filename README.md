# MajorCompass

A **deep-research agent** that checks whether college majors deliver the pay and jobs students expect. A Lead plans the question, several Researchers query the data in parallel, and a Writer produces a short answer in which every number is cited. US, bachelor's degrees.

- **Path:** Deep Research (`"path": "research"` in `submission.json`). SQL is one of the Researchers' tools; this is not a Text2SQL or evals project.
- **Live site:** <YOUR CLOUD RUN URL>
- **Team:** <UNI 1>, <UNI 2>

**Target users:** students, families and academic advisors choosing a major who want evidence (earnings, unemployment, whether graduates end up working in their field, how satisfied they are) instead of folklore. It can also build a shortlist from stated priorities, but it never declares a single "best" major.

## How it works

```
question ──> Lead ──(research × N, same turn, parallel)──> Researcher 1..N ──> findings
               │  asks a clarifying question if the question is vague           │
               └── goes back for gaps (capped) <────────────────────────────────┘
                                   └──> Writer ──> cited answer ──> citation check ──> user
```

| Agent | Model | Job |
|---|---|---|
| **Lead** | `gemini-3.5-flash` | Clarifies vague questions, splits the question into 2-5 self-contained tasks, calls `research` once per task **in the same turn**, reviews findings, goes back for gaps, then hands over |
| **Researcher 1..N** | `gemini-3.5-flash-lite` | Each runs in a **fresh context** (it sees only its task). Tools: `describe_data`, `find_majors`, `run_sql` |
| **Writer** | `gemini-3.5-flash` | Sees only the question, the Researchers' findings exactly as written, and the evidence ledger. Writes the answer |

- **Parallel fan-out, fresh contexts.** The Lead calls `research` several times in one turn; each call starts a separately numbered `Researcher N` agent with no shared history. The context panel in the UI shows them as side-by-side columns.
- **Goes back for more, with caps.** If a finding is `partial` or `cannot_answer` for something the data might hold, the Lead sends at most two more rounds. Hard limits in code: 8 Researcher runs per question, 6 Lead turns, 10 turns per Researcher. If the Lead runs out of turns, the Writer still answers from the evidence collected so far.
- **Vague input gets clarification.** "Is engineering good?" or "Which major should I pick?" with no priorities returns a short clarifying question and runs no research.
- **Accurately cited.** Every successful `run_sql` is stored with its SQL and rows under an evidence id (`E123`). The Writer cites `[E123]` after each number. The server then checks that (1) every cited id exists in that request's ledger (otherwise `[unverified]`), and (2) every dollar amount, percentage and large or decimal number appears in the rows of the evidence cited on the same line, within rounding. Figures that fail, or have no citation, are listed under "Not verified against the cited query results". A Sources list gives the dataset and exact SQL for each cited id. Limit: the check proves a number exists in the cited result, not that it is attached to the right major.
- **Says when the data can't answer.** Researchers return `STATUS: answered | partial | cannot_answer` plus `GAPS`. The Writer adds a "What the data can't tell you" section for causation, regret about a major, a specific school, forecasts, or anything outside the tables.
- **Shows its work.** `POST /chat` keeps Project 1's shape: `{response, session_id, tool_calls}`. Every entry in `tool_calls` (the Lead's `research` calls and each Researcher's `describe_data` / `find_majors` / `run_sql`) has an `agent` field. The page shows them under "Show the work", and a live **Agent timeline** panel shows the exact messages each agent received. (The request accepts an optional `run_id` so the page can poll that panel while `/chat` runs; the response shape is unchanged.)
- **Errors never leak internals.** SQL errors, tool failures and model failures return a fixed message with a next step; the real exception goes to the server log only.
- **Read-only data access.** The app opens `data/majors.duckdb` read-only with external access disabled. It makes no calls to the data providers at runtime; the only external call is to the model on Vertex AI.

## Data

Five public sources, loaded once by `build_data.py` into `data/majors.duckdb` (7.8 MB, committed). The model does not know these data deeply: they are the 2024 ACS, the 2025-35 BLS projections, and the 2023 NSCG, all recent, and the model's answers without the tools are wrong (see the comparison below).

| Source | Where it comes from | Tables |
|---|---|---|
| **Census ACS 2024 1-year PUMS** (person microdata, bachelor's holders) | Auto-downloaded by `build_data.py`: <https://www2.census.gov/programs-surveys/acs/data/pums/2024/1-Year/csv_pus.zip> (~575 MB); major and occupation labels from the Census API, with a bundled fallback in `data/labels/` | `major_outcomes`, `earnings_by_age`, `earnings_growth` (view), `major_occupations` |
| **BLS Employment Projections 2025-35** (Table 1.2) | Manual: <https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx> saved as `data/raw/bls_occupation.xlsx` | `occupation_outlook` (also joined into `major_occupations`) |
| **College Scorecard, Field of Study** | Manual: <https://collegescorecard.ed.gov/data/> ("Most Recent Data by Field of Study"), CSV unzipped into `data/raw/` | `scorecard_programs` |
| **FRED** (Federal Reserve Bank of St. Louis) | CSV exports in `data/raw/`; any series missing there is downloaded from `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>` | `fred_series` |
| **NSF National Survey of College Graduates (NSCG) 2023** | Auto-downloaded: <https://ncses.nsf.gov/822/assets/0/files/college_grads_2023.zip> (~64 MB) | `nscg_field_experience`, `nscg_group_experience`, `nscg_job_facets`, `nscg_outside_field_reasons`, plus the linked `acs_nscg_crosswalk` and `major_pay_vs_experience` (view) |

Row counts in the committed database: `major_outcomes` 174, `earnings_by_age` 1,383, `major_occupations` 2,598, `occupation_outlook` 425, `scorecard_programs` 392, `fred_series` 4,204, `nscg_field_experience` 120, `nscg_group_experience` 8, `nscg_job_facets` 1,080, `nscg_outside_field_reasons` 462, `acs_nscg_crosswalk` 174, `major_pay_vs_experience` 153.

**What each source contributes**
- **ACS:** earnings (median, p25, p75), gender gaps, unemployment, labor-force participation, earnings by age band, and each major's most common occupations. Earnings = wages of full-time (35+ hrs), year-round (50+ wks) workers, ages 25-64, whose highest degree is a bachelor's, in 2024 dollars. Medians are survey-weighted. Gender gap is `(men's median − women's median) / men's median`, shown only with 30+ men and 30+ women, and is not adjusted for occupation, hours or experience.
- **BLS:** projected 2025-35 growth and wages for the occupations each major's graduates work in. Projections describe occupations, not majors, and the wage is an employment-weighted average of occupation medians.
- **Scorecard:** national median, 25th and 75th percentile earnings by field of study 1, 4, 5 and 7 years after completion (federal-aid recipients who are working and not enrolled), plus debt. `national_median_earnings_5yr` is the direct measure of "five years after graduation".
- **NSCG:** what graduates themselves report about their current job: whether it is related to their degree, overall and per-aspect satisfaction (salary, security, advancement, contribution to society and others), why they work outside their field, and remote work. Population: employed bachelor's holders aged 25-64, February 2023. Fields with under 30 respondents are dropped; under 100 are flagged.
- **FRED** (context, not by major): monthly US unemployment by education level and for bachelor's-only graduates aged 20-24 and 25-34, plus CPI.

### The ACS-to-NSCG crosswalk (our own work)

The ACS and NSCG name majors differently, so `data/labels/acs_nscg_crosswalk.csv` is a hand-made match from each of the 174 ACS majors to the closest NSCG field, with a match quality: **same** (112), **close** (28), **loose** (24) or **none** (10). `major_pay_vs_experience` joins pay from ACS to the NSCG answers through it for the 153 majors with usable NSCG data. The agent is told to report the match quality whenever it is not "same".

### FRED series

All unemployment series come from the Bureau of Labor Statistics' monthly Current Population Survey (CPS), republished by FRED. SA = seasonally adjusted, NSA = not.

| Series | Measures | Source page |
|---|---|---|
| `LNS14027662` | Unemployment, bachelor's degree and higher, 25+ (SA) | <https://fred.stlouisfed.org/series/LNS14027662> |
| `LNS14027689` | Unemployment, some college or associate degree, 25+ (SA) | <https://fred.stlouisfed.org/series/LNS14027689> |
| `LNS14027660` | Unemployment, high school graduates, no college, 25+ (SA) | <https://fred.stlouisfed.org/series/LNS14027660> |
| `LNS14027659` | Unemployment, less than a high school diploma, 25+ (SA) | <https://fred.stlouisfed.org/series/LNS14027659> |
| `CGBD2024` | Unemployment, bachelor's-only graduates, ages 20-24 (NSA) | <https://fred.stlouisfed.org/series/CGBD2024> |
| `CGBD2534` | Unemployment, bachelor's-only graduates, ages 25-34 (NSA) | <https://fred.stlouisfed.org/series/CGBD2534> |
| `UNRATE` | Unemployment, all civilian workers, 16+ (SA) | <https://fred.stlouisfed.org/series/UNRATE> |
| `CPIAUCSL` | Consumer Price Index, all urban consumers | <https://fred.stlouisfed.org/series/CPIAUCSL> |

Coverage runs through September 2026 (from 1992 for the education series, 2000 for `CGBD`, 1947-48 for `UNRATE` and `CPIAUCSL`). Every series is missing October 2025; missing months are dropped, not estimated. FRED rates (CPS, monthly) and `major_outcomes.unemployment_rate_pct` (ACS 2024) measure unemployment differently and are never combined.

### What is in `data/`

- `majors.duckdb`: the committed database with all five sources. This is all the app reads.
- `labels/`: major and occupation code lists from the Census PUMS dictionary, and the crosswalk.
- `raw/`: only the small raw inputs. The large raw files (the ACS download, the Scorecard CSV, the NSCG zip) are **not committed**: GitHub rejects files over 100 MB, and `build_data.py` shows exactly how each is obtained.

To rebuild: put the BLS workbook and the Scorecard CSV in `data/raw/`, then

```bash
uv sync --group build
uv run --group build build_data.py                 # national ACS (~575 MB download)
uv run --group build build_data.py --states ny,ca  # quick test build, NOT national
```

## Sample queries

Each has one answer computable from one table. The expected answers come from running the SQL shown against `data/majors.duckdb`.

| # | Query | Expected answer | Source table |
|---|---|---|---|
| 1 | Among majors with at least 100 sampled full-time earners, which has the highest median earnings? | **Computer Engineering and Electrical Engineering, tied at $121,830** | `major_outcomes` (ACS 2024) |
| 2 | Among fields with at least 100 survey respondents, which has the highest share of employed graduates in a job not related to their degree? | **Anthropology and archaeology, 56.8%** (217 respondents) | `nscg_field_experience` (NSCG 2023) |
| 3 | Among majors with median earnings of at least $100,000 and at least 100 respondents in both the earnings data and the graduate survey, which has the lowest share of graduates very satisfied with their job? | **Statistics And Decision Science, 30.0%** (NSCG Statistics, match quality "same"; 101 NSCG respondents) | `major_pay_vs_experience` (ACS + NSCG via crosswalk) |

```sql
-- 1
SELECT major, median_earnings FROM major_outcomes
WHERE earnings_sample_size >= 100 ORDER BY median_earnings DESC LIMIT 2;
-- 2
SELECT field, pct_job_not_related, sample_size FROM nscg_field_experience
WHERE sample_size >= 100 ORDER BY pct_job_not_related DESC LIMIT 1;
-- 3
SELECT major, pct_very_satisfied FROM major_pay_vs_experience
WHERE median_earnings >= 100000 AND earnings_sample_size >= 100 AND nscg_sample_size >= 100
ORDER BY pct_very_satisfied ASC LIMIT 1;
```

Query 3's NSCG sample (101) sits just above the 100 cutoff; the next major is Information Sciences at 38.4%.

Also worth trying: a vague question ("Is engineering a good major?") should return a clarifying question, and "Do graduates regret their major?" should be answered with a statement that the data cannot say.

## Agent vs. same model with no tools and no system prompt

| # | Agent | `gemini-3.5-flash`, no tools, no system prompt |
|---|---|---|
| 1 | Computer Engineering and Electrical Engineering, tied at $121,830. Correct. Also lists College Scorecard's top field (Law, $142,745) as a second lens. | Chemical Engineering, $65,000, from the FiveThirtyEight dataset (ACS 2010-2012). Wrong. |
| 2 | Anthropology and archaeology, 56.8% (217 respondents). Correct. Adds the top reason for leaving the field, per field. | Three different answers from three surveys; for NSCG, "Arts / Dramatic Arts, 36.1%". Wrong. Needed 2 follow-up prompts. |
| 3 | Statistics And Decision Science, 30.0%. Correct. Also offers two side answers from mixed sources (Chemical Engineering 41.6%, Law 31.3%). | Computer Science. Wrong, no figure. Needed 4 follow-up prompts. |

Agent answers are from the deployed site. Plain-model answers are from Vertex AI Studio, `gemini-3.5-flash`, no system prompt. Its answers to 1 and 2 cite Google Search results, so search grounding was on for those two.

## Limits we state to users

- ACS is a sample: rankings require 100+ sampled earners, and `low_sample_warning` flags the rest (26 of 174 majors). Standard errors are not computed.
- ACS earnings are wages only (no self-employment income) and assign each graduate to their first major.
- "Five years after graduation" has two lenses that do not measure the same thing: Scorecard `national_median_earnings_5yr` follows real graduates but only federal-aid recipients, and each horizon is a different graduating cohort; ACS `earnings_by_age` compares different people at different ages (25-29 is roughly 3-7 years after a typical graduation).
- NSCG satisfaction is satisfaction with the current job among people who have one, self-reported in February 2023. It is not regret about the major, and not a prediction for any one student. Many humanities, arts and business fields are thin in NSCG; the agent falls back to broad field groups and says so.
- Scorecard fields (CIP codes) and NSCG fields are matched to ACS majors by description or crosswalk, never exactly.
- Differences between majors are descriptive, not proof that a major causes them.

## Run and deploy

```bash
gcloud auth application-default login
uv run app.py            # http://127.0.0.1:8000
```

Environment variables: `MODEL_NAME` (Lead and Writer, default `vertex_ai/gemini-3.5-flash`), `RESEARCHER_MODEL_NAME` (default `vertex_ai/gemini-3.5-flash-lite`), `VERTEXAI_PROJECT`.

Deployed to **Cloud Run** with continuous deploy from this GitHub repo (Dockerfile build; request timeout 300 s or more; max instances 1, because chat history is kept in the container; service account with the Vertex AI User role).

## Repo layout

`app.py` (agents and API) · `context_panel.py`, `context-panel.js` (agent timeline panel, from the course's Wikipedia deep-research template) · `index.html` (front end) · `build_data.py` (offline build) · `data/majors.duckdb`, `data/labels/` · `Dockerfile` · `pyproject.toml`, `uv.lock` · `submission.json`
