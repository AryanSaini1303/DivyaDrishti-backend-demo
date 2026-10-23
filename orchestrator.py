import json
import os
import asyncio
import traceback
from openai import AsyncOpenAI
from schema import build_system_prompt
from tools import TOOLS, run_tool

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SYSTEM_PROMPT = build_system_prompt()
DEBUG_TOOL_CALLS = os.getenv("DEBUG_TOOL_CALLS", "false").lower() == "true"

CHART_EXTRACTION_PROMPT_TEMPLATE = """You are extracting chart-ready data for a business analytics
dashboard. Given the data and the question below, determine the best possible chart that
summarizes this data for the user.

Question:
{question}

Data (one entry per database query that was run; each may contain one row or many):
{data}

Instructions:
- Never fabricate numbers. Only use figures explicitly present in the data.
- If a single query's result already contains a clean comparable series (a time trend or
  category breakdown across multiple rows), chart that directly.
- CRITICAL - which axis is which: "labels" are the x-axis categories, one per row of the
  underlying data (e.g. one per SKU, one per date, one per franchise). "series" are the numeric
  columns/metrics being measured, one series per metric - NOT one series per row. A result with
  24 SKUs and a capital-tied-up figure for each becomes labels: ["SKU-A","SKU-B",...] (24 items)
  with ONE series named "Capital Tied Up" holding 24 numbers - never 24 separate series (one per
  SKU) plotted against two generic category labels like ["SKU","Capital Tied Up"]. If you find
  yourself about to name a series after a row's own identifier (a SKU code, a customer name, a
  date) rather than after a metric, that's the signal you have labels and series backwards - stop
  and swap them. A result can have many rows and still be one series; it only becomes multiple
  series when there are genuinely multiple numeric metrics per row (e.g. both "ordered_qty" and
  "received_qty" per SKU).
- If there are multiple queries and each produced just one summary figure (a single row), and
  comparing those figures together would give a useful overview (e.g. "11 disputes" vs
  "10 shipments"), combine them into one chart - one bar per query, labeled by what that
  number represents. This is the normal, expected shape for an "overview" or "summary"
  question that touches several tables or metrics at once; do not null this out just because
  no single query alone had multiple rows.
- Only combine figures this way when they share the same unit and a comparable scale (e.g. all
  are plain counts, or all are the same currency in a similar range). Never combine a currency
  total with a raw count, a percentage, or a currency total of a very different magnitude onto
  one chart - one bar dwarfing the others makes the chart unreadable and the comparison
  meaningless. If the available summary figures don't share a common unit/scale, either chart
  only the subset that does, or return chart_data: null rather than forcing an incoherent mix.
- The same scale-mismatch problem applies to multiple REAL per-row series too (e.g. attendance
  percentage, which runs 0-100, plotted alongside a sales count, which might run 0-10) - not
  just to combining separate single-figure queries. If two series in the same chart have very
  different value ranges (roughly 5x or more apart in their max values), plotting them as raw
  bars on one linear axis makes the smaller series collapse to invisible slivers near zero,
  which silently hides exactly the pattern the chart was supposed to show. When this happens,
  normalize each affected series independently to a 0-100 "percent of that series' own maximum"
  scale before combining them, so both are visible and their shapes remain comparable - and
  rename the axis/series labels to make clear these are now relative (e.g. "Attendance (% of
  max)", "Sales (% of max)"), not the original absolute units. The exact original absolute
  numbers are already covered in the spoken answer alongside the chart - the chart's job here is
  showing the pattern/correlation shape, not re-stating precise figures.
- Keep labels short (one or two words: "Sales", "Discount", "Orders") rather than verbatim
  column or field names - long labels get truncated illegibly on a chart axis.
- Only return chart_data: null when there is truly nothing comparable - a single isolated
  figure with nothing else alongside it, or purely textual/non-numeric information.
- chart_type is "bar", "line", or "pie". Prefer "bar" for a small number of distinct
  categories or combined metrics, "line" for an actual time series.
- Return STRICT JSON ONLY in this shape:
{{"chart_data": {{"chart_type": "bar|line|pie", "title": "...", "labels": ["..."], "series": [{{"name": "...", "data": [0]}}]}} }}
or:
{{"chart_data": null}}
"""


async def extract_chart_data(question: str, sql_results: list):
    if not sql_results:
        return None
    data_str = json.dumps(sql_results)[:8000]
    try:
        completion = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a precise data extraction assistant. Follow instructions strictly."},
                {"role": "user", "content": CHART_EXTRACTION_PROMPT_TEMPLATE.format(question=question, data=data_str)},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(completion.choices[0].message.content)
        return parsed.get("chart_data")
    except Exception:
        return None


async def stream_answer(question: str, history: list, origin: str):
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += history[-6:]
        messages.append({"role": "user", "content": question})
        sql_results = []
        for _ in range(5):
            decision = await client.chat.completions.create(
                model="gpt-4.1",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.1,
            )
            msg = decision.choices[0].message
            if not msg.tool_calls:
                chart_task = asyncio.create_task(extract_chart_data(question, sql_results))
                if msg.content:
                    yield ("token", {"text": msg.content})
                yield ("chart_data", {"chart_data": await chart_task})
                yield ("done", {})
                return
            messages.append(msg)
            calls = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    calls.append(asyncio.sleep(0, result={"error": "malformed tool arguments"}))
                    continue
                calls.append(run_tool(tc.function.name, args, origin))
            results = await asyncio.gather(*calls, return_exceptions=True)
            safe_results = [
                {"error": str(r)} if isinstance(r, Exception) else r
                for r in results
            ]
            for tc, result in zip(msg.tool_calls, safe_results):
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
                if tc.function.name == "query_database" and "error" not in result:
                    sql_results.append(result.get("rows", []))
            meta = {"tools_used": [tc.function.name for tc in msg.tool_calls]}
            if DEBUG_TOOL_CALLS:
                meta["debug"] = [
                    {"tool": tc.function.name, "args": tc.function.arguments, "result": result}
                    for tc, result in zip(msg.tool_calls, safe_results)
                ]
            yield ("meta", meta)
        messages.append({
            "role": "system",
            "content": (
                "You have used your available tool-call rounds for this turn and cannot query "
                "anything further. Answer using only what the tool results above actually show. "
                "If that isn't enough to answer the question, say plainly what you do have, what "
                "you don't, and why (e.g. empty result, a query error, or a concept this schema "
                "doesn't track) - do not claim a database or connection problem unless a tool "
                "result above actually shows one."
            ),
        })
        chart_task = asyncio.create_task(extract_chart_data(question, sql_results))
        stream = await client.chat.completions.create(model="gpt-4.1", messages=messages, stream=True, temperature=0.2)
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield ("token", {"text": delta})
        yield ("chart_data", {"chart_data": await chart_task})
        yield ("done", {})
    except Exception as e:
        traceback.print_exc()
        yield ("error", {"message": str(e)})
        yield ("done", {})