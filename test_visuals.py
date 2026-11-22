"""Offline tests for visuals.build_cards (no OpenAI or database needed).

    python test_visuals.py
"""
import json
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
from visuals import build_cards, describe_results, legacy_chart_data  # noqa: E402

# Shaped like real query_database results (asyncpg rows -> dicts, Decimal -> float, dates -> ISO)
R = [
    # 0: revenue by vendor
    [{"vendor": "Arvind Mills", "revenue": 4231000.0}, {"vendor": "Raymond", "revenue": 3120500.5},
     {"vendor": "Siyaram", "revenue": 2210000.0}, {"vendor": "Vardhman", "revenue": "1500000"},
     {"vendor": "Welspun", "revenue": None}],
    # 1: this month / last month totals
    [{"this_month": 1840000.0, "last_month": 1640000.0, "orders": 212, "orders_prev": 230}],
    # 2: monthly revenue (out of order on purpose)
    [{"month": "2026-10-01", "revenue": 1640000.0}, {"month": "2026-08-01", "revenue": 1210000.0},
     {"month": "2026-09-01", "revenue": 1480000.0}, {"month": "2026-11-01", "revenue": 1840000.0}],
    # 3: ordered vs received per SKU
    [{"sku": "KUR-RED-M", "ordered": 120, "received": 96}, {"sku": "KUR-BLU-L", "ordered": 80, "received": 80},
     {"sku": "SAR-GRN", "ordered": 50, "received": 31}],
    # 4: lead funnel
    [{"stage": "Leads", "n": 480}, {"stage": "Qualified", "n": 210}, {"stage": "Won", "n": 96}, {"stage": "Delivered", "n": 88}],
    # 5: overdue invoices (record list)
    [{"id": "8c1e…", "invoice_no": "WI-1042", "customer": "Shree Textiles", "amount": 84000.0, "days_overdue": 34},
     {"id": "1d2f…", "invoice_no": "WI-1019", "customer": "Lakshmi Fabrics", "amount": 51250.0, "days_overdue": 21}],
    # 6: orders by weekday x hour
    [{"weekday": d, "hour": h, "orders": (i * 7 + j * 3) % 11} for i, d in enumerate(["Mon", "Tue", "Wed"]) for j, h in enumerate(["10", "11", "12", "13"])],
    # 7: empty
    [],
]

SPEC = {
    "cards": [
        {"type": "kpis", "title": "Revenue this month", "items": [
            {"label": "Revenue", "query": 1, "column": "this_month", "unit": "currency",
             "compare_column": "last_month", "compare_label": "vs last month",
             "spark_query": 2, "spark_x": "month", "spark_y": "revenue"},
            {"label": "Orders", "query": 1, "column": "orders", "unit": "count", "compare_column": "orders_prev"}]},
        {"type": "ranking", "title": "Top vendors by revenue", "query": 0, "label_column": "vendor", "value_column": "revenue", "unit": "currency"},
        {"type": "trend", "title": "Revenue by month", "query": 2, "x_column": "month", "value_columns": ["revenue"], "unit": "currency"},
        {"type": "comparison", "title": "Ordered vs received", "query": 3, "label_column": "sku", "value_columns": ["ordered", "received"], "series_names": ["Ordered", "Received"], "unit": "quantity"},
        {"type": "funnel", "title": "Lead funnel", "query": 4, "label_column": "stage", "value_column": "n"},  # 5th card: over the cap
    ],
    "follow_ups": ["Break this down by franchise", "Which SKUs drive this?", "Compare with last quarter", "one too many"],
}


