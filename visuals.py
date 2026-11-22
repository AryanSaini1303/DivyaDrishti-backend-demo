"""Answer visuals: the cards that appear next to the avatar after an answer.

How it works
------------
1. The LLM never sees or retypes the numbers it charts. It gets a compact description of
   each query result (column names, types, row count, a few sample rows) and returns a *spec*:
   which card types to show and which query/columns each one reads from.
2. build_cards() then builds every card straight from the real SQL rows. Anything that
   doesn't line up (a missing column, a non-numeric value column, an empty result) is
   dropped instead of being guessed - so a card can be missing, but never wrong.

Card types (the wire format the frontend renders):
    kpis         one to four headline figures, optional change vs a previous figure, optional sparkline
    ranking      items ranked by a value (top vendors, best-selling SKUs)
    trend        one or more series over time
    comparison   two or more metrics side by side per category (this month vs last, ordered vs received)
    composition  parts of a whole (share by category)
    funnel       ordered stages with drop-off (leads -> orders -> delivered)
    table        the actual rows (top orders, overdue invoices)
    heatmap      a value over two dimensions (orders by weekday x hour)

Plus follow_ups: 2-3 short next questions the user can tap to ask.
"""
import json
import os
import re
import datetime
from openai import AsyncOpenAI

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Tried in order; the first that works is used. gpt-4.1-mini follows the spec format more
# reliably than 4o-mini; 4o-mini stays as a fallback.
VISUALS_MODELS = [m.strip() for m in os.getenv("VISUALS_MODELS", "gpt-4.1-mini,gpt-4o-mini").split(",") if m.strip()]

MAX_CARDS = 4
MAX_ITEMS = 60          # ranking / composition / trend points kept per card
MAX_TABLE_ROWS = 100
UNITS = {"currency", "count", "percent", "days", "number", "quantity"}
CARD_TYPES = {"kpis", "ranking", "trend", "comparison", "composition", "funnel", "table", "heatmap"}

VISUALS_PROMPT = """You design the visual cards shown next to a spoken answer in a business
analytics assistant. The user asked:

{question}

These database queries were run to answer it (results described below; query numbers start at 0):

{results}

Choose the cards that make this answer most useful at a glance. Return STRICT JSON:
{{"cards": [ ... ], "follow_ups": ["...", "..."]}}

Card types and their fields (column names must be copied exactly from the results above):

- "kpis": headline figures. {{"type":"kpis","title":"...","items":[{{"label":"...","query":0,"column":"...","row":0,"unit":"..."}}]}}
  Optional per item: "compare_query","compare_column","compare_row","compare_label" (e.g. "vs last month")
  when the data contains the same figure for a previous period - the card shows the % change.
  Optional per item: "spark_query","spark_x","spark_y" pointing at a time series of that figure.
  Use for 1-4 single figures (totals, counts, averages). Prefer this over a chart for lone numbers.
- "ranking": {{"type":"ranking","title":"...","query":0,"label_column":"...","value_column":"...","unit":"..."}}
  Items ranked by one value. Use for "top/best/most/least X by Y" and any category breakdown
  of one metric with more than ~3 categories.
- "trend": {{"type":"trend","title":"...","query":0,"x_column":"...","value_columns":["..."],"unit":"..."}}
  A metric over time (x_column is a date/month/week/day). Only when the rows really are a time series.
- "comparison": {{"type":"comparison","title":"...","query":0,"label_column":"...","value_columns":["...","..."],"series_names":["...","..."],"unit":"..."}}
  Two or more metrics with the SAME unit per category (ordered vs received per SKU, this month vs
  last month per franchise). Never mix units (money with counts) in one comparison.
- "composition": {{"type":"composition","title":"...","query":0,"label_column":"...","value_column":"...","unit":"..."}}
  Parts of a whole where the share matters (sales mix by category). Best with 2-8 parts.
- "funnel": {{"type":"funnel","title":"...","steps":[{{"label":"...","query":0,"column":"...","row":0}}]}}
  or {{"type":"funnel","title":"...","query":0,"label_column":"...","value_column":"..."}} when one
  query already lists the stages in order. Only for genuinely sequential stages (leads -> won ->
  ordered -> delivered), each a count of the same things moving through a process.
- "table": {{"type":"table","title":"...","query":0,"columns":["...","..."]}}
  The rows themselves, for record lists (which orders, which SKUs, overdue invoices). Pick the
  3-6 most useful human-readable columns; never id/uuid columns.
- "heatmap": {{"type":"heatmap","title":"...","query":0,"x_column":"...","y_column":"...","value_column":"...","unit":"..."}}
  A value over two categorical dimensions (weekday x hour, warehouse x month). Only when the rows
  have exactly that shape.

"unit" is one of: currency (money, rupees), count (number of things), quantity (units of stock),
percent, days, number (anything else). Decide from the column meaning, not just the name.

Rules:
- At most {max_cards} cards, most important first. Usually 1-3. Don't repeat the same data in two
  cards of different types - pick the best type for it (the user can switch views on a card).
- A good answer often pairs a kpis card (the headline numbers) with one chart or table.
- Never chart id columns. Never invent columns, queries or rows that aren't listed above.
- If nothing here is worth visualising (empty results, purely textual data), return "cards": [].
- Titles: short and specific (max ~6 words), e.g. "Top vendors by revenue", "Orders by week".
- follow_ups: 2-3 short natural questions (max ~8 words each) that dig one level deeper into THIS
  answer, phrased the way the user would ask them aloud - e.g. "Break this down by franchise",
  "How does this compare to last month?", "Which SKUs drive this?". Only questions this kind of
  business database can answer. No generic questions.
"""


