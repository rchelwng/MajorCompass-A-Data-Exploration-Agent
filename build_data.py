"""Build data/majors.duckdb from four public sources. Run once, offline, before app.py.

    uv sync --group build
    uv run --group build build_data.py                  # national ACS file (~575 MB download)
    uv run --group build build_data.py --states ny,ca   # quick test from state files only (NOT national)

Auto-downloaded (cached in data/raw/):
  * Census ACS 2024 1-year PUMS person file (+ field-of-degree and occupation labels)
  * FRED unemployment-by-education series and CPI
Download by hand into data/raw/ (these sites block scripted downloads):
  * College Scorecard "Most Recent Data by Field of Study" zip -> unzip to data/raw/ (needs Most-Recent-Cohorts-Field-of-Study.csv)
        https://collegescorecard.ed.gov/data/   (button: Most Recent Data by Field of Study)
  * BLS Employment Projections, Table 1.2 workbook -> data/raw/bls_occupation.xlsx
        https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx  (linked from
        https://www.bls.gov/emp/tables/occupational-projections-and-characteristics.htm)
"""

import json
import re
import urllib.request
import zipfile
from pathlib import Path

import duckdb

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "majors.duckdb"

YEAR = 2024  # latest ACS 1-year PUMS released (2025 1-year PUMS: no date announced as of Nov 2026)
PUMS_URL = f"https://www2.census.gov/programs-surveys/acs/data/pums/{YEAR}/1-Year/csv_pus.zip"
LABEL_URL = "https://api.census.gov/data/{year}/acs/acs1/pums/variables/{var}.json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
UA = {"User-Agent": "Mozilla/5.0 (course project)"}

FRED_SERIES = {
    "LNS14027662": "Unemployment rate, bachelor's degree and higher, 25+ (monthly, %, seasonally adjusted)",
    "LNS14027689": "Unemployment rate, some college or associate degree, 25+ (monthly, %, seasonally adjusted)",
    "LNS14027660": "Unemployment rate, high school graduates no college, 25+ (monthly, %, seasonally adjusted)",
    "LNS14027659": "Unemployment rate, less than a high school diploma, 25+ (monthly, %, seasonally adjusted)",
    "CGBD2024": "Unemployment rate, college graduates with a bachelor's degree only, ages 20-24 (monthly, %, not seasonally adjusted)",
    "CGBD2534": "Unemployment rate, college graduates with a bachelor's degree only, ages 25-34 (monthly, %, not seasonally adjusted)",
    "UNRATE": "Unemployment rate, all civilian workers 16+ (monthly, %, seasonally adjusted)",
    "CPIAUCSL": "Consumer Price Index for All Urban Consumers, all items (monthly index, 1982-84=100)",
}

# (label, min age, max age). Bands overlap on purpose for the last two: 25-34 and 25-64 are summary rows.
AGE_BANDS = [("22-24", 22, 24), ("25-29", 25, 29), ("30-34", 30, 34), ("35-44", 35, 44),
             ("45-54", 45, 54), ("55-64", 55, 64), ("25-34", 25, 34), ("25-64", 25, 64)]


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print("Downloading", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=900) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.rename(dest)
    return dest


LABELS = Path(__file__).parent / "data" / "labels"  # code lists parsed from the official 2020-2024 PUMS data dictionary


def census_labels(var: str) -> dict[str, str]:
    """Code -> label for a PUMS variable. Census API for this year first; the bundled dictionary copy if the API is unreachable."""
    try:
        path = download(LABEL_URL.format(year=YEAR, var=var), RAW / f"{var}.json")
        return dict(json.loads(path.read_text())["values"]["item"])
    except Exception as e:  # network down, 404, or an unexpected shape: fall back, loudly
        local = LABELS / f"{var.lower()}.csv"
        print(f"Census API labels for {var} unavailable ({type(e).__name__}); using bundled {local.name}")
        (RAW / f"{var}.json").unlink(missing_ok=True)
        rows = con_read_labels(local)
        return rows


