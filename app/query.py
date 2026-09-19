import os
import json
import asyncio
from openai import AsyncOpenAI  # type:ignore
from dotenv import load_dotenv  # type:ignore
from datetime import date
from industry_config import DOCS_MAP, META_MAP

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

CLASSIFIER_PROMPT = (
    "You are a query reformulator and classifier for Divya Drishti, a multi-industry business "
    "analytics assistant. Your job is threefold:\n"
    "1. Rewrite the user's latest question into a precise, standalone business question (if applicable).\n"
    "2. Classify the user's query into one of 6 categories.\n"
    "3. Detect if the question is about an image.\n\n"

    f"If the user's question includes words like 'latest', 'current', 'recent', or 'upcoming', "
    f"include the current date ({date.today()}) in the reformulated query.\n\n"

    "### Rules for Reformulation:\n"
    "- Always include all necessary context from history in the rewritten query.\n"
    "- The query must be **a complete, grammatically correct question** "
    "(starting with what, why, how, when, where, which, etc.).\n"
    "- Keep the query concise but detailed enough for accurate search.\n"
    "- Remove filler words, greetings, or irrelevant chatter.\n"
    "- Never output vague phrases, keywords, or incomplete fragments.\n\n"

    "### Categories:\n"
    "- **Casual**: Greetings or small talk (hi, hello, how are you, thanks, etc.).\n"
    "- **Document-Specific**: The question targets content that clearly belongs to a single report only. "
    "Example: \"What was Q1 2026 revenue?\"\n"
    "- **Cross-Document**: The question requires looking at multiple reports, or aggregating/comparing info "
    "across them. Example: \"Compare Q1 2025 and Q1 2026 performance.\"\n"
    "- **Contextual**: Questions that rely on conversation history or follow-ups "
    "(e.g., 'Explain that in more detail', 'What about the margins?').\n"
    "- **Meta**: Questions about the knowledge base itself "
    "(e.g., 'Which reports do you have?', 'How many documents are there?').\n"
    "- **Analytical**: Questions that ask for a trend, comparison, ranking, or breakdown that is naturally "
    "expressed as numbers alongside a category or time axis — the kind of answer a chart would help show. "
    "Example: \"How has quarterly revenue trended?\", \"Which region grew fastest?\", \"Break down revenue "
    "by category.\" Use this category even if the question also fits Document-Specific or Cross-Document — "
    "Analytical takes priority whenever the answer is fundamentally a set of comparable numbers.\n\n"

    "### Query Types:\n"
    "- **small_talk**: Casual conversation, greetings, or chit-chat.\n"
    "- **question**: Standard business/report-related question (not about images).\n"
    "- **image_question**: Any query that explicitly refers to an image, describes an image, "
    "asks about image content, or requests information based on an image.\n\n"

    "### Output format:\n"
    "- For casual conversation:\n"
    "{\n"
    "  \"query_type\": \"small_talk\",\n"
    "  \"query\": \"<user's casual message>\",\n"
    "  \"category\": \"Casual\"\n"
    "}\n\n"

    "- For image-related questions:\n"
    "{\n"
    "  \"query_type\": \"image_question\",\n"
    "  \"query\": \"<fully constructed standalone question referencing an image>\",\n"
    "  \"category\": \"<one of: Document-Specific, Cross-Document, Contextual, Meta, Analytical>\"\n"
    "}\n\n"

    "- For standard questions:\n"
    "{\n"
    "  \"query_type\": \"question\",\n"
    "  \"query\": \"<fully constructed standalone question>\",\n"
    "  \"category\": \"<one of: Document-Specific, Cross-Document, Contextual, Meta, Analytical>\"\n"
    "}\n\n"

    "Respond only with a valid JSON object — no explanation, no extra text."
)

