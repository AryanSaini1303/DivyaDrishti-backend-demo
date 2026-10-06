from db import execute_readonly_sql #type:ignore
from doc_search import search_documents #type:ignore

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": "Run a read-only SELECT against the live Postgres database for current figures, records, or aggregates.",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Semantic search over policy docs, PPTs, and reference PDFs.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


async def run_tool(name: str, args: dict, origin: str):
    if name == "query_database":
        return await execute_readonly_sql(args["sql"])
    if name == "search_documents":
        return await search_documents(args["query"], origin)
    return {"error": f"unknown tool {name}"}