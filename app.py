import itertools
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import uvicorn
from agents import Agent, MaxTurnsExceeded, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from context_panel import AgentContexts, PanelHooks, add_context_panel

log = logging.getLogger("major-assistant")

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

HERE = Path(__file__).parent
MODEL_NAME = os.environ.get("MODEL_NAME", "vertex_ai/gemini-3.5-flash")  # Lead and Writer
RESEARCHER_MODEL_NAME = os.environ.get("RESEARCHER_MODEL_NAME", "vertex_ai/gemini-3.5-flash-lite")  # Researchers only query and take notes
SESSIONS_DB = os.environ.get("SESSIONS_DB", "/tmp/conversations.db")
MAX_ROWS = 60  # rows a query may return to the model
MAX_RESEARCHERS = 8  # total Researcher runs per question: prompts ask for restraint, code enforces it
LEAD_MAX_TURNS = 6
RESEARCHER_MAX_TURNS = 10

# --- The Database (built offline by build_data.py) ---

try:
    # The security boundary is the connection, not the prompt: read-only, no file or network access.
    db = duckdb.connect(
        str(HERE / "data" / "majors.duckdb"),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
except duckdb.Error:
    raise SystemExit("No database found. Run `uv run --group build build_data.py` first.")

# Which public source each table comes from. Shown next to every citation.
TABLE_SOURCES = {
    "major_outcomes": "Census ACS 2024 1-yr PUMS",
    "earnings_by_age": "Census ACS 2024 1-yr PUMS",
    "earnings_growth": "Census ACS 2024 1-yr PUMS",
    "major_occupations": "Census ACS 2024 PUMS + BLS Employment Projections",
    "occupation_outlook": "BLS Employment Projections (matched to ACS occupation codes)",
    "scorecard_programs": "College Scorecard Field of Study",
    "fred_series": "FRED (BLS CPS unemployment by education; CPI)",
    "data_dictionary": "data dictionary",
}

# --- Per-request state: evidence ledger + the tool-call log shown in the UI ---

_ids = itertools.count(1)
_ids_lock = threading.Lock()


@dataclass
class Ctx:
    calls: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    researchers: int = 0
    findings: list = field(default_factory=list)  # what each Researcher reported: the Writer reads these, not the Lead's retelling
    hooks: object = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_evidence(self, query: str, columns: list, rows: list) -> str:
        tables = sorted({t for t in TABLE_SOURCES if re.search(rf"\b{t}\b", query)})
        sources = " + ".join(dict.fromkeys(TABLE_SOURCES[t] for t in tables)) or "database"
        with _ids_lock:  # ids are unique across requests, so an old [E12] can never validate against a new ledger
            eid = f"E{next(_ids)}"
        self.evidence[eid] = {"sources": sources, "tables": tables, "query": query, "columns": columns, "rows": rows}
        return eid

    def next_researcher(self) -> int | None:
        with self.lock:
            if self.researchers >= MAX_RESEARCHERS:
                return None
            self.researchers += 1
            return self.researchers


class Hooks(PanelHooks):
    """PanelHooks records each agent's exact context for the context panel. On top of that, record each
    tool call for the /chat response, tagged with the agent that made it."""

    def __init__(self, contexts: AgentContexts, ctx: Ctx):
        super().__init__(contexts, lambda event: None)  # /chat is request-response: the panel polls instead of streaming
        self.ctx = ctx

    async def on_tool_end(self, context, agent, tool, result):
        try:
            args = json.loads(context.tool_arguments)
        except (TypeError, ValueError):
            args = {}
        self.ctx.calls.append({"agent": agent.name, "name": tool.name, "args": args, "result": str(result)})


# --- Data tools (used by Researchers) ---


def tool_failed(ctx, error: Exception) -> str:
    """Used for every tool: if a tool raises, the model gets a fixed message and a next step, never the exception text."""
    log.error("tool failed: %s: %s", type(error).__name__, error)
    return "That tool failed. Next step: try once more with a simpler request; if it fails again, report what you could not get under GAPS."



@function_tool(failure_error_function=tool_failed)
def describe_data(ctx: RunContextWrapper[Ctx]) -> str:
    """List every table and column in the database with definitions and caveats. Call this first."""
    with db.cursor() as cur:
        rows = cur.execute("SELECT table_name, column_name, description FROM data_dictionary ORDER BY table_name <> '(all)', table_name").fetchall()
        tables = cur.execute("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'main' AND table_name <> 'data_dictionary' ORDER BY table_name, ordinal_position").fetchall()
    schema: dict[str, list[str]] = {}
    for t, c, ty in tables:
        schema.setdefault(t, []).append(f"{c} {ty}")
    out = ["SCHEMA:"] + [f"{t}({', '.join(cols)})" for t, cols in schema.items()] + ["", "DEFINITIONS AND CAVEATS:"]
    out += [f"- {t}{'.' + c if c and c != '(all)' else ''}: {d}" for t, c, d in rows]
    return "\n".join(out)


@function_tool(failure_error_function=tool_failed)
def find_majors(ctx: RunContextWrapper[Ctx], keyword: str) -> str:
    """Find exact major names (ACS) and program names (College Scorecard) containing a keyword, e.g. 'computer' or 'nurs'.

    Args:
        keyword: A word or fragment to search for, case-insensitive.
    """
    with db.cursor() as cur:
        rows = cur.execute(
            """SELECT 'ACS major (major_outcomes.major)' AS kind, major AS name FROM major_outcomes WHERE major ILIKE ?
               UNION ALL
               SELECT 'Scorecard program (scorecard_programs.cip_description)', cip_description FROM scorecard_programs WHERE cip_description ILIKE ?
               LIMIT 40""",
            [f"%{keyword}%", f"%{keyword}%"],
        ).fetchall()
    return json.dumps(rows) if rows else "No matches. Try a shorter or different keyword."


def sql_error_hint(e: Exception) -> str:
    if isinstance(e, duckdb.ParserException):
        return "The query has a syntax error. Next step: rewrite it as a single DuckDB SELECT statement."
    if isinstance(e, (duckdb.BinderException, duckdb.CatalogException)):
        return "The query uses a table or column that does not exist. Next step: call describe_data and use the exact names it lists."
    if isinstance(e, duckdb.PermissionException):
        return "That operation is not allowed: the database is read-only. Next step: query the listed tables with SELECT only."
    return "The query could not be run. Next step: try a simpler SELECT over one table, or call describe_data to check the schema."


@function_tool(failure_error_function=tool_failed)
def run_sql(ctx: RunContextWrapper[Ctx], query: str) -> str:
    """Run a read-only DuckDB SQL query. Successful results come back with an evidence_id to cite.

    Args:
        query: A DuckDB SQL query.
    """
    # The SDK runs tools in threads. Each call gets its own cursor: DuckDB connections aren't thread-safe.
    with db.cursor() as cursor:
        try:
            cursor.execute(query)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except duckdb.Error as e:
            # The model cannot see an exception, and raw engine text can leak paths or internals.
            # Log the detail here; return a fixed message with a next step it can act on.
            log.warning("run_sql failed: %s: %s", type(e).__name__, e)
            return json.dumps({"error": sql_error_hint(e)})
        columns = [c[0] for c in cursor.description or []]
    shown = [list(r) for r in rows[:MAX_ROWS]]
    eid = ctx.context.add_evidence(query, columns, shown)
    return json.dumps({"evidence_id": eid, "columns": columns, "rows": shown, "truncated": len(rows) > MAX_ROWS}, default=str)


# --- Agents ---

# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})


def litellm(name: str) -> LitellmModel:
    # LiteLLM routes the call to Gemini. api_key is unused: Vertex AI authenticates with gcloud / the Cloud Run service account.
    return LitellmModel(model=name, api_key="unused")


RESEARCHER_INSTRUCTIONS = """You are a Researcher on a college-major data team. You receive ONE self-contained sub-question and no other context.
Answer it using ONLY the database, through your tools.

Process:
1. Call describe_data once to learn the tables, definitions and caveats. Use find_majors to resolve fuzzy major names to exact names.
2. Use run_sql (DuckDB) to get the numbers. Use at most 6 queries. If a query errors, fix it.
3. When ranking majors, require earnings_sample_size >= 100 (or sample_size >= 100 for other tables) unless asked otherwise, and say you did.
4. Never state a number from memory. Every number you report must come from a run_sql result and carry its evidence_id as [E123].

Reply in exactly this format:
STATUS: answered | partial | cannot_answer
FINDINGS:
- each finding with its numbers and a citation like [E123] (one id per bracket, e.g. [E12][E13])
CAVEATS: sample sizes, definitions that matter (who is counted, which years, cross-section vs cohort), approximate matches (e.g. Scorecard CIP vs ACS major); for 'five years after graduation' prefer scorecard_programs.national_median_earnings_5yr and cross-check with earnings_by_age
GAPS: what the data could not answer, or "none"

If the data cannot answer the question (e.g. job satisfaction, a specific school, causation, years after 2024, anything not in the tables), say STATUS: cannot_answer and why. Do not guess or substitute a different question without saying so."""

WRITER_INSTRUCTIONS = """You are the Writer for a college-major decision assistant. You receive the student's question, the Researchers' findings, and an EVIDENCE LEDGER of the exact query results.

Rules:
- Use ONLY facts from the findings and ledger. Every number gets a citation [E123] using ids that exist in the ledger, one id per bracket. Check each number against the ledger rows; if a finding conflicts with the ledger, trust the ledger.
- Lead with the direct answer in one or two sentences. Then support it (a compact markdown table is good for rankings). Round sensibly and state units ($, %, ages).
- Always state the key definitions in plain words (bachelor's holders, ages, full-time year-round, 2024 dollars) and any sample-size warnings.
- Add a short "What the data can't tell you" section whenever a Researcher reported partial/cannot_answer, or whenever the question implies causation, personal fit, satisfaction, a specific school, or the future. Never fill a gap with outside knowledge.
- Differences between majors are descriptive, not proof that the major caused them. Gender gaps are raw, not adjusted for occupation, hours or experience.
- If the student asked for advice choosing a major: give a shortlist of 3-5 options with trade-offs (pay, spread p25-p75, unemployment, growth with age, typical jobs), tie it to the priorities they stated, and never declare a single "best" major.
- Plain, specific sentences. No "profoundly", "pivotal", "paving the way", or "it is worth noting". Under about 450 words. No preamble, no mention of agents or tools."""

LEAD_INSTRUCTIONS = """You are the Lead of a college-major decision assistant. Students ask whether majors lead to the careers and salaries they expect. You orchestrate; you never answer from memory, and you never write the report: a Writer does, from the Researchers' findings.

Data available (via Researchers): Census ACS 2024 microdata summarised by bachelor's major (earnings and percentiles, gender gaps, unemployment, labor-force participation, earnings by age band, top occupations), BLS employment projections by occupation (growth), College Scorecard national earnings by field 1, 4, 5 and 7 years after graduation (median with P25/P75; federal-aid recipients) and debt, and FRED unemployment by education level. All US, bachelor's holders, ACS 2024.

1. CLARIFY FIRST if the question is too vague to research: no metric ("is engineering good?"), no comparison set, or a request to recommend a major without any priorities (pay, low unemployment risk, interests, time horizon). Reply with ONE short question block (at most 2 questions, with example answers) and call NO tools. Do not ask if the question is already specific enough; assume sensible defaults and say so in your DONE line.
2. PLAN: split the question into 2 to 5 independent research tasks. Call research once per task, ALL IN THE SAME TURN, so the researchers work in parallel. Researchers see no conversation, so each task must be fully self-contained: name the metric, the majors, the age group and the minimum sample.
3. REVIEW the findings. If a task came back partial/cannot_answer for something the data might hold, findings conflict, or a comparison is missing, call research again for just that part. Do this at most twice. If research says no researchers are left, stop.
4. When the question is covered, reply `DONE: ` followed by the student's complete question in one paragraph, including any priorities from earlier in the conversation. Nothing else: the Writer takes it from there.

Earlier turns of this conversation are in your history. Re-research rather than reusing old [E#] ids."""

researcher = Agent(
    name="Researcher",
    instructions=RESEARCHER_INSTRUCTIONS,
    model=litellm(RESEARCHER_MODEL_NAME),
    model_settings=SETTINGS,
    tools=[describe_data, find_majors, run_sql],
)


@function_tool(failure_error_function=tool_failed)
async def research(ctx: RunContextWrapper[Ctx], task: str) -> str:
    """Send a Researcher to answer one task from the database, in a fresh context. Call several in one turn to run them in parallel.

    Args:
        task: One self-contained task with the metric, majors, age group and sample-size rule spelled out.
    """
    n = ctx.context.next_researcher()
    if n is None:
        return "No researchers left. Reply DONE."
    # What researcher.as_tool() does: a fresh context and its own numbered copy, so each run gets its own column in the context panel.
    try:
        result = await Runner.run(researcher.clone(name=f"Researcher {n}"), task, context=ctx.context, hooks=ctx.context.hooks, max_turns=RESEARCHER_MAX_TURNS)
        finding = str(result.final_output)
    except MaxTurnsExceeded:
        finding = "STATUS: partial\nFINDINGS:\nCAVEATS: This researcher ran out of steps before finishing.\nGAPS: unfinished; a narrower task may succeed"
    ctx.context.findings.append(f"TASK: {task}\n\n{finding}")
    return finding


lead = Agent(name="Lead", instructions=LEAD_INSTRUCTIONS, model=litellm(MODEL_NAME), model_settings=SETTINGS, tools=[research])

writer = Agent(name="Writer", instructions=WRITER_INSTRUCTIONS, model=litellm(MODEL_NAME), model_settings=SETTINGS)


async def write(ctx: Ctx, question: str, note: str = "") -> str:
    """One Writer call, outside the Lead's loop. It sees the question, the findings exactly as the Researchers wrote them, and the ledger."""
    ledger = "\n".join(
        f"[{eid}] source: {e['sources']}\n  SQL: {e['query']}\n  columns: {e['columns']}\n  rows (first 25): {json.dumps(e['rows'][:25], default=str)}"
        for eid, e in ctx.evidence.items()
    ) or "(empty: no queries were run)"
    findings = "\n\n---\n\n".join(ctx.findings) or "(no findings)"
    prompt = f"STUDENT QUESTION:\n{question}\n{note}\n\nRESEARCHER FINDINGS:\n\n{findings}\n\nEVIDENCE LEDGER:\n{ledger}"
    result = await Runner.run(writer, prompt, context=ctx, hooks=ctx.hooks, max_turns=2)
    return str(result.final_output)


# --- Citation check: every [E#] in the answer must exist in this request's ledger ---


NUMBER = re.compile(r"(\$)?(\d[\d,]*(?:\.\d+)?)\s?(%|k\b|K\b|M\b|million\b)?")


def figures(line: str) -> list[tuple[str, float, int, bool]]:
    """Numbers in a line worth verifying: (text, value, decimals shown, is_percent). Years, ages, counts under 1000
    and other bare small integers are skipped: only money, percentages and big or decimal numbers are claims about the data."""
    out = []
    for m in NUMBER.finditer(line):
        dollar, raw, suffix = m.groups()
        digits = raw.replace(",", "")
        value = float(digits)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        if suffix and suffix.lower() == "k":
            value, decimals = value * 1e3, 0
        elif suffix and suffix.lower() in ("m", "million"):
            value, decimals = value * 1e6, 0
        if not (dollar or suffix) and "." not in digits and value < 1000:
            continue  # a bare small integer: an age, a count, a threshold
        if not (dollar or suffix) and 1900 <= value <= 2100 and "," not in raw:
            continue  # a year
        out.append((m.group(0).strip(), value, decimals, suffix == "%"))
    return out


def in_evidence(value: float, decimals: int, evidence_values: list[float]) -> bool:
    tol = max(0.5 * 10 ** -decimals, 0.006 * abs(value)) if decimals or value >= 1000 else 0.5
    return any(abs(value - v) <= tol for v in evidence_values)


def check_citations(text: str, ctx: Ctx, question: str = "") -> str:
    """1. Every [E#] must exist in this request's ledger (else [unverified]).
    2. Every money / percent / large number must appear in the rows of the evidence cited on its line, within rounding.
       Numbers the student typed themselves are exempt. Anything else is listed as not verified: the Writer may have
       derived it (a difference, a ratio) or misquoted it."""
    text = re.sub(r"\[(E\d+(?:\s*[,;]\s*E\d+)+)\]", lambda m: "".join(f"[{x}]" for x in re.findall(r"E\d+", m.group(1))), text)
    cited: list[str] = []

    def check(m):
        eid = m.group(1)
        if eid not in ctx.evidence:
            return "[unverified]"
        if eid not in cited:
            cited.append(eid)
        return m.group(0)

    text = re.sub(r"\[(E\d+)\]", check, text)

    asked = {round(v) for _, v, _, _ in figures(question)}
    unverified: list[str] = []
    for line in text.split("\n"):
        ids = [i for i in re.findall(r"\[(E\d+)\]", line) if i in ctx.evidence]
        values = [float(v) for i in ids for row in ctx.evidence[i]["rows"] for v in row if isinstance(v, (int, float)) and not isinstance(v, bool)]
        sql_numbers = {round(v) for i in ids for _, v, _, _ in figures(ctx.evidence[i]["query"])}
        for shown, value, decimals, _ in figures(re.sub(r"\[(?:E\d+|unverified)\]", "", line)):
            if round(value) in asked or round(value) in sql_numbers:
                continue
            if not ids or not in_evidence(value, decimals, values):
                unverified.append(shown)
    if unverified:
        log.warning("figures not verified against cited data: %s", unverified)
        text += "\n\n> **Not verified against the cited query results:** " + ", ".join(dict.fromkeys(unverified)) + \
                ". These may be derived (a difference or ratio) or misquoted: check them against the Sources."
    if cited:
        lines = [f"- [{e}] {ctx.evidence[e]['sources']}. SQL: `{ctx.evidence[e]['query'].strip()[:400]}`" for e in cited]
        text += "\n\n---\n**Sources**\n" + "\n".join(lines)
    return text


# --- Conversation memory (plain text turns, so the Lead never sees stale tool output) ---


def _conn():
    c = sqlite3.connect(SESSIONS_DB)
    c.execute("CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT)")
    return c


def load_history(session_id: str, limit: int = 8) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT role, content FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(rows)]