CHART_EXTRACTION_PROMPT_TEMPLATE = """You are extracting chart-ready data for a business analytics
dashboard. Given the context and question below, determine whether the context contains a clean,
comparable numeric series (a time trend or category breakdown) that directly answers the question.

Context:
{context}

Question:
{question}

Instructions:
- Never fabricate numbers. Only use figures explicitly present in the context.
- If the context contains a clear time series or category breakdown that answers the question,
  return chart_data with chart_type ("bar", "line", or "pie"), a short title, labels, and one or
  more series with numeric data.
- If the context does not support a clean chart (a single figure, or no comparable numeric series),
  return chart_data: null. Do not force a chart onto data that doesn't fit one.
- Return STRICT JSON ONLY in this shape:
{{"chart_data": {{"chart_type": "bar|line|pie", "title": "...", "labels": ["..."], "series": [{{"name": "...", "data": [0]}}]}} }}
or:
{{"chart_data": null}}
"""


async def build_context_async(collection, query_embedding, top_k):
    def _query():
        return collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"]
        )
    results = await asyncio.to_thread(_query)

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    distances = results["distances"][0]
    scored_results = sorted(
        [
            {"document": doc.strip(), "metadata": meta, "score": 1 - dist}
            for doc, meta, dist in zip(docs, metas, distances)
        ],
        key=lambda x: x["score"],
        reverse=True
    )
    context = ""
    context_json = []
    pages = []
    seen = set()
    for item in scored_results:
        source = item["metadata"]["pdf_name"]
        page = item["metadata"]["page"]
        page_key = f"{source} | Page {page}"
        if page_key not in seen:
            seen.add(page_key)
            pages.append(page_key)
        context += f"[{page_key}] (score={item['score']:.2f})\n{item['document']}\n\n"
        context_json.append({
            "pdf_name": source,
            "page_num": page,
            "content": item["document"],
            "score": item["score"]
        })
    return context, context_json, pages


async def stream_narrative(context: str, question: str, origin: str, unique_docs):
    """Async generator yielding narrative text chunks as they arrive from the model."""
    messages = [
        {
            "role": "system",
            "content": (
                f"You are Divya Drishti, a business analytics assistant for the {origin} knowledge base. "
                "Answer the user's question using only the provided context whenever possible. "
                f"You have your knowledge base from the following reports:\n\n{unique_docs}\n\n"
                "Do not omit important details and do not alter the wording or meaning of the context. "
                "Answer casual greetings and general conversation, but ignore questions outside business "
                "or operational context. Explicitly avoid abusive or inappropriate topics.\n\n"
                # "Cite the report name and page number for every fact you include using this format: "
                # "(Report: <pdf_name>, Page: <page_number>).\n"
                "Extract and include all relevant information from the context. "
                "If the context is insufficient, respond with: 'Couldn't find that in the provided materials.' "
                "Never fabricate or make up facts. Make it clear when you're using general knowledge vs the "
                "provided materials.\n\n"
                "Additionally, you may answer questions about your knowledge base itself (e.g., listing the "
                "reports you have, the number of pages, etc.) even if this information is not in the "
                "user-provided context. For other meta-questions, only respond using known facts.\n\n"
                "Write in plain prose only. Do not use markdown formatting of any kind — no asterisks, "
                "no bold text, no numbered lists, no bullet points, no headers. This response will be "
                "read aloud by a voice assistant and shown as flowing ambient text, not rendered as a "
                "document, so formatting symbols would be spoken or displayed literally and break the "
                "experience. Write as you would speak: complete sentences, natural paragraph breaks only.\n\n"
                "Avoid making tables in your responses."
            )
        },
        {"role": "user", "content": f"Here is the business context from your knowledge base:\n\n{context}"},
        {"role": "user", "content": f"Question: {question}"}
    ]
    stream = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.2,
        stream=True
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


async def extract_chart_data(context: str, question: str):
    """Runs concurrently alongside stream_narrative — resolves independently, never blocks it."""
    prompt = CHART_EXTRACTION_PROMPT_TEMPLATE.format(context=context, question=question)
    completion = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are a precise data extraction assistant. Follow instructions strictly."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        response_format={"type": "json_object"}
    )
    try:
        parsed = json.loads(completion.choices[0].message.content)
        return parsed.get("chart_data")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


