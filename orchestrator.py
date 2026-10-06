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
- If there are multiple queries and each produced just one summary figure (a single row), and
  comparing those figures together would give a useful overview (e.g. "11 disputes" vs
  "10 shipments"), combine them into one chart - one bar per query, labeled by what that
  number represents. This is the normal, expected shape for an "overview" or "summary"
  question that touches several tables or metrics at once; do not null this out just because
  no single query alone had multiple rows.
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

        for _ in range(2):
            decision = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
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

        # Only reached if the loop used up both tool rounds and the model
        # still wanted to call more tools - force a final answer from
        # whatever's been gathered so far, with no tool access left.
        chart_task = asyncio.create_task(extract_chart_data(question, sql_results))
        stream = await client.chat.completions.create(model="gpt-4.1-mini", messages=messages, stream=True)
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