def con_read_labels(path: Path) -> dict[str, str]:
    import csv
    with open(path, newline="") as f:
        return {r["code"]: r["label"] for r in csv.DictReader(f)}


def weighted_quantiles(src: str, keys: list[str]) -> str:
    """SQL for weighted p10/p25/p50/p75/p90 of `wage` per `keys`, over a source query with columns keys, wage, w."""
    k = ", ".join(keys)
    qs = ", ".join(f"min(wage) FILTER (WHERE cum >= {q} * tot) AS p{int(q * 100)}" for q in (0.1, 0.25, 0.5, 0.75, 0.9))
    return f"""
        SELECT {k}, count(*) AS n, sum(w) AS pop, {qs}
        FROM (SELECT {k}, wage, w,
                     sum(w) OVER (PARTITION BY {k} ORDER BY wage ROWS UNBOUNDED PRECEDING) AS cum,
                     sum(w) OVER (PARTITION BY {k}) AS tot
              FROM ({src}))
        GROUP BY {k}"""


def load_pums(con, states: list[str] | None = None):
    """states=None: the national file (~575 MB zip). states=['ny','ca']: only those state files (much smaller,
    but NOT nationally representative: for a quick test run only)."""
    pattern = "psam_pus*.csv" if not states else "psam_p??.csv"
    files = sorted(RAW.glob(pattern))
    if states:
        files = [f for f in RAW.glob("psam_p??.csv") if f.stem[-2:] in states]
    if not files:
        for st in (states or ["us"]):
            zpath = download(PUMS_URL.replace("csv_pus.zip", f"csv_p{st}.zip"), RAW / f"csv_p{st}.zip")
            with zipfile.ZipFile(zpath) as z:
                z.extractall(RAW, [n for n in z.namelist() if n.lower().endswith(".csv")])
        files = sorted(RAW.glob(pattern)) if not states else [f for f in RAW.glob("psam_p??.csv") if f.stem[-2:] in states]
    if states:
        print("WARNING: building from state files only:", states, "- results are not national")
    print("Reading", [f.name for f in files])
    # Bachelor's holders only (SCHL 21), ages 22-64. wage = WAGP in constant 2024 dollars (ADJINC is scaled by 1e6).
    con.execute(f"""
        CREATE TABLE p AS
        SELECT FOD1P AS fod,
               TRY_CAST(SEX AS INT) AS sex, TRY_CAST(AGEP AS INT) AS age, TRY_CAST(ESR AS INT) AS esr,
               TRY_CAST(WKHP AS INT) AS hours, TRY_CAST(WKWN AS INT) AS weeks,
               TRY_CAST(WAGP AS DOUBLE) * TRY_CAST(ADJINC AS DOUBLE) / 1e6 AS wage,
               SOCP AS socp, TRY_CAST(PWGTP AS DOUBLE) AS w
        FROM read_csv({[str(f) for f in files]}, header = true, all_varchar = true, union_by_name = true)
        WHERE TRY_CAST(SCHL AS INT) = 21 AND FOD1P IS NOT NULL AND FOD1P <> ''
          AND TRY_CAST(AGEP AS INT) BETWEEN 22 AND 64
    """)
    con.execute("ALTER TABLE p ADD COLUMN ft BOOLEAN")
    con.execute("UPDATE p SET ft = (hours >= 35 AND weeks >= 50 AND wage > 0)")  # full-time, year-round wage earner
    print("Bachelor's holders 22-64 in sample:", con.execute("SELECT count(*) FROM p").fetchone()[0])

    fod, soc = census_labels("FOD1P"), census_labels("SOCP")
    con.execute("CREATE TABLE fod_labels (code VARCHAR, label VARCHAR)")
    con.executemany("INSERT INTO fod_labels VALUES (?, ?)", list(fod.items()))
    con.execute("CREATE TABLE soc_labels (code VARCHAR, label VARCHAR)")
    con.executemany("INSERT INTO soc_labels VALUES (?, ?)", list(soc.items()))

    # Headline table: one row per major.
    con.execute("CREATE TEMP TABLE q_all AS " + weighted_quantiles("SELECT fod, wage, w FROM p WHERE ft AND age BETWEEN 25 AND 64", ["fod"]))
    con.execute("CREATE TEMP TABLE q_sex AS " + weighted_quantiles("SELECT fod, sex, wage, w FROM p WHERE ft AND age BETWEEN 25 AND 64", ["fod", "sex"]))
    con.execute("""
        CREATE TEMP TABLE shares AS
        SELECT fod, count(*) AS n_all, sum(w) AS pop,
               sum(w) FILTER (WHERE sex = 2) / sum(w) AS pct_female,
               sum(w) FILTER (WHERE esr = 3) / NULLIF(sum(w) FILTER (WHERE esr BETWEEN 1 AND 5), 0) AS unemp,
               sum(w) FILTER (WHERE esr BETWEEN 1 AND 5) / sum(w) AS lfp
        FROM p WHERE age BETWEEN 25 AND 64 GROUP BY fod
    """)
    con.execute("""
        CREATE TABLE major_outcomes AS
        SELECT l.label AS major, s.fod AS acs_code,
               s.n_all AS sample_size, round(s.pop) AS est_graduates_25_64,
               round(100 * s.pct_female, 1) AS pct_female,
               round(100 * s.unemp, 2) AS unemployment_rate_pct,
               round(100 * s.lfp, 1) AS labor_force_participation_pct,
               a.n AS earnings_sample_size,
               round(a.p50) AS median_earnings, round(a.p25) AS p25_earnings, round(a.p75) AS p75_earnings,
               round(f.p50) AS median_earnings_women, round(m.p50) AS median_earnings_men,
               f.n AS n_women_full_time, m.n AS n_men_full_time,
               CASE WHEN f.n >= 30 AND m.n >= 30 THEN round(100.0 * (m.p50 - f.p50) / m.p50, 1) END AS gender_gap_pct,
               (a.n IS NULL OR a.n < 100) AS low_sample_warning
        FROM shares s
        JOIN fod_labels l ON l.code = s.fod
        LEFT JOIN q_all a ON a.fod = s.fod
        LEFT JOIN q_sex f ON f.fod = s.fod AND f.sex = 2
        LEFT JOIN q_sex m ON m.fod = s.fod AND m.sex = 1
    """)

    # Earnings by age band (a cross-section: different people at each age, not one cohort followed over time).
    src = " UNION ALL ".join(
        f"SELECT fod, '{b}' AS age_band, wage, w FROM p WHERE ft AND age BETWEEN {lo} AND {hi}" for b, lo, hi in AGE_BANDS
    )
    con.execute("CREATE TEMP TABLE q_age AS " + weighted_quantiles(src, ["fod", "age_band"]))
    con.execute("""
        CREATE TABLE earnings_by_age AS
        SELECT l.label AS major, q.fod AS acs_code, q.age_band, q.n AS sample_size, round(q.pop) AS est_workers,
               round(q.p10) AS p10_earnings, round(q.p25) AS p25_earnings, round(q.p50) AS median_earnings,
               round(q.p75) AS p75_earnings, round(q.p90) AS p90_earnings
        FROM q_age q JOIN fod_labels l ON l.code = q.fod
    """)
    con.execute("""
        CREATE VIEW earnings_growth AS
        SELECT major, acs_code,
               max(median_earnings) FILTER (WHERE age_band = '22-24') AS median_22_24,
               max(median_earnings) FILTER (WHERE age_band = '25-29') AS median_25_29,
               max(median_earnings) FILTER (WHERE age_band = '30-34') AS median_30_34,
               max(median_earnings) FILTER (WHERE age_band = '35-44') AS median_35_44,
               max(median_earnings) FILTER (WHERE age_band = '45-54') AS median_45_54,
               min(sample_size) FILTER (WHERE age_band IN ('25-29', '35-44')) AS min_sample_25_29_and_35_44,
               round(100.0 * (max(median_earnings) FILTER (WHERE age_band = '35-44')
                     / NULLIF(max(median_earnings) FILTER (WHERE age_band = '25-29'), 0) - 1), 1) AS pct_growth_25_29_to_35_44
        FROM earnings_by_age GROUP BY major, acs_code
    """)


