"""Print the README's "Three sample queries" section as markdown. For each question:
the expected answer (computed here, straight from the database), the agent's answer, and the answer from
the same model with no tools and no system prompt.

    uv run app.py                                               # terminal 1: the agent, on http://127.0.0.1:8000
    uv run python scripts/sample_queries.py > samples.md        # terminal 2

    uv run python scripts/sample_queries.py --agent-url https://<service>.run.app   # test the deployed agent instead
    uv run python scripts/sample_queries.py --expected-only                         # no model calls: only the SQL answers
"""

import argparse
import json
import os
import urllib.request
from collections import Counter
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = os.environ.get("MODEL_NAME", "vertex_ai/gemini-3.5-flash")  # same default as app.py: the Lead and Writer model

# Each question has one answer that one query computes. `sql` is that query; `source` says where the answer lives.
SAMPLES = [
    {
        "question": "Among majors with at least 100 sampled full-time earners, which has the highest median earnings?",
        "source": "`major_outcomes.median_earnings` (Census ACS 2024 PUMS)",
        "sql": """SELECT major, median_earnings, earnings_sample_size
                  FROM major_outcomes WHERE earnings_sample_size >= 100
                  ORDER BY median_earnings DESC, major LIMIT 3""",
    },
    {
        "question": "Among fields with at least 100 survey respondents, which has the highest share of employed graduates "
                    "in a job not related to their degree?",
        "source": "`nscg_field_experience.pct_job_not_related` (NSF NSCG 2023)",
        "sql": """SELECT field, sample_size, pct_job_not_related
                  FROM nscg_field_experience WHERE sample_size >= 100
                  ORDER BY pct_job_not_related DESC LIMIT 3""",
    },
    {
        "question": "Among majors with median earnings of at least $100,000 and at least 100 respondents in both the earnings data and "
                    "the graduate survey, which has the lowest share of graduates very satisfied with their job?",
        "source": "view `major_pay_vs_experience` (ACS `major_outcomes` joined to NSCG `nscg_field_experience` through `acs_nscg_crosswalk`)",
        "sql": """SELECT major, median_earnings, earnings_sample_size, nscg_field, match_quality, nscg_sample_size, pct_very_satisfied
                  FROM major_pay_vs_experience WHERE NOT low_sample_warning AND median_earnings >= 100000
                  ORDER BY pct_very_satisfied LIMIT 3""",
    },
]


def expected(con, sql: str) -> str:
    """The query's rows as a markdown table. The first row is the answer; the next two show how close the runners-up are."""
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    fmt = lambda v: f"{v:,.0f}" if isinstance(v, float) and v >= 1000 else str(v)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in cur.fetchall()]
    return "\n".join(lines)


def ask_agent(url: str, question: str) -> str:
    """One fresh /chat request (no session), plus a one-line count of the tool calls each agent made."""
    req = urllib.request.Request(url.rstrip("/") + "/chat", data=json.dumps({"message": question}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        body = json.loads(r.read())
    calls = Counter("Researchers" if c["agent"].startswith("Researcher") else c["agent"] for c in body["tool_calls"])
    summary = ", ".join(f"{agent}: {n}" for agent, n in calls.items()) or "none"
    return f"{body['response']}\n\n_Tool calls: {summary}._"


def ask_plain(question: str) -> str:
    """The same model the Lead and Writer use, with only the question: no tools, no system prompt."""
    import litellm

    r = litellm.completion(model=MODEL_NAME, messages=[{"role": "user", "content": question}], vertex_location="global", timeout=180)
    return r.choices[0].message.content or "(empty reply)"


def attempt(fn, *args) -> str:
    try:
        return fn(*args)
    except Exception as e:  # keep going: one failed call should not lose the other answers
        return f"_Not run: {type(e).__name__}. Check that the agent is running and that Vertex AI credentials are set up._"


def fold(title: str, body: str) -> str:
    return f"<details><summary>{title}</summary>\n\n{body}\n\n</details>"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-url", default="http://127.0.0.1:8000", help="base URL of the running agent")
    ap.add_argument("--expected-only", action="store_true", help="print only the expected answers; make no model calls")
    args = ap.parse_args()
    con = duckdb.connect(str(ROOT / "data" / "majors.duckdb"), read_only=True)
    model = MODEL_NAME.split("/")[-1]
    for i, s in enumerate(SAMPLES, 1):
        print(f"### {i}. {s['question']}\n")
        print(f"**Expected answer** (top row), from {s['source']}:\n")
        print(expected(con, s["sql"]) + "\n")
        print(fold("SQL behind the expected answer", "```sql\n" + "\n".join(l.strip() for l in s["sql"].splitlines()) + "\n```") + "\n")
        if args.expected_only:
            continue
        print(fold("<b>Agent's answer</b>", attempt(ask_agent, args.agent_url, s["question"])) + "\n")
        print(fold(f"<b>Same model (<code>{model}</code>), no tools, no system prompt</b>", attempt(ask_plain, s["question"])) + "\n")


if __name__ == "__main__":
    main()