async def extract_image_answer(context: str, question: str, origin: str, unique_docs):
    """Image-referencing questions stay single-shot (rare path, needs structured per-page JSON)."""
    prompt = f"""
You are Divya Drishti, a business analytics assistant for the {origin} knowledge base. Answer the
user's question using ONLY the provided context whenever possible.
You have your knowledge base from the following reports:

{unique_docs}

Instructions:
- Do not omit important details and do not alter the wording or meaning of the context.
- Cite the report name and page number for every fact using this format: (Report: <pdf_name>, Page: <page_number>).
- If the context is insufficient, respond with: 'Couldn't find that in the provided materials.'
- Never fabricate facts. Write in plain prose only — no markdown, no asterisks, no bullet points, since
  this will be read aloud and shown as flowing text.
- Return STRICT JSON ONLY.

Context:
{context}

User Query:
{question}

Output format:
{{"final_answer": "<direct answer>"}}
"""
    completion = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are Divya Drishti. Follow instructions strictly."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        response_format={"type": "json_object"}
    )
    try:
        return json.loads(completion.choices[0].message.content)
    except (json.JSONDecodeError, AttributeError):
        return {"final_answer": "Couldn't find that in the provided materials."}


async def stream_answer(question: str, conversation: list, origin: str, top_k: int = 8):
    """
    Async generator yielding (event_name, data_dict) tuples:
      "meta"       — category + source pages, sent once, early
      "token"      — narrative text chunks, sent repeatedly as they stream in
      "chart_data" — sent once, after narrative streaming completes (resolved concurrently, not sequentially)
      "done"       — sent once, signals the stream is finished
    Transport-agnostic — main.py formats these into SSE.
    """
    if conversation is None:
        conversation = []

    classifier_completion = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": CLASSIFIER_PROMPT},
            {"role": "user", "content": f"Conversation history:\n{conversation}"},
            {"role": "user", "content": f"User's question:\n{question}"}
        ],
        temperature=0.2
    )
    query = json.loads(classifier_completion.choices[0].message.content.strip())

    collection = DOCS_MAP[origin]
    collection_meta = META_MAP[origin]
    all_items = await asyncio.to_thread(collection.get, include=["metadatas"])
    unique_docs = {m["pdf_name"] for m in all_items["metadatas"] if m and "pdf_name" in m}

    # --- Casual: no retrieval, canned reply ---
    if query["query_type"] == "small_talk":
        yield ("meta", {"category": "Casual", "pages": []})
        yield ("token", {"text": "Hey! I'm Divya Drishti, your business analytics assistant. Ask me "
                                  "anything about your reports — trends, comparisons, or specific figures."})
        yield ("chart_data", {"chart_data": None})
        yield ("done", {})
        return

    # --- Image-referencing: single-shot, not streamed (rare path) ---
    if query["query_type"] == "image_question":
        query_embedding = (await client.embeddings.create(
            model="text-embedding-3-large", input=query["query"]
        )).data[0].embedding
        context, context_json, pages = await build_context_async(collection_meta, query_embedding, top_k)
        result = await extract_image_answer(context, query["query"], origin, unique_docs)
        yield ("meta", {"category": query["category"], "pages": pages})
        yield ("token", {"text": result.get("final_answer", "Couldn't find that in the provided materials.")})
        yield ("chart_data", {"chart_data": None})
        yield ("done", {})
        return

    # --- Standard question: retrieve context ---
    query_embedding = (await client.embeddings.create(
        model="text-embedding-3-large", input=query["query"]
    )).data[0].embedding
    context, context_json, pages = await build_context_async(collection, query_embedding, top_k)

    if not context_json:
        yield ("meta", {"category": query["category"], "pages": []})
        yield ("token", {"text": "No relevant info found."})
        yield ("chart_data", {"chart_data": None})
        yield ("done", {})
        return

    yield ("meta", {"category": query["category"], "pages": pages})

    # chart_data extraction kicks off in the background here and resolves independently —
    # it does NOT block narrative streaming from starting. Runs for every standard
    # question now, not just ones the classifier happened to label "Analytical" — the
    # extraction prompt already has its own "return null if no clean series exists"
    # logic, so gating it a second time behind the classifier's category guess only
    # created false negatives (a correct-but-conservative category call silently
    # killing a chart that should have existed).
    chart_task = asyncio.create_task(extract_chart_data(context, query["query"]))

    async for delta in stream_narrative(context, query["query"], origin, unique_docs):
        yield ("token", {"text": delta})

    chart_data = await chart_task
    yield ("chart_data", {"chart_data": chart_data})
    yield ("done", {})