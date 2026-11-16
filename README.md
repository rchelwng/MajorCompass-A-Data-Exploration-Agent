# MajorCompass

A **deep-research agent** that checks whether US bachelor's majors deliver the pay and jobs students expect. A Lead plans the question, several Researchers query the data in parallel, and a Writer produces a short answer in which every number is cited.

- **Target users:** students, families and academic advisors choosing a major who want evidence: earnings, unemployment, whether graduates work in their field, and how satisfied they are.
- **Path:** Deep Research · **Live site:** <https://majorcompass.cloud.run/> · **Team:** rw3024, mcr2235

## How it works

| Agent | Model | Job |
|---|---|---|
| **Lead** | `gemini-3.5-flash` | Asks a clarifying question if the input is vague. Otherwise splits it into 2-5 tasks, calls `research` once per task **in the same turn**, and goes back for gaps |
| **Researcher 1..N** | `gemini-3.5-flash-lite` | Runs in parallel, each in a **fresh context**. Tools: `describe_data`, `find_majors`, `run_sql` |
| **Writer** | `gemini-3.5-flash` | Writes the answer from the findings and the evidence ledger only |

- **Turn caps:** up to 2 extra research rounds, with hard limits of 8 Researcher runs, 6 Lead turns and 10 turns per Researcher.
- **Citations:** each `run_sql` result is stored under an evidence id (`[E123]`). The server checks that every cited id exists and every number in the answer appears in the cited rows. Anything that fails is listed as "Not verified".
- **States what the data can't answer:** Researchers report `answered | partial | cannot_answer`, and the Writer adds a "What the data can't tell you" section.
- **Shows its work:** `POST /chat` returns `{response, session_id, tool_calls}`, the same shape as Project 1. Every tool call, including each Researcher's, has an `agent` field. The front end shows them under "Show the work" and in a live agent timeline.

## Data

`build_data.py` builds all five sources into `data/majors.duckdb` (7.8 MB, committed, opened read-only). The data is recent (2023-2025 releases), so the model doesn't know it well.

| Source | Where it comes from | Tables |
|---|---|---|
| Census ACS 2024 1-year PUMS | <https://www2.census.gov/programs-surveys/acs/data/pums/2024/1-Year/csv_pus.zip> (auto-downloaded) | `major_outcomes`, `earnings_by_age`, `major_occupations` |
| BLS Employment Projections 2025-35 | <https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx> → `data/raw/bls_occupation.xlsx` | `occupation_outlook` |
| College Scorecard, Field of Study | <https://collegescorecard.ed.gov/data/> → CSV in `data/raw/` | `scorecard_programs` |
| FRED unemployment and CPI | `data/raw/*.csv`, or `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>` | `fred_series` |
| NSF NSCG 2023 | <https://ncses.nsf.gov/822/assets/0/files/college_grads_2023.zip> (auto-downloaded) | `nscg_*` tables |

**FRED series.** These are monthly CPS unemployment rates from the Bureau of Labor Statistics, republished by FRED, plus CPI. They give national context and aren't broken down by major. SA = seasonally adjusted, NSA = not.

| Series | Measures |
|---|---|
| [`LNS14027662`](https://fred.stlouisfed.org/series/LNS14027662) | Unemployment, bachelor's degree and higher, 25+ (SA) |
| [`LNS14027689`](https://fred.stlouisfed.org/series/LNS14027689) | Unemployment, some college or associate degree, 25+ (SA) |
| [`LNS14027660`](https://fred.stlouisfed.org/series/LNS14027660) | Unemployment, high school graduates, no college, 25+ (SA) |
| [`LNS14027659`](https://fred.stlouisfed.org/series/LNS14027659) | Unemployment, less than a high school diploma, 25+ (SA) |
| [`CGBD2024`](https://fred.stlouisfed.org/series/CGBD2024) | Unemployment, bachelor's-only graduates, ages 20-24 (NSA) |
| [`CGBD2534`](https://fred.stlouisfed.org/series/CGBD2534) | Unemployment, bachelor's-only graduates, ages 25-34 (NSA) |
| [`UNRATE`](https://fred.stlouisfed.org/series/UNRATE) | Unemployment, all civilian workers, 16+ (SA) |
| [`CPIAUCSL`](https://fred.stlouisfed.org/series/CPIAUCSL) | Consumer Price Index, all urban consumers |

Coverage runs through September 2026. Every series is missing October 2025, and missing months are dropped, not estimated. FRED (CPS) rates are never combined with the ACS unemployment rates in `major_outcomes` because the two surveys measure unemployment differently.

We hand-built a crosswalk from ACS majors to NSCG fields ([data/labels/acs_nscg_crosswalk.csv](data/labels/acs_nscg_crosswalk.csv)). The `major_pay_vs_experience` view uses it to join pay to graduates' own reports about their jobs.

**To rebuild the database**, put the BLS and Scorecard files in `data/raw/`, then run:

```bash
uv sync --group build
uv run --group build build_data.py
```

## Sample queries

The expected answers come from running the SQL below against `data/majors.duckdb`. The no-tools answers are from `gemini-3.5-flash` in Vertex AI Studio with no system prompt. Search grounding was on for queries 1 and 2.

| # | Query | Expected answer (source) | Agent | Same model, no tools |
|---|---|---|---|---|
| 1 | Among majors with at least 100 sampled full-time earners, which has the highest median earnings? | Computer Engineering and Electrical Engineering, tied at **$121,830** (`major_outcomes`, ACS 2024) | Correct. Adds Scorecard's top field (Law, $142,745) as a second lens | Wrong: Chemical Engineering, $65,000 (FiveThirtyEight, ACS 2010-12) |
| 2 | Among fields with at least 100 survey respondents, which has the highest share of employed graduates in a job not related to their degree? | Anthropology and archaeology, **56.8%** (`nscg_field_experience`, NSCG 2023) | Correct. Adds the top reason graduates give for working outside their field | Wrong: Three answers from three surveys; for NSCG, "Arts / Dramatic Arts, 36.1%". Needed 2 follow-ups |
| 3 | Among majors with median earnings of at least $100,000 and at least 100 respondents in both the earnings data and the graduate survey, which has the lowest share of graduates very satisfied with their job? | Statistics And Decision Science, **30.0%** (`major_pay_vs_experience`, ACS + NSCG) | Correct | Wrong: Computer Science, no figure. Needed 4 follow-ups |

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

Two more to try: "Is engineering a good major?" should get a clarifying question, and "Do graduates regret their major?" should get an answer saying the data can't tell.

## Run and deploy

```bash
gcloud auth application-default login
uv run app.py   # http://127.0.0.1:8000
```

The app is deployed to **Google Cloud Run** from this repo using the Dockerfile. Settings: request timeout ≥ 300 s, max instances 1 (chat history lives in the container), and a service account with the Vertex AI User role.