def load_bls(con):
    import pandas as pd

    path = next((p for p in (RAW / "bls_occupation.xlsx", RAW / "occupation.xlsx") if p.exists()), None)
    if path is None:
        raise SystemExit("Missing data/raw/bls_occupation.xlsx. Download the BLS Employment Projections workbook (see top of build_data.py).")
    xl = pd.ExcelFile(path)
    sheet = next((n for n in xl.sheet_names if re.fullmatch(r"Table 1\.2", n.strip())), None)
    if sheet is None:
        raise SystemExit(f"BLS workbook: no 'Table 1.2' sheet. Sheets found: {xl.sheet_names}")
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    hdr = next(i for i, row in raw.iterrows()
               if any("code" in str(c).lower() for c in row) and any("title" in str(c).lower() for c in row))
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [re.sub(r"\s+", " ", str(c)).strip().lower() for c in raw.iloc[hdr]]

    def col(pattern):
        hits = [c for c in df.columns if re.search(pattern, c)]
        if not hits:
            raise SystemExit(f"BLS workbook: no column matching {pattern!r}. Columns: {list(df.columns)}")
        return hits[0]

    years = sorted((re.search(r"employment,? ?(\d{4})$", c).group(1), c) for c in df.columns if re.search(r"employment,? ?(\d{4})$", c))
    if len(years) < 2:
        raise SystemExit(f"BLS workbook: could not find base/projection employment columns. Columns: {list(df.columns)}")
    (y0, c0), (y1, c1) = years[0], years[-1]
    if any("occupation type" in c for c in df.columns):
        df = df[df[col("occupation type")].astype(str).str.strip().str.lower() == "line item"]
    out = pd.DataFrame({
        "code": df[col(r"code")].astype(str).str.strip(),
        "title": df[col(r"title")],
        "emp_base": pd.to_numeric(df[c0], errors="coerce"),
        "emp_proj": pd.to_numeric(df[c1], errors="coerce"),
        "wage": pd.to_numeric(df[col(r"median annual wage")], errors="coerce"),
    })
    out = out[out.code.str.fullmatch(r"\d\d-\d{4}") & (out.emp_base > 0)]
    con.register("bls_df", out)
    con.execute("CREATE TEMP TABLE bls AS SELECT * FROM bls_df")
    # ACS occupation codes (SOCP) use X as a wildcard for grouped SOC codes, e.g. 1110XX. Map each BLS occupation to its
    # MOST SPECIFIC ACS code: an exact 6-digit match wins, otherwise the wildcard code with the fewest X's. (A plain
    # wildcard match would also pull 11-1021 into 1110XX, although 111021 is its own ACS code.)
    con.execute(f"""
        CREATE TABLE occupation_outlook AS
        WITH m AS (
            SELECT l.code AS socp, b.*
            FROM soc_labels l JOIN bls b ON replace(b.code, '-', '') LIKE replace(l.code, 'X', '_')
            QUALIFY row_number() OVER (PARTITION BY b.code
                                       ORDER BY (replace(b.code, '-', '') = l.code) DESC, length(l.code) - length(replace(l.code, 'X', '')) ASC) = 1)
        SELECT socp,
               count(*) AS n_bls_occupations,
               round(sum(emp_base)) AS emp_{y0}_thousands, round(sum(emp_proj)) AS emp_{y1}_thousands,
               round(100.0 * (sum(emp_proj) - sum(emp_base)) / sum(emp_base), 1) AS projected_growth_pct,
               round(sum(wage * emp_base) FILTER (WHERE wage IS NOT NULL) / sum(emp_base) FILTER (WHERE wage IS NOT NULL)) AS avg_median_wage_employment_weighted
        FROM m WHERE socp IN (SELECT DISTINCT socp FROM p WHERE socp IS NOT NULL)
        GROUP BY socp
    """)
    con.execute(f"""
        CREATE TABLE major_occupations AS
        WITH t AS (SELECT fod, socp, sum(w) AS pop, count(*) AS n FROM p
                   WHERE age BETWEEN 25 AND 64 AND esr IN (1, 2) AND socp IS NOT NULL GROUP BY fod, socp),
             tot AS (SELECT fod, sum(pop) AS total FROM t GROUP BY fod)
        SELECT l.label AS major, t.fod AS acs_code, t.socp, s.label AS occupation,
               round(100 * t.pop / tot.total, 1) AS pct_of_employed_graduates, t.n AS sample_size,
               o.projected_growth_pct AS bls_projected_growth_pct_{y0}_{y1},
               o.avg_median_wage_employment_weighted AS bls_avg_median_wage_{y0}
        FROM t JOIN tot ON tot.fod = t.fod
        JOIN fod_labels l ON l.code = t.fod
        LEFT JOIN soc_labels s ON s.code = t.socp
        LEFT JOIN occupation_outlook o ON o.socp = t.socp
        QUALIFY row_number() OVER (PARTITION BY t.fod ORDER BY t.pop DESC) <= 15
    """)
    return y0, y1