def test_main():
    out = build_cards(SPEC, R)
    cards = out["cards"]
    assert [c["type"] for c in cards] == ["kpis", "ranking", "trend", "comparison"], [c["type"] for c in cards]
    k = cards[0]["items"]
    assert k[0]["value"] == 1840000.0 and abs(k[0]["change"] - 0.12195) < 1e-4 and k[0]["spark"] == [1210000.0, 1480000.0, 1640000.0, 1840000.0]
    assert k[1]["change"] < 0 and k[1]["unit"] == "count"
    rk = cards[1]
    assert [i["label"] for i in rk["items"]] == ["Arvind Mills", "Raymond", "Siyaram", "Vardhman"]  # sorted, None dropped, "1500000" parsed
    assert rk["unit"] == "currency" and rk["total_items"] == 4 and rk["label_name"] == "vendor"
    tr = cards[2]
    assert tr["x"] == ["2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"] and tr["x_kind"] == "date"
    assert tr["series"][0]["data"] == [1210000.0, 1480000.0, 1640000.0, 1840000.0] and tr["label_name"] == "month"
    cp = cards[3]
    assert cp["categories"] == ["KUR-RED-M", "KUR-BLU-L", "SAR-GRN"] and [s["name"] for s in cp["series"]] == ["Ordered", "Received"]
    assert out["follow_ups"] == ["Break this down by franchise", "Which SKUs drive this?", "Compare with last quarter"]


def test_other_types():
    spec = {"cards": [
        {"type": "funnel", "title": "Lead funnel", "query": 4, "label_column": "stage", "value_column": "n"},
        {"type": "funnel", "title": "Lead funnel (steps)", "steps": [{"label": "Leads", "query": 4, "column": "n", "row": 0}, {"label": "Won", "query": 4, "column": "n", "row": 2}]},
        {"type": "table", "title": "Overdue invoices", "query": 5, "columns": ["id", "invoice_no", "customer", "amount", "days_overdue"], "units": {"amount": "currency", "days_overdue": "days"}},
        {"type": "heatmap", "title": "Orders by weekday and hour", "query": 6, "x_column": "hour", "y_column": "weekday", "value_column": "orders", "unit": "count"},
    ]}
    cards = build_cards(spec, R)["cards"]
    assert [c["type"] for c in cards] == ["funnel", "funnel", "table", "heatmap"]
    assert [s["value"] for s in cards[0]["steps"]] == [480, 210, 96, 88]
    assert [s["value"] for s in cards[1]["steps"]] == [480, 96]
    t = cards[2]
    assert [c["key"] for c in t["columns"]] == ["invoice_no", "customer", "amount", "days_overdue"]  # id column refused
    assert t["columns"][2]["unit"] == "currency" and t["columns"][3]["unit"] == "days" and t["columns"][1]["unit"] is None
    h = cards[3]
    assert h["x"] == ["10", "11", "12", "13"] and h["y"] == ["Mon", "Tue", "Wed"] and len(h["values"]) == 3 and len(h["values"][0]) == 4


def test_bad_specs_are_dropped():
    spec = {"cards": [
        {"type": "ranking", "query": 0, "label_column": "vendor", "value_column": "nope"},        # missing column
        {"type": "ranking", "query": 0, "label_column": "revenue", "value_column": "vendor"},    # text as value
        {"type": "trend", "query": 7, "x_column": "month", "value_columns": ["revenue"]},        # empty result
        {"type": "pie3d", "query": 0},                                                          # unknown type
        {"type": "kpis", "items": [{"query": 9, "column": "x"}]},                               # no such query
        {"type": "comparison", "query": 3, "label_column": "sku", "value_columns": ["ordered"]}, # one series only
        "garbage",
    ]}
    assert build_cards(spec, R)["cards"] == []
    assert build_cards(None, R) == {"cards": [], "follow_ups": []}
    assert build_cards({"cards": "x"}, R)["cards"] == []


def test_legacy_and_description():
    cards = build_cards(SPEC, R)["cards"]
    leg = legacy_chart_data(cards)
    assert leg["chart_type"] == "bar" and leg["labels"][0] == "Arvind Mills"  # first chartable card (kpis mixes units)
    d = describe_results(R)
    assert "Query 0: 5 row(s)" in d and "revenue (number)" in d and "month (date)" in d and "Query 7: 0 rows" in d
    json.dumps(build_cards(SPEC, R))  # wire-serialisable


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
