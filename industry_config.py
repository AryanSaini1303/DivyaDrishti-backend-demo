import chromadb  # type: ignore

INDUSTRIES = [
    "MEDICAL",
    "B2C",
    "WHOLESALE_FASHION",
    "TECH_MANUFACTURING",
]

chroma_client = chromadb.PersistentClient(path="./VECTOR_DB")

DOCS_MAP = {
    industry: chroma_client.get_or_create_collection(name=f"{industry}_DOCS")
    for industry in INDUSTRIES
}

META_MAP = {
    industry: chroma_client.get_or_create_collection(name=f"{industry}_METADATA")
    for industry in INDUSTRIES
}