def load_scorecard(con):
    files = sorted(RAW.glob("**/Most-Recent-Cohorts-Field-of-Study*.csv"))
    if not files:
        raise SystemExit("Missing College Scorecard Field-of-Study CSV in data/raw/ (see top of build_data.py).")
    f = files[0]
    nulls = "['NULL', 'PrivacySuppressed', 'PS', 'NA', '']"
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_csv('{f}', all_varchar = true, nullstr = {nulls}, sample_size = 5000)").fetchall()}
    need = {"CIPCODE", "CIPDESC", "CREDLEV"}
    if need - have:
        raise SystemExit(f"Scorecard file is missing columns {sorted(need - have)}; check the schema of {f.name}.")

    # One row per 4-digit CIP, bachelor's (CREDLEV 3). The file carries NATIONAL earnings per field (the *_NAT columns:
    # constant within a CIP + credential), so use those for the headline figures; they are not derived from program rows.
    # Horizons: 1, 4, 5, 7 years after completion. Medians for all; P25/P75 where the file has them.
    out, found = [], []
    for yrs in (1, 4, 5, 7):
        for stat, label in (("MDN", "median"), ("P25", "p25"), ("P75", "p75")):
            col = f"EARN_{stat}_{yrs}YR_NAT"
            if col in have:
                out.append(f"any_value(TRY_CAST({col} AS DOUBLE)) AS national_{label}_earnings_{yrs}yr")
                found.append(col)
        cnt = f"EARN_COUNT_WNE_{yrs}YR_NAT"
        if cnt in have:
            out.append(f"any_value(TRY_CAST({cnt} AS DOUBLE)) AS national_graduates_{yrs}yr")
            found.append(cnt)
    if not any("median" in o for o in out):
        raise SystemExit(f"Scorecard file has no EARN_MDN_<n>YR_NAT columns. Columns containing 'EARN': {sorted(c for c in have if 'EARN' in c)}")
    print("Scorecard national columns used:", found)
    debt = "TRY_CAST(DEBT_ALL_STGP_EVAL_MDN AS DOUBLE)" if "DEBT_ALL_STGP_EVAL_MDN" in have else "NULL::DOUBLE"
    con.execute(f"""
        CREATE TABLE scorecard_programs AS
        SELECT lpad(CIPCODE, 4, '0') AS cip_code,
               regexp_replace(any_value(CIPDESC), '\\.$', '') AS cip_description,
               count(DISTINCT UNITID) AS n_institutions,
               {", ".join(out)},
               round(median({debt})) AS median_of_program_median_debt
        FROM read_csv('{f}', all_varchar = true, nullstr = {nulls})
        WHERE CREDLEV = '3'
        GROUP BY lpad(CIPCODE, 4, '0')
    """)


