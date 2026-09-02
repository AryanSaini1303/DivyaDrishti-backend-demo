import os
from openai import OpenAI  # type:ignore
from dotenv import load_dotenv  # type:ignore
import json
from datetime import date
from industry_config import DOCS_MAP, META_MAP

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

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


def build_context(collection, query_embedding, top_k):
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )
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


def get_answer(question: str, conversation: list, origin: str, top_k: int = 20):
    if conversation is None:
        conversation = []

    classifier_completion = client.chat.completions.create(
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
    all_items = collection.get(include=["metadatas"])
    unique_docs = {
        meta["pdf_name"] for meta in all_items["metadatas"] if meta and "pdf_name" in meta
    }

    # --- Casual / small talk: no retrieval needed ---
    if query["query_type"] == "small_talk":
        return {
            "answer": "Hey! I'm Divya Drishti, your business analytics assistant. Ask me anything about "
                       "your reports — trends, comparisons, or specific figures.",
            "pages": [],
            "category": "Casual",
            "context_json": [],
            "chart_data": None
        }

    # --- Image-referencing questions: unchanged retrieval target (metadata collection) ---
    if query["query_type"] == "image_question":
        query_embedding = client.embeddings.create(
            model="text-embedding-3-large",
            input=query["query"]
        ).data[0].embedding
        context, context_json, pages = build_context(collection_meta, query_embedding, top_k)

        prompt = f"""
You are Divya Drishti, a business analytics assistant for the {origin} knowledge base. Answer the
user's question using ONLY the provided context whenever possible.
You have your knowledge base from the following reports:

{unique_docs}

Instructions:
- Do not omit important details and do not alter the wording or meaning of the context.
- Answer casual greetings and general conversation, but ignore questions outside business/operational context.
- Explicitly avoid abusive or inappropriate topics.
- Cite the report name and page number for every fact you include using this format: (Report: <pdf_name>, Page: <page_number>).
- Extract and include all relevant information from the context.
- If the context is insufficient, respond with: 'Couldn't find that in the provided materials.'
- Never fabricate or make up facts.
- Return STRICT JSON ONLY.

Context:
{context}

User Query:
{query['query']}

Output format:
{{
    "context_json": [
        {{"pdf_name": "<name>", "page_num": <int>, "content": "<text>", "score": 1.0}}
    ],
    "final_answer": "<direct answer>"
}}
"""
        completion = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "You are Divya Drishti. Follow instructions strictly."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        try:
            response_json = json.loads(completion.choices[0].message.content)
        except json.JSONDecodeError:
            return {
                "answer": "Couldn't find that in the provided materials.",
                "pages": [], "category": query["category"], "context_json": [], "chart_data": None
            }
        context_json = response_json.get("context_json", [])
        pages = [f"{p['pdf_name']} | Page {p['page_num']}" for p in context_json]
        return {
            "answer": response_json.get("final_answer", "Couldn't find that in the provided materials."),
            "pages": pages,
            "category": query["category"],
            "context_json": context_json,
            "chart_data": None
        }

    # --- Standard question: retrieve context ---
    query_embedding = client.embeddings.create(
        model="text-embedding-3-large",
        input=query["query"]
    ).data[0].embedding
    context, context_json, pages = build_context(collection, query_embedding, top_k)

    if not context_json:
        return {
            "answer": "No relevant info found.", "pages": [], "category": query["category"],
            "context_json": [], "chart_data": None
        }

    # --- Analytical: structured narrative + chart_data ---
    if query["category"] == "Analytical":
        analytical_prompt = f"""
You are Divya Drishti, a business analytics assistant for the {origin} knowledge base. Answer the
user's question using ONLY the provided context. Your answer will be used to render both a text
summary and, where the data supports it, a chart in a frontend dashboard.

You have your knowledge base from the following reports:
{unique_docs}

Instructions:
- Cite the report name and page number for every fact using this format: (Report: <pdf_name>, Page: <page_number>).
- Never fabricate numbers. Only use figures explicitly present in the context.
- If the context contains a clear time series or category breakdown that answers the question,
  populate chart_data. If the context does not support a clean chart (e.g. a single figure, or no
  comparable numeric series), set chart_data to null — do not force a chart onto data that doesn't fit one.
- chart_type must be one of "bar", "line", "pie", or null.
- Return STRICT JSON ONLY, no explanation outside the JSON.

Context:
{context}

User Query:
{query['query']}

Output format:
{{
    "narrative": "<conversational answer with citations, 2-4 sentences>",
    "chart_data": {{
        "chart_type": "bar" | "line" | "pie",
        "title": "<short chart title>",
        "labels": ["<label1>", "<label2>", "..."],
        "series": [
            {{"name": "<series name>", "data": [<number>, <number>, "..."]}}
        ]
    }}
}}
or, if no chart fits:
{{
    "narrative": "<conversational answer with citations>",
    "chart_data": null
}}
"""
        completion = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "You are Divya Drishti. Follow instructions strictly."},
                {"role": "user", "content": analytical_prompt}
            ],
            temperature=0.2
        )
        try:
            response_json = json.loads(completion.choices[0].message.content)
        except json.JSONDecodeError:
            # Fall back to raw text as the narrative rather than failing outright
            return {
                "answer": completion.choices[0].message.content.strip(),
                "pages": pages, "category": "Analytical", "context_json": context_json,
                "chart_data": None
            }
        return {
            "answer": response_json.get("narrative", "Couldn't find that in the provided materials."),
            "pages": pages,
            "category": "Analytical",
            "context_json": context_json,
            "chart_data": response_json.get("chart_data")
        }

    # --- Everything else (Document-Specific, Cross-Document, Contextual, Meta): plain prose ---
    completion = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are Divya Drishti, a business analytics assistant for the {origin} knowledge base. "
                    "Answer the user's question using only the provided context whenever possible. "
                    f"You have your knowledge base from the following reports:\n\n{unique_docs}\n\n"
                    "Do not omit important details and do not alter the wording or meaning of the context. "
                    "Answer casual greetings and general conversation, but ignore questions outside business "
                    "or operational context. Explicitly avoid abusive or inappropriate topics.\n\n"
                    "Cite the report name and page number for every fact you include using this format: "
                    "(Report: <pdf_name>, Page: <page_number>).\n"
                    "Extract and include all relevant information from the context. "
                    "If the context is insufficient, respond with: 'Couldn't find that in the provided materials.' "
                    "Never fabricate or make up facts. Make it clear when you're using general knowledge vs the "
                    "provided materials.\n\n"
                    "Additionally, you may answer questions about your knowledge base itself (e.g., listing the "
                    "reports you have, the number of pages, etc.) even if this information is not in the "
                    "user-provided context. For other meta-questions, only respond using known facts.\n\n"
                    "Avoid making tables in your responses."
                )
            },
            {"role": "user", "content": f"Here is the business context from your knowledge base:\n\n{context}"},
            {"role": "user", "content": f"Question: {query['query']}"}
        ],
        temperature=0.2
    )
    return {
        "answer": completion.choices[0].message.content.strip(),
        "pages": pages,
        "category": query["category"],
        "context_json": context_json,
        "chart_data": None
    }