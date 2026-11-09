"""Build data/majors.duckdb from five public sources. Run once, offline, before app.py.

    uv sync --group build
    uv run --group build build_data.py                  # national ACS file (~575 MB download)
    uv run --group build build_data.py --states ny,ca   # quick test from state files only (NOT national)
    uv run build_data.py --only nscg                    # add or refresh only the NSCG tables in the existing database (~64 MB download)

Auto-downloaded (cached in data/raw/):
  * Census ACS 2024 1-year PUMS person file (+ field-of-degree and occupation labels)
  * FRED unemployment-by-education series and CPI
  * NSF National Survey of College Graduates (NSCG) 2023 public-use file (job satisfaction, job related to degree, remote work)
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

NSCG_YEAR = 2023  # latest NSCG public-use file (the survey runs every two years; no later file is posted as of Nov 2026)
NSCG_URL = f"https://ncses.nsf.gov/822/assets/0/files/college_grads_{NSCG_YEAR}.zip"
NSCG_MIN_SAMPLE = 30  # fields with fewer employed respondents are dropped; below 100 is kept but flagged
# NSCG broad field groups (NDGMEMG). "S&E" in the official labels is spelled out for groups 6 and 7.
NSCG_GROUPS = {
    "1": "Computer and mathematical sciences",
    "2": "Biological, agricultural and environmental life sciences",
    "3": "Physical and related sciences",
    "4": "Social and related sciences",
    "5": "Engineering",
    "6": "Science and engineering-related fields (health, technology, architecture, STEM teaching)",
    "7": "Non-science and engineering fields (business, education, humanities, arts and others)",
}
# (facet, satisfaction column: question A28, importance column: question C6)
NSCG_FACETS = [
    ("salary", "SATSAL", "FACSAL"), ("benefits", "SATBEN", "FACBEN"), ("job security", "SATSEC", "FACSEC"),
    ("job location", "SATLOC", "FACLOC"), ("opportunities for advancement", "SATADV", "FACADV"),
    ("intellectual challenge", "SATCHAL", "FACCHAL"), ("level of responsibility", "SATRESP", "FACRESP"),
    ("degree of independence", "SATIND", "FACIND"), ("contribution to society", "SATSOC", "FACSOC"),
]
# (code in NRREA: question A23, reason, yes/no column: question A22). Asked only when the job is "not related" to the degree.
NSCG_REASONS = [
    ("1", "pay or promotion opportunities", "NRPAY"), ("2", "working conditions (hours, equipment, environment)", "NRCON"),
    ("3", "job location", "NRLOC"), ("4", "change in career or professional interests", "NRCHG"),
    ("5", "family-related reasons", "NRFAM"), ("6", "no job available in the degree field", "NROCNA"),
    ("7", "other reason", "NROT"),
]

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


def share(cond: str, valid: str) -> str:
    """SQL for a survey-weighted percentage: weight where `cond` holds / weight where the answer is `valid`."""
    return f"round(100.0 * coalesce(sum(w) FILTER (WHERE {cond}), 0) / NULLIF(sum(w) FILTER (WHERE {valid}), 0), 1)"


def nscg_experience(key: str) -> str:
    """SQL for the headline NSCG shares per `key` (an expression over temp table nscg), plus the full-time median salary."""
    related, sat, telec = "related IN ('1', '2', '3')", "sat IN ('1', '2', '3', '4')", "telec IN ('1', '2', '3', '4', '5')"
    salary = weighted_quantiles(
        f"SELECT {key} AS k, salary AS wage, w FROM nscg WHERE hours >= 35 AND weeks >= 50 AND salary BETWEEN 1 AND 9999997", ["k"])
    return f"""
        SELECT e.*, round(s.p50) AS median_salary_full_time, s.n AS salary_sample_size, e.sample_size < 100 AS low_sample_warning
        FROM (SELECT {key} AS k, count(*) AS sample_size, round(sum(w)) AS est_workers,
                     {share("related = '1'", related)} AS pct_job_closely_related,
                     {share("related = '2'", related)} AS pct_job_somewhat_related,
                     {share("related = '3'", related)} AS pct_job_not_related,
                     {share("sat = '1'", sat)} AS pct_very_satisfied,
                     {share("sat IN ('3', '4')", sat)} AS pct_dissatisfied,
                     {share("telec IN ('3', '4')", telec)} AS pct_worked_remotely,
                     {share("telec IN ('3', '4') AND telefr = '4'", telec)} AS pct_remote_5_days_a_week
              FROM nscg GROUP BY 1) e
        LEFT JOIN ({salary}) s ON s.k = e.k"""


def load_nscg(con):
    """NSF National Survey of College Graduates: what graduates themselves say about their job, by field of degree.
    Needs major_outcomes (for the ACS major names in the crosswalk). Safe to re-run: it replaces its own tables."""
    zpath = download(NSCG_URL, RAW / f"college_grads_{NSCG_YEAR}.zip")
    out = RAW / "nscg"
    with zipfile.ZipFile(zpath) as z:
        csv_name = next((n for n in z.namelist() if re.search(r"(^|/)epcg\d\d\.csv$", n, re.I)), None)
        fmt_name = next((n for n in z.namelist() if re.search(r"(^|/)Ppcg\d\d\.sas$", n, re.I)), None)
        if not csv_name or not fmt_name:
            raise SystemExit(f"NSCG zip: expected epcgNN.csv and PpcgNN.sas. Files found: {z.namelist()}")
        for n in (csv_name, fmt_name):
            if not (out / n).exists():
                z.extract(n, out)
    # Field names come from the file's own SAS format list ($EDUCnnF: "116730" = "116730: Computer science").
    block = re.search(r"VALUE \$EDUC\d\dF(.*?)\n\s*;", (out / fmt_name).read_text(encoding="latin-1"), re.S)
    fields = [(c, l) for c, l in re.findall(r'"(\d{6})"\s*=\s*"\d{6}: (.*?)"', block.group(1) if block else "") if not c.startswith("9999")]
    if len(fields) < 100:
        raise SystemExit(f"NSCG: could not read the field-of-study labels from {fmt_name} (found {len(fields)}).")

    con.execute("DROP VIEW IF EXISTS major_pay_vs_experience")  # it reads the tables replaced below
    con.execute("CREATE OR REPLACE TEMP TABLE nscg_labels (code VARCHAR, label VARCHAR)")
    con.executemany("INSERT INTO nscg_labels VALUES (?, ?)", fields)
    con.execute("CREATE OR REPLACE TEMP TABLE nscg_groups (code VARCHAR, label VARCHAR)")
    con.executemany("INSERT INTO nscg_groups VALUES (?, ?)", list(NSCG_GROUPS.items()))

    # Same population as the ACS tables, as far as the survey allows: highest degree is a bachelor's (DGRDG 1), ages 25-64,
    # and employed in the reference week (LFSTAT 1) because every question used here is about the principal job.
    # Field = field of that highest degree (N2DGRMED), which is what "related to your highest degree" refers to.
    extra = ", ".join(c for _, s, f in NSCG_FACETS for c in (s, f)) + ", " + ", ".join(c for _, _, c in NSCG_REASONS)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE nscg AS
        SELECT N2DGRMED AS code, NDGMEMG AS grp, TRY_CAST(WTSURVY AS DOUBLE) AS w,
               OCEDRLP AS related, JOBSATIS AS sat, TELEC AS telec, TELEFR AS telefr, NRREA AS reason,
               TRY_CAST(SALARY AS DOUBLE) AS salary, TRY_CAST(HRSWK AS INT) AS hours, TRY_CAST(WKSWK AS INT) AS weeks, {extra}
        FROM read_csv('{out / csv_name}', header = true, all_varchar = true)
        WHERE DGRDG = '1' AND LFSTAT = '1' AND TRY_CAST(AGE AS INT) BETWEEN 25 AND 64
          AND N2DGRMED IN (SELECT code FROM nscg_labels) AND NDGMEMG IN (SELECT code FROM nscg_groups)
    """)
    n = con.execute("SELECT count(*) FROM nscg").fetchone()[0]
    print(f"NSCG {NSCG_YEAR}: employed bachelor's holders 25-64 in sample:", n)
    if n < 10000:
        raise SystemExit("NSCG: far fewer respondents than expected; check the file and the DGRDG / LFSTAT / AGE codes.")

    # One row per field (thin fields dropped), and one row per broad group plus 'All fields' as the reliable fallback.
    con.execute(f"""
        CREATE OR REPLACE TABLE nscg_field_experience AS
        SELECT l.label AS field, e.k AS nscg_code, g.label AS field_group, e.* EXCLUDE (k)
        FROM ({nscg_experience("code")}) e
        JOIN nscg_labels l ON l.code = e.k
        JOIN (SELECT code, any_value(grp) AS grp FROM nscg GROUP BY code) c ON c.code = e.k
        JOIN nscg_groups g ON g.code = c.grp
        WHERE e.sample_size >= {NSCG_MIN_SAMPLE}
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE nscg_group_experience AS
        SELECT 'All fields' AS field_group, e.* EXCLUDE (k) FROM ({nscg_experience("'all'")}) e
        UNION ALL
        SELECT g.label, e.* EXCLUDE (k) FROM ({nscg_experience("grp")}) e JOIN nscg_groups g ON g.code = e.k
    """)

    # Nine job aspects: satisfaction with each on the current job, and how important each is to the respondent.
    # Long format (one row per field and facet), with the all-fields figure alongside as the benchmark.
    valid = "IN ('1', '2', '3', '4')"
    facets = " UNION ALL ".join(f"SELECT code, '{label}' AS facet, w, {s} AS s, {f} AS f FROM nscg" for label, s, f in NSCG_FACETS)
    facet_cols = f"""{share("s = '1'", f"s {valid}")} AS pct_very_satisfied, {share("f = '1'", f"f {valid}")} AS pct_rate_very_important"""
    con.execute(f"""
        CREATE OR REPLACE TABLE nscg_job_facets AS
        WITH u AS ({facets}),
             by_field AS (SELECT code, facet, count(*) AS sample_size, {facet_cols} FROM u GROUP BY code, facet),
             overall AS (SELECT facet, {facet_cols} FROM u GROUP BY facet)
        SELECT l.label AS field, b.code AS nscg_code, b.facet, b.sample_size,
               b.pct_very_satisfied, o.pct_very_satisfied AS all_fields_pct_very_satisfied,
               b.pct_rate_very_important, o.pct_rate_very_important AS all_fields_pct_rate_very_important,
               b.sample_size < 100 AS low_sample_warning
        FROM by_field b JOIN overall o USING (facet) JOIN nscg_labels l ON l.code = b.code
        WHERE b.sample_size >= {NSCG_MIN_SAMPLE}
    """)

    # Why people work outside their degree field. Only those whose job is "not related" were asked, so samples are smaller.
    reasons = " UNION ALL ".join(f"SELECT code, w, reason, '{c}' AS reason_code, '{label}' AS reason_label, {col} AS cited FROM nscg WHERE related = '3'"
                                 for c, label, col in NSCG_REASONS)
    reason_cols = f"""{share("reason = reason_code", "reason IN ('1', '2', '3', '4', '5', '6', '7')")} AS pct_primary_reason,
                      {share("cited = 'Y'", "cited IN ('Y', 'N')")} AS pct_citing_reason"""
    con.execute(f"""
        CREATE OR REPLACE TABLE nscg_outside_field_reasons AS
        WITH u AS ({reasons}),
             by_field AS (SELECT code, reason_label, count(*) AS n, {reason_cols} FROM u GROUP BY code, reason_label),
             overall AS (SELECT reason_label, {reason_cols} FROM u GROUP BY reason_label)
        SELECT l.label AS field, b.code AS nscg_code, b.reason_label AS reason, b.n AS n_job_not_related,
               b.pct_primary_reason, o.pct_primary_reason AS all_fields_pct_primary_reason,
               b.pct_citing_reason, o.pct_citing_reason AS all_fields_pct_citing_reason,
               b.n < 100 AS low_sample_warning
        FROM by_field b JOIN overall o USING (reason_label) JOIN nscg_labels l ON l.code = b.code
        WHERE b.n >= {NSCG_MIN_SAMPLE}
    """)

    # ACS major -> closest NSCG field. Hand-made (data/labels/acs_nscg_crosswalk.csv); names are taken from the two official
    # code lists, so the file only decides which code goes with which. Every ACS major must appear exactly once.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE xwalk AS
                    SELECT acs_code, nscg_code, match_quality FROM read_csv('{LABELS / "acs_nscg_crosswalk.csv"}', header = true, all_varchar = true)""")
    con.execute("""
        CREATE OR REPLACE TABLE acs_nscg_crosswalk AS
        SELECT m.major, m.acs_code, l.label AS nscg_field, x.nscg_code,
               coalesce(x.match_quality, 'none') AS match_quality,
               coalesce(x.nscg_code IN (SELECT nscg_code FROM nscg_field_experience), FALSE) AS nscg_data_available
        FROM major_outcomes m
        LEFT JOIN xwalk x ON x.acs_code = m.acs_code
        LEFT JOIN nscg_labels l ON l.code = x.nscg_code
    """)
    missing = con.execute("SELECT count(*) FROM major_outcomes WHERE acs_code NOT IN (SELECT acs_code FROM xwalk)").fetchone()[0]
    bad = con.execute("""SELECT count(*) FROM acs_nscg_crosswalk
                         WHERE (match_quality = 'none') <> (nscg_code IS NULL) OR (nscg_code IS NOT NULL AND nscg_field IS NULL)
                            OR match_quality NOT IN ('same', 'close', 'loose', 'none')""").fetchone()[0]
    dup = con.execute("SELECT count(*) - count(DISTINCT acs_code) FROM acs_nscg_crosswalk").fetchone()[0]
    if missing or bad or dup:
        raise SystemExit(f"acs_nscg_crosswalk.csv: {missing} ACS majors not listed, {bad} rows with an unknown NSCG code or match_quality, {dup} duplicate ACS majors.")

    # Pay (ACS) next to experience (NSCG) for every ACS major that has an NSCG field with data: one query, no manual matching.
    con.execute("""
        CREATE VIEW major_pay_vs_experience AS
        SELECT m.major, m.acs_code, m.median_earnings, m.p25_earnings, m.p75_earnings, m.unemployment_rate_pct, m.earnings_sample_size,
               x.nscg_field, x.match_quality, n.field_group, n.sample_size AS nscg_sample_size,
               n.pct_job_closely_related, n.pct_job_not_related, n.pct_very_satisfied, n.pct_dissatisfied,
               n.pct_worked_remotely, n.median_salary_full_time AS nscg_median_salary,
               (m.low_sample_warning OR n.low_sample_warning) AS low_sample_warning
        FROM major_outcomes m
        JOIN acs_nscg_crosswalk x ON x.acs_code = m.acs_code
        JOIN nscg_field_experience n ON n.nscg_code = x.nscg_code
    """)
    con.execute("DROP TABLE nscg")


NSCG_TABLES = ("nscg_field_experience", "nscg_group_experience", "nscg_job_facets", "nscg_outside_field_reasons",
               "acs_nscg_crosswalk", "major_pay_vs_experience")

NSCG_DICTIONARY = [
    ("nscg_field_experience", "(all)",
     f"NSF National Survey of College Graduates (NSCG) {NSCG_YEAR} public-use microdata: what graduates THEMSELVES report about their job. "
     "Population: highest degree is a bachelor's, aged 25-64, EMPLOYED in the reference week (Feb 1, 2023); answers are about the principal job; shares are survey-weighted (WTSURVY). "
     "One row per NSCG field of that degree. Fields with fewer than 30 respondents are dropped; sample_size below 100 is unreliable (low_sample_warning). "
     "The survey oversamples science and engineering, so many humanities, arts, education and business fields are thin: fall back to nscg_group_experience and say so. "
     "NSCG fields are NOT ACS majors: connect them with acs_nscg_crosswalk or the view major_pay_vs_experience, never by guessing. "
     "Limits: this is satisfaction with the CURRENT JOB, not with the choice of major (the survey has no 'would you choose it again' question); "
     "graduates who were unemployed or out of the labor force are not included; these are descriptive shares, not predictions for one person."),
    ("nscg_field_experience", "field / nscg_code / field_group", "NSCG field of study label and code, and its broad group (7 groups; the survey's 'S&E' is spelled out as science and engineering)."),
    ("nscg_field_experience", "sample_size / est_workers", "Unweighted respondents behind the row, and the survey-weighted number of employed graduates they represent."),
    ("nscg_field_experience", "pct_job_closely_related / pct_job_somewhat_related / pct_job_not_related",
     "Answer to 'To what extent was your work on your principal job related to your highest degree?' (%; the three add to 100). pct_job_not_related is the share working outside their field."),
    ("nscg_field_experience", "pct_very_satisfied / pct_dissatisfied",
     "Answer to 'How would you rate your overall satisfaction with this job?': share 'very satisfied', and share 'somewhat' or 'very dissatisfied' (%). The rest answered 'somewhat satisfied'."),
    ("nscg_field_experience", "pct_worked_remotely / pct_remote_5_days_a_week",
     "Share (%) who telecommuted or worked remotely on the principal job in the reference week (required to, or allowed to and did), and the share doing so consistently 5 or more days a week. February 2023: remote-work patterns may have changed since."),
    ("nscg_field_experience", "median_salary_full_time / salary_sample_size",
     "Weighted median basic annual salary on the principal job (before deductions, excluding bonuses and overtime; 2023 dollars, NOT inflation-adjusted) for those working 35+ hours a week and 50+ weeks a year, and the respondents behind it. "
     "A second, independent survey to cross-check ACS major_outcomes.median_earnings: definitions and years differ, so expect them to be close, not equal."),
    ("nscg_group_experience", "(all)", "The same columns as nscg_field_experience for the 7 broad field groups plus one 'All fields' row (the national benchmark). Large samples: use it as the benchmark, and as the fallback when a field is thin."),
    ("nscg_job_facets", "(all)",
     "One row per NSCG field and job facet. facet is one of: 'salary', 'benefits', 'job security', 'job location', 'opportunities for advancement', 'intellectual challenge', "
     "'level of responsibility', 'degree of independence', 'contribution to society'. Same population and sample rules as nscg_field_experience."),
    ("nscg_job_facets", "pct_very_satisfied / all_fields_pct_very_satisfied", "Share (%) 'very satisfied' with that aspect of their principal job, and the same share across all fields (the benchmark, repeated on every row of the facet)."),
    ("nscg_job_facets", "pct_rate_very_important / all_fields_pct_rate_very_important",
     "Share (%) answering 'very important' to 'When thinking about a job, how important is each of the following factors to you?', and the all-fields benchmark. This describes what people who hold the degree value, not what the job delivers."),
    ("nscg_outside_field_reasons", "(all)",
     "Asked ONLY of graduates whose job is 'not related' to their degree: why they work outside the field. One row per NSCG field and reason. reason is one of: 'pay or promotion opportunities', "
     "'working conditions (hours, equipment, environment)', 'job location', 'change in career or professional interests', 'family-related reasons', 'no job available in the degree field', 'other reason'. "
     "n_job_not_related is the sample (rows need 30+; below 100 is flagged), so only larger fields appear."),
    ("nscg_outside_field_reasons", "pct_primary_reason / pct_citing_reason",
     "pct_primary_reason: share (%) naming this as the MOST important reason (adds to 100 across the 7 reasons of a field). pct_citing_reason: share (%) saying it was a factor at all (several can apply, so these add to more than 100). "
     "all_fields_* columns give the same shares across all fields."),
    ("acs_nscg_crosswalk", "(all)",
     "Hand-made link from each ACS major (major, acs_code) to the closest NSCG field (nscg_field, nscg_code). match_quality: 'same' = same field; 'close' = near equivalent or slightly broader NSCG field; "
     "'loose' = closest catch-all NSCG field, indicative only; 'none' = no usable NSCG field. Several ACS majors can share one NSCG field. nscg_data_available is FALSE when the NSCG field had too few respondents. Always report match_quality when it is not 'same'."),
    ("major_pay_vs_experience", "(all)",
     "VIEW joining ACS major_outcomes (median_earnings, p25/p75, unemployment_rate_pct, earnings_sample_size) to NSCG nscg_field_experience (pct_job_not_related, pct_job_closely_related, pct_very_satisfied, pct_dissatisfied, "
     "pct_worked_remotely, nscg_median_salary, nscg_sample_size) through acs_nscg_crosswalk. Use it for questions that combine pay with satisfaction or working in the field. "
     "Only ACS majors with an NSCG field that has data appear. NSCG figures repeat when several ACS majors map to one NSCG field. low_sample_warning is TRUE if EITHER source is under 100. "
     "The two surveys differ in year (ACS 2024, NSCG Feb 2023) and sample: report match_quality and both sample sizes."),
]


def write_nscg_dictionary(con):
    con.execute(f"DELETE FROM data_dictionary WHERE table_name IN {NSCG_TABLES}")
    con.executemany("INSERT INTO data_dictionary VALUES (?, ?, ?)", NSCG_DICTIONARY)


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
    ap.add_argument("--only", choices=["nscg"], help="add or refresh one source in the existing database instead of rebuilding everything")
    args = ap.parse_args()
    if args.only == "nscg":
        # The other four sources stay exactly as built: only the NSCG tables and their dictionary rows are replaced.
        if not DB_PATH.exists():
            raise SystemExit(f"{DB_PATH} not found. Run the full build first (without --only).")
        con = duckdb.connect(str(DB_PATH))
        load_nscg(con)
        write_nscg_dictionary(con)
        for t in NSCG_TABLES:
            print(f"{t}: {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]} rows")
        con.close()
        print(f"Updated {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB)")
        return
    DB_PATH.unlink(missing_ok=True)
    DATA.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    load_pums(con, [x.strip().lower() for x in args.states.split(",")] if args.states else None)
    y0, y1 = load_bls(con)
    load_scorecard(con)
    load_fred(con)
    load_nscg(con)
    write_dictionary(con, y0, y1)
    write_nscg_dictionary(con)
    con.execute("DROP TABLE p")
    con.execute("DROP TABLE fod_labels")
    con.execute("DROP TABLE soc_labels")
    for t in ("major_outcomes", "earnings_by_age", "major_occupations", "occupation_outlook", "scorecard_programs", "fred_series") + NSCG_TABLES:
        print(f"{t}: {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]} rows")
    con.close()
    print(f"Built {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
