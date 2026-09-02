import sys
import chromadb #type:ignore

CHROMA_PATH = "../CWC_DB"
COLLECTION_NAME = "CWC_DOCS"

def delete_file(file_name: str):
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_or_create_collection(name=COLLECTION_NAME)
        results = collection.get(where={"pdf_name": file_name})
        if not results["ids"]:
            print(f"⚠️ No entries found for file: {file_name}")
            return
        collection.delete(ids=results["ids"])
        print(f"✅ Deleted {len(results['ids'])} chunks for file: {file_name}")
    except Exception as e:
        print(f"❌ Error deleting file {file_name}: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python delete_file.py <file_name>")
    else:
        delete_file(sys.argv[1])

# python test.py "MetaData Test Sample"