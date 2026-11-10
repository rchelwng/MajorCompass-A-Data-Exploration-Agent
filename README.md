# MajorCompass

A deep-research agent (Lead, parallel Researchers, Writer) that checks whether college majors deliver the pay and jobs students expect. US, bachelor's degrees.

**Target user(s):** students, families and academic advisors choosing a major.

## Data

Five public sources, loaded by `build_data.py` into `data/majors.duckdb` (committed).

| Source | Where it comes from | Tables |
|---|---|---|
| Census ACS 2024 PUMS | Auto-downloaded: <https://www2.census.gov/programs-surveys/acs/data/pums/2024/1-Year/csv_pus.zip> | `major_outcomes`, `earnings_by_age`, `earnings_growth`, `major_occupations` |
| BLS Employment Projections 2025-35 | Manual: <https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx> to `data/raw/bls_occupation.xlsx` | `occupation_outlook` |
| College Scorecard, Field of Study | Manual: <https://collegescorecard.ed.gov/data/>, unzip into `data/raw/` | `scorecard_programs` |
| FRED (Federal Reserve Bank of St. Louis) | CSV exports in `data/raw/` (eight series, listed below) | `fred_series` |
| NSF National Survey of College Graduates 2023 | Auto-downloaded: <https://ncses.nsf.gov/822/assets/0/files/college_grads_2023.zip> | `nscg_field_experience`, `nscg_group_experience`, `nscg_job_facets`, `nscg_outside_field_reasons`, `major_pay_vs_experience` |

```bash
uv run --group build build_data.py
```

### FRED: labor-market context

FRED supplies the national, month-by-month unemployment picture the other sources can't: how graduates fare against non-graduates, and how the market for recent graduates is moving. It is **context, not major-level data**. FRED has no breakdown by major, so the agent uses it to frame a question, never to rank majors.

| Series ID | What it measures | File in `data/raw/` | FRED page |
|---|---|---|---|
| `LNS14027662` | Unemployment rate, bachelor's degree and higher, 25+ (SA) | `LNS14027662 - bachelors and higher.csv` | [series](https://fred.stlouisfed.org/series/LNS14027662) |
| `LNS14027689` | Unemployment rate, some college or associate degree, 25+ (SA) | `fred_LNS14027689.csv` | [series](https://fred.stlouisfed.org/series/LNS14027689) |
| `LNS14027660` | Unemployment rate, high school graduates, no college, 25+ (SA) | `LNS14027660 - high school.csv` | [series](https://fred.stlouisfed.org/series/LNS14027660) |
| `LNS14027659` | Unemployment rate, less than a high school diploma, 25+ (SA) | `LNS14027659 - less than high school.csv` | [series](https://fred.stlouisfed.org/series/LNS14027659) |
| `CGBD2024` | Unemployment rate, bachelor's-only graduates, ages 20-24 (NSA) | `fred_CGBD2024.csv` | [series](https://fred.stlouisfed.org/series/CGBD2024) |
| `CGBD2534` | Unemployment rate, bachelor's-only graduates, ages 25-34 (NSA) | `CGBD2534 - bachelors only grad.csv` | [series](https://fred.stlouisfed.org/series/CGBD2534) |
| `UNRATE` | Unemployment rate, all civilian workers, 16+ (SA) | `UNRATE.csv` | [series](https://fred.stlouisfed.org/series/UNRATE) |
| `CPIAUCSL` | Consumer Price Index, all urban consumers (SA) | `CPIAUCSL.csv` | [series](https://fred.stlouisfed.org/series/CPIAUCSL) |

SA = seasonally adjusted, NSA = not seasonally adjusted. All unemployment series come from the Bureau of Labor Statistics' monthly Current Population Survey (CPS), republished by FRED. Each file is a FRED "Download data → CSV" export (header `observation_date,<SERIES_ID>`, one row per month, `.` where FRED has no value; October 2025 in every series). Coverage runs from 1992 (education series), 2000 (`CGBD2534`) or 1948 (`UNRATE`) through September 2026.

**How it is loaded.** `build_data.py` reads any CSV in `data/raw/` whose name starts with the series ID or `fred_<SERIES_ID>` and writes one long table, `fred_series(series_id, description, date, value)`, one row per series per month. Missing months are dropped, not estimated.

**How the agent uses it.** A Researcher queries `fred_series` for questions like "Do graduates have lower unemployment than non-graduates?" or "How is the job market for recent graduates?", and the Writer cites the exact query as evidence like any other source.

**Limits the agent is told to state**
- **Different survey, different number.** FRED's rates come from the monthly CPS; `major_outcomes.unemployment_rate_pct` comes from the 2024 ACS. They measure unemployment differently over different periods, so they are never compared or combined.
- **Seasonal adjustment.** The two `CGBD` series are not seasonally adjusted, so compare them across the same month in different years.
- **Not by major.** Nothing in FRED says which major a graduate held.
- **CPI.** `CPIAUCSL` is for converting dollars between years. ACS earnings are already in constant 2024 dollars.

## Sample queries

| # | Query | Expected answer | Source table |
|---|---|---|---|
| 1 | Among majors with at least 100 sampled full-time earners, which has the highest median earnings? | Computer Engineering and Electrical Engineering, tied at $121,830 | `major_outcomes` |
| 2 | Among fields with at least 100 survey respondents, which has the highest share of employed graduates in a job not related to their degree? | Anthropology and archaeology, 56.8% | `nscg_field_experience` |
| 3 | Among majors with median earnings of at least $100,000 and at least 100 respondents in both the earnings data and the graduate survey, which has the lowest share of graduates very satisfied with their job? | Statistics And Decision Science, 30.0% | `major_pay_vs_experience` |

## Agent vs. same model with no tools and no system prompt

(your comparison table and the note underneath, unchanged)