def save_turn(session_id: str, user: str, assistant: str):
    with _conn() as c:
        c.executemany("INSERT INTO turns (session_id, role, content) VALUES (?, ?, ?)", [(session_id, "user", user), (session_id, "assistant", assistant)])


# --- FastAPI App ---

# run_id -> every agent's context, in order. In-memory, single process; the context panel reads it. Oldest runs are dropped.
sessions: dict[str, AgentContexts] = {}
MAX_RUNS_KEPT = 30

app = FastAPI()
add_context_panel(app, sessions)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    run_id: str | None = None  # optional: lets the page poll /context for this run while /chat is still working


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    run_id = request.run_id or str(uuid.uuid4())
    sessions[run_id] = AgentContexts()
    while len(sessions) > MAX_RUNS_KEPT:
        sessions.pop(next(iter(sessions)))
    ctx = Ctx()
    ctx.hooks = Hooks(sessions[run_id], ctx)
    question = ""  # the Lead's restated question, once it has one
    try:
        history = load_history(session_id)
    except sqlite3.Error:
        log.exception("could not load history")  # answer without memory rather than fail
        history = []
    try:
        result = await Runner.run(lead, history + [{"role": "user", "content": request.message}], context=ctx, hooks=ctx.hooks, max_turns=LEAD_MAX_TURNS)
        lead_text = str(result.final_output).strip()
        if not ctx.findings:
            response = lead_text  # nothing researched: the Lead is asking a clarifying question (or explaining why it can't)
        else:
            question = re.sub(r"^\s*DONE:?\s*", "", lead_text, flags=re.I) or request.message
            response = await write(ctx, question)
    except MaxTurnsExceeded:
        # The Lead ran out of turns: write from whatever the Researchers found rather than fail.
        if not ctx.findings:
            return ChatResponse(response="That took too many steps and nothing was found. Try a narrower question.", session_id=session_id, tool_calls=ctx.calls)
        response = await write(ctx, request.message, "(The Lead hit its turn cap. Report only what the ledger supports and say what is missing.)")
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        log.exception("chat failed")  # full detail stays in the server logs, not in the chat
        return ChatResponse(
            response="Something went wrong while researching your question. Please try again in a minute. "
            "If it keeps happening, ask a narrower question (one metric, a few majors).",
            session_id=session_id,
            tool_calls=ctx.calls,
        )

    response = check_citations(response, ctx, request.message + " " + (question if ctx.findings else ""))
    try:
        save_turn(session_id, request.message, response)
    except sqlite3.Error:
        log.exception("could not save history")  # the answer is still good; only follow-ups lose memory
    return ChatResponse(response=response, session_id=session_id, tool_calls=ctx.calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        try:
            with _conn() as c:
                c.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
        except sqlite3.Error:
            log.exception("could not clear history")
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