# --------------------------------------------------------------------------- helpers

def _num(v):
    """A clean float, or None if the value isn't a usable number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float("inf") else None
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return float(s)
    return None


def _label(v):
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([T ][\d:.+\-Z]*)?$")


def _is_date(v):
    return isinstance(v, str) and bool(_DATE_RE.match(v.strip()))


def _unit(u):
    return u if u in UNITS else "number"


def _col_type(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return "empty"
    if all(_num(v) is not None for v in vals):
        return "number"
    if all(_is_date(v) for v in vals):
        return "date"
    if all(isinstance(v, bool) for v in vals):
        return "bool"
    return "text"


def describe_results(sql_results):
    """Compact per-query description for the LLM: columns, types, size and a sample."""
    out = []
    for qi, rows in enumerate(sql_results):
        if not rows:
            out.append(f"Query {qi}: 0 rows")
            continue
        cols = list(rows[0].keys())
        types = {c: _col_type([r.get(c) for r in rows]) for c in cols}
        desc = [f"Query {qi}: {len(rows)} row(s); columns: " + ", ".join(f"{c} ({types[c]})" for c in cols)]
        for c in cols:
            if types[c] == "number":
                nums = [_num(r.get(c)) for r in rows if _num(r.get(c)) is not None]
                if len(nums) > 1:
                    desc.append(f"  {c}: min {min(nums):g}, max {max(nums):g}")
        sample = rows[:8] if len(rows) <= 12 else rows[:6] + rows[-2:]
        desc.append("  sample rows: " + json.dumps(sample, default=str)[:2500])
        out.append("\n".join(desc))
    return "\n\n".join(out)


def _rows(sql_results, q):
    if not isinstance(q, int) or q < 0 or q >= len(sql_results):
        return None
    rows = sql_results[q]
    return rows if rows else None


def _has(rows, *cols):
    return all(c in rows[0] for c in cols if c)


def _cell(sql_results, q, col, row=0):
    rows = _rows(sql_results, q)
    if not rows or not _has(rows, col):
        return None
    row = row if isinstance(row, int) and 0 <= row < len(rows) else 0
    return _num(rows[row].get(col))


def _series_from(rows, x_col, y_col):
    pts = [(r.get(x_col), _num(r.get(y_col))) for r in rows]
    return [(x, y) for x, y in pts if y is not None]


def _sort_time(points):
    if points and all(_is_date(str(x)) for x, _ in points):
        return sorted(points, key=lambda p: str(p[0]))
    return points


# --------------------------------------------------------------------------- builders

def _kpis(spec, R):
    items = []
    for it in (spec.get("items") or [])[:4]:
        v = _cell(R, it.get("query"), it.get("column"), it.get("row", 0))
        if v is None:
            continue
        item = {"label": str(it.get("label") or it.get("column")), "value": v, "unit": _unit(it.get("unit"))}
        if it.get("compare_column") is not None:
            prev = _cell(R, it.get("compare_query", it.get("query")), it.get("compare_column"), it.get("compare_row", 0))
            if prev is not None:
                item["previous"] = prev
                item["change"] = (v - prev) / abs(prev) if prev else None
                item["compare_label"] = str(it.get("compare_label") or "vs previous")
        if it.get("spark_y"):
            rows = _rows(R, it.get("spark_query"))
            if rows and _has(rows, it.get("spark_x"), it.get("spark_y")):
                pts = _sort_time(_series_from(rows, it["spark_x"], it["spark_y"]))[-40:]
                if len(pts) >= 3:
                    item["spark"] = [y for _, y in pts]
        items.append(item)
    if not items:
        return None
    return {"items": items}


def _ranking(spec, R):
    rows = _rows(R, spec.get("query"))
    lc, vc = spec.get("label_column"), spec.get("value_column")
    if not rows or not _has(rows, lc, vc):
        return None
    items = [{"label": _label(r.get(lc)), "value": _num(r.get(vc))} for r in rows]
    items = [i for i in items if i["value"] is not None]
    if len(items) < 2:
        return None
    items.sort(key=lambda i: i["value"], reverse=True)
    return {"items": items[:MAX_ITEMS], "total_items": len(items), "value_label": str(spec.get("value_name") or vc), "label_name": lc}


def _composition(spec, R):
    out = _ranking(spec, R)
    if not out:
        return None
    out["items"] = [i for i in out["items"] if i["value"] > 0]
    return out if len(out["items"]) >= 2 else None


def _multi(spec, R, x_key):
    rows = _rows(R, spec.get("query"))
    xc = spec.get(x_key)
    vcs = [c for c in (spec.get("value_columns") or []) if c]
    if not rows or not vcs or not _has(rows, xc, *vcs):
        return None
    names = spec.get("series_names") or []
    series = []
    for i, c in enumerate(vcs[:6]):
        data = [_num(r.get(c)) for r in rows]
        if sum(d is not None for d in data) == 0:
            continue
        series.append({"name": str(names[i]) if i < len(names) and names[i] else c, "data": data})
    labels = [_label(r.get(xc)) for r in rows]
    if not series or len(labels) < 2:
        return None
    return labels, series


def _trend(spec, R):
    got = _multi(spec, R, "x_column")
    if not got:
        return None
    labels, series = got
    is_date = all(_is_date(l) for l in labels)
    if is_date:  # chronological, whatever order the SQL returned
        order = sorted(range(len(labels)), key=lambda i: labels[i])
        labels = [labels[i] for i in order]
        for s in series:
            s["data"] = [s["data"][i] for i in order]
    labels, series = labels[-MAX_ITEMS:], [{**s, "data": s["data"][-MAX_ITEMS:]} for s in series]
    return {"x": labels, "x_kind": "date" if is_date else "category", "series": series, "label_name": spec.get("x_column")}


def _comparison(spec, R):
    got = _multi(spec, R, "label_column")
    if not got:
        return None
    labels, series = got
    if len(series) < 2:
        return None
    return {"categories": labels[:MAX_ITEMS], "series": [{**s, "data": s["data"][:MAX_ITEMS]} for s in series], "label_name": spec.get("label_column")}


def _funnel(spec, R):
    steps = []
    if spec.get("steps"):
        for st in spec["steps"][:8]:
            v = _cell(R, st.get("query"), st.get("column"), st.get("row", 0))
            if v is not None:
                steps.append({"label": str(st.get("label") or st.get("column")), "value": v})
    else:
        rows = _rows(R, spec.get("query"))
        lc, vc = spec.get("label_column"), spec.get("value_column")
        if rows and _has(rows, lc, vc):
            steps = [{"label": _label(r.get(lc)), "value": _num(r.get(vc))} for r in rows[:8]]
            steps = [s for s in steps if s["value"] is not None]
    if len(steps) < 2:
        return None
    return {"steps": steps}


_ID_COL = re.compile(r"(^id$|_id$|uuid)", re.IGNORECASE)


def _table(spec, R):
    rows = _rows(R, spec.get("query"))
    if not rows:
        return None
    cols = [c for c in (spec.get("columns") or []) if c in rows[0] and not _ID_COL.search(c)]
    if not cols:
        cols = [c for c in rows[0].keys() if not _ID_COL.search(c)][:6]
    if not cols:
        return None
    units = spec.get("units") or {}
    columns = []
    for c in cols:
        t = _col_type([r.get(c) for r in rows])
        columns.append({"key": c, "label": c.replace("_", " "), "kind": t, "unit": _unit(units.get(c)) if t == "number" else None})
    data = [{c: r.get(c) for c in cols} for r in rows[:MAX_TABLE_ROWS]]
    return {"columns": columns, "rows": data, "total_rows": len(rows)}


def _heatmap(spec, R):
    rows = _rows(R, spec.get("query"))
    xc, yc, vc = spec.get("x_column"), spec.get("y_column"), spec.get("value_column")
    if not rows or not _has(rows, xc, yc, vc):
        return None
    xs, ys, cells = [], [], {}
    for r in rows:
        x, y, v = _label(r.get(xc)), _label(r.get(yc)), _num(r.get(vc))
        if v is None:
            continue
        if x not in xs:
            xs.append(x)
        if y not in ys:
            ys.append(y)
        cells[(x, y)] = cells.get((x, y), 0) + v
    if len(xs) < 2 or len(ys) < 2 or len(xs) > 40 or len(ys) > 30:
        return None
    if all(_is_date(x) for x in xs):
        xs.sort()
    values = [[cells.get((x, y)) for x in xs] for y in ys]
    return {"x": xs, "y": ys, "values": values}


BUILDERS = {
    "kpis": _kpis, "ranking": _ranking, "trend": _trend, "comparison": _comparison,
    "composition": _composition, "funnel": _funnel, "table": _table, "heatmap": _heatmap,
}


def build_cards(spec, sql_results):
    """spec (from the LLM) + the real rows -> list of renderable cards. Invalid specs are dropped."""
    cards = []
    for i, cs in enumerate((spec or {}).get("cards") or []):
        if not isinstance(cs, dict) or cs.get("type") not in CARD_TYPES:
            continue
        try:
            body = BUILDERS[cs["type"]](cs, sql_results)
        except Exception:
            body = None
        if not body:
            continue
        card = {"type": cs["type"], "title": str(cs.get("title") or "").strip()[:80] or cs["type"].title(), **body}
        if cs["type"] != "kpis" and cs["type"] != "table":
            card["unit"] = _unit(cs.get("unit"))
        cards.append(card)
        if len(cards) >= MAX_CARDS:
            break
    follow_ups = [str(f).strip() for f in ((spec or {}).get("follow_ups") or []) if str(f).strip()][:3]
    return {"cards": cards, "follow_ups": follow_ups}


def legacy_chart_data(cards):
    """The old single-chart shape, for frontends that don't know about cards yet."""
    for c in cards:
        t = c["type"]
        if t in ("ranking", "composition"):
            items = c["items"][:20]
            return {"chart_type": "pie" if t == "composition" else "bar", "title": c["title"],
                    "labels": [i["label"] for i in items], "series": [{"name": c.get("value_label", "Value"), "data": [i["value"] for i in items]}]}
        if t == "trend":
            return {"chart_type": "line", "title": c["title"], "labels": c["x"], "series": c["series"]}
        if t == "comparison":
            return {"chart_type": "bar", "title": c["title"], "labels": c["categories"], "series": c["series"]}
        if t == "funnel":
            return {"chart_type": "bar", "title": c["title"], "labels": [s["label"] for s in c["steps"]],
                    "series": [{"name": "Count", "data": [s["value"] for s in c["steps"]]}]}
        if t == "kpis" and len(c["items"]) >= 2 and len({i["unit"] for i in c["items"]}) == 1:
            return {"chart_type": "bar", "title": c["title"], "labels": [i["label"] for i in c["items"]],
                    "series": [{"name": "Value", "data": [i["value"] for i in c["items"]]}]}
    return None


async def extract_visuals(question, sql_results):
    """-> {"cards": [...], "follow_ups": [...]}; never raises."""
    empty = {"cards": [], "follow_ups": []}
    if not sql_results or not any(sql_results):
        return empty
    prompt = VISUALS_PROMPT.format(question=question, results=describe_results(sql_results), max_cards=MAX_CARDS)
    for model in VISUALS_MODELS:
        try:
            completion = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a precise analytics visualisation designer. Output strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            spec = json.loads(completion.choices[0].message.content)
            return build_cards(spec, sql_results)
        except Exception as e:  # model unavailable or malformed output: try the next one
            print(f"[visuals] {model} failed: {e}")
    return empty