def load_fred(con):
    con.execute("CREATE TABLE fred_series (series_id VARCHAR, description VARCHAR, date DATE, value DOUBLE)")
    for sid, desc in FRED_SERIES.items():
        local = sorted(RAW.glob(f"fred_{sid}.csv")) + sorted(RAW.glob(f"{sid}*.csv"))  # e.g. LNS14027662_-_bachelors_and_higher.csv
        try:
            path = local[0] if local else download(FRED_URL.format(id=sid), RAW / f"fred_{sid}.csv")
        except Exception as e:
            print(f"WARNING: FRED {sid} not loaded ({type(e).__name__}). Put its CSV in data/raw/ to include it.")
            continue
        con.execute(f"""
            INSERT INTO fred_series
            SELECT ?, ?, TRY_CAST(date AS DATE), TRY_CAST(value AS DOUBLE)
            FROM read_csv('{path}', header = true, names = ['date', 'value'], all_varchar = true)
            WHERE TRY_CAST(value AS DOUBLE) IS NOT NULL
        """, [sid, desc])
    print("FRED series loaded:", [r[0] for r in con.execute("SELECT DISTINCT series_id FROM fred_series").fetchall()])


def write_dictionary(con, y0, y1):
    rows = [
        ("(all)", "", "CONVENTIONS. 'Earnings' = annual wages and salary (ACS WAGP) of full-time (35+ hrs/wk), year-round (50+ wks) workers, in constant 2024 dollars (ADJINC applied), "
                      "for people whose HIGHEST degree is a bachelor's. Excludes self-employment income. Medians/percentiles are survey-weighted (PWGTP). "
                      "Unemployment = unemployed / labor force (ACS ESR) among bachelor's holders 25-64 in 2024. These are point estimates from a sample: check sample_size; below 100 earners is unreliable."),
        ("major_outcomes", "major", "ACS field of degree (first major, FOD1P) label."),
        ("major_outcomes", "sample_size", "Unweighted ACS respondents (bachelor's, 25-64) in this major, all employment statuses."),
        ("major_outcomes", "est_graduates_25_64", "Survey-weighted number of bachelor's holders aged 25-64 with this major."),
        ("major_outcomes", "pct_female", "Share of those graduates who are female (%)."),
        ("major_outcomes", "unemployment_rate_pct", "Unemployment rate (%) among labor-force participants, ages 25-64, 2024."),
        ("major_outcomes", "labor_force_participation_pct", "Share (%) of graduates employed or looking for work."),
        ("major_outcomes", "earnings_sample_size", "Unweighted full-time year-round earners behind the earnings columns."),
        ("major_outcomes", "median_earnings / p25_earnings / p75_earnings", "Weighted median, 25th and 75th percentile of earnings, ages 25-64, 2024 dollars."),
        ("major_outcomes", "median_earnings_women / median_earnings_men", "Median earnings by sex (ACS SEX is binary)."),
        ("major_outcomes", "gender_gap_pct", "(men's median - women's median) / men's median * 100. Positive = men earn more. NULL unless 30+ full-time men AND women. NOT adjusted for occupation, hours or experience."),
        ("major_outcomes", "low_sample_warning", "TRUE if fewer than 100 full-time earners. Exclude or flag these when ranking."),
        ("earnings_by_age", "age_band", "22-24, 25-29, 30-34, 35-44, 45-54, 55-64 are disjoint bands; 25-34 and 25-64 are summary rows (do not add them to the others). "
                                         "This is a cross-section of different people at each age, not one graduating class followed over time. Age 25-29 is roughly 3-7 years after a typical age-22 graduation: the closest ACS gets to 'five years out'."),
        ("earnings_by_age", "median_earnings, p10..p90_earnings, sample_size, est_workers", "Weighted earnings quantiles per major and age band (same definition as major_outcomes). Check sample_size per band."),
        ("earnings_growth", "pct_growth_25_29_to_35_44", "VIEW over earnings_by_age: % rise in median earnings from ages 25-29 to ages 35-44 (cross-sectional). min_sample_25_29_and_35_44 is the smaller of the two samples."),
        ("major_occupations", "(all)", f"Top 15 occupations (ACS SOCP) of employed bachelor's holders 25-64 per major, with BLS Employment Projections {y0}-{y1} growth and wage for that occupation group where the codes match."),
        ("major_occupations", "pct_of_employed_graduates", "Share (%) of this major's employed graduates working in the occupation."),
        ("occupation_outlook", "(all)", f"BLS Employment Projections {y0}-{y1} aggregated to ACS occupation codes: projected_growth_pct and avg_median_wage_employment_weighted (an employment-weighted AVERAGE of the member occupations' median annual wages, not a true median). Employment is in thousands. Projections are forecasts, not outcomes."),
        ("scorecard_programs", "(all)", "College Scorecard Field-of-Study data, bachelor's programs (CREDLEV 3), one row per 4-digit CIP code. "
                                         "national_median/p25/p75_earnings_Nyr = the file's NATIONAL earnings for graduates in that field, N = 1, 4, 5, 7 years after completion (p25/p75 where available): "
                                         "use national_median_earnings_5yr as the direct measure of 'five years after graduation' and compare it with national_median_earnings_1yr for early growth. "
                                         "Cohorts: only graduates who received federal student aid, who were working and not enrolled in the measurement year; earnings = W-2 wages plus positive self-employment income, inflation-adjusted; "
                                         "values are noise-perturbed for privacy and missing for small fields. Each horizon is a different pooled graduating cohort measured in different calendar years, so 1yr-vs-5yr differences mix cohort and time effects. "
                                         "national_graduates_Nyr = graduates working and not enrolled behind the figure; n_institutions = institutions offering the field. "
                                         "median_of_program_median_debt = median across institutions of each program's median federal-loan debt. "
                                         "Uses CIP codes, not ACS majors: match by description (use find_majors) and say the match is approximate."),
        ("fred_series", "(all)", "FRED monthly series (series_id, date, value): unemployment by education level (BLS CPS, ages 25+), unemployment of bachelor's-only graduates aged 20-24 (CGBD2024) and 25-34 (CGBD2534), overall unemployment (UNRATE), and CPI (CPIAUCSL). Labor-market context, NOT by major. Some months can be missing (e.g. Oct 2025)."),
    ]
    con.execute("CREATE TABLE data_dictionary (table_name VARCHAR, column_name VARCHAR, description VARCHAR)")
    con.executemany("INSERT INTO data_dictionary VALUES (?, ?, ?)", rows)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", help="comma-separated state codes (ny,ca,...) for a quick, non-national test build")
    args = ap.parse_args()
    DB_PATH.unlink(missing_ok=True)
    DATA.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    load_pums(con, [x.strip().lower() for x in args.states.split(",")] if args.states else None)
    y0, y1 = load_bls(con)
    load_scorecard(con)
    load_fred(con)
    write_dictionary(con, y0, y1)
    con.execute("DROP TABLE p")
    con.execute("DROP TABLE fod_labels")
    con.execute("DROP TABLE soc_labels")
    for t in ("major_outcomes", "earnings_by_age", "major_occupations", "occupation_outlook", "scorecard_programs", "fred_series"):
        print(f"{t}: {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]} rows")
    con.close()
    print(f"Built {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
