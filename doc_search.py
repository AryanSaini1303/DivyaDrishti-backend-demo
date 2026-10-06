from industry_config import DOCS_MAP
from embeddings import embed_text #type:ignore

async def search_documents(query: str, origin: str):
    collection = DOCS_MAP[origin]
    embedding = await embed_text(query)
    results = collection.query(query_embeddings=[embedding], n_results=6, include=["documents", "metadatas"])
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    return {
        "chunks": [
            {"source": m["pdf_name"], "page": m["page"], "text": d}
            for d, m in zip(docs, metas)
        ]
    }