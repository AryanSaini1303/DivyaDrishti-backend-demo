import boto3 #type: ignore
from fastapi import FastAPI, Request, File, UploadFile, Request, HTTPException, Header, Query #type: ignore
from datetime import datetime, timedelta
from pydantic import BaseModel #type: ignore
from app.query import get_answer
from fastapi.middleware.cors import CORSMiddleware #type: ignore
import os
import fitz #type: ignore
import re
from openai import OpenAI #type: ignore
from dotenv import load_dotenv #type: ignore
import tiktoken #type: ignore
import nltk #type: ignore 
from typing import List
import requests #type: ignore
from app.query import get_answer
from industry_config import INDUSTRIES, DOCS_MAP, META_MAP
from fastapi import Depends #type: ignore

nltk.download('punkt')

enc = tiktoken.encoding_for_model("text-embedding-3-large")

s3_client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-south-1"))
S3_BUCKET = os.getenv("S3_BUCKET")

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
assert OPENAI_API_KEY, "Missing OPENAI_API_KEY in .env"

PDF_PREFIX = "pdfs" 

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    # allow_origins=["*"],
    allow_origins=["http://localhost:3000","https://iit-roorkee-bot.vercel.app", "https://damchat.in"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def num_tokens(text):
    return len(enc.encode(text))

def split_into_chunks(text, max_tokens=800, overlap=100):
    words = text.split()
    chunks = []
    current_chunk = []
    for word in words:
        test_chunk = current_chunk + [word]
        test_text = ' '.join(test_chunk)
        if num_tokens(test_text) <= max_tokens:
            current_chunk = test_chunk
        else:
            if current_chunk:
                chunks.append(' '.join(current_chunk))
            current_chunk = current_chunk[-overlap:] if overlap > 0 else []
    if current_chunk:
        chunks.append(' '.join(current_chunk))
    return chunks

def validate_origin(x_origin: str = Header(None)) -> str:
    if x_origin not in INDUSTRIES:
        raise HTTPException(status_code=400, detail=f"Invalid industry: {x_origin}")
    return x_origin

class QueryRequest(BaseModel):
    question: str
    conversation: list
    origin:str

@app.post("/ask")
async def ask_question(req: QueryRequest):
    if req.origin not in INDUSTRIES:
        raise HTTPException(status_code=400, detail=f"Invalid industry: {req.origin}")
    result = get_answer(req.question, req.conversation, req.origin)
    return {
        "answer": result["answer"],
        "pages": result["pages"],
        "category": result["category"],
        "context": result["context_json"],
        "chart_data": result["chart_data"]
    }

@app.post("/add")
async def add_main_pdfs(files: List[UploadFile] = File(...), x_origin: str = Depends(validate_origin)):
    try:
        # print(f"Origin Header: {x_origin}")
        processed_files = []
        errors = []
        for file in files:
            temp_path = f"./temp_{file.filename}"
            with open(temp_path, "wb") as f:
                f.write(await file.read())
            pdf_name = os.path.splitext(file.filename)[0]
            doc = fitz.open(temp_path)
            for i in range(len(doc)):
                page_num = i + 1
                raw_text = doc[i].get_text().strip()
                if not raw_text:
                    continue
                clean_text = re.sub(r'\s+', ' ', raw_text)
                chunks = split_into_chunks(clean_text)
                for idx, chunk in enumerate(chunks):
                    try:
                        embedding = client.embeddings.create(
                            model="text-embedding-3-large",
                            input=chunk
                        ).data[0].embedding
                        chunk_id = f"{pdf_name}_page_{page_num}_chunk_{idx}"
                        collection=DOCS_MAP[x_origin]
                        collection.add(
                            documents=[chunk],
                            embeddings=[embedding],
                            ids=[chunk_id],
                            metadatas=[{
                                "page": page_num,
                                "pdf_name": pdf_name,
                                "chunk_index": idx
                            }]
                        )
                    except Exception as e:
                        errors.append({
                            "file": file.filename,
                            "chunk_id": chunk_id,
                            "error": str(e)
                        })
            os.remove(temp_path)
            print(file.filename)
            processed_files.append(file.filename)
        return {
            "status": "completed",
            "files_processed": processed_files,
            "errors": errors
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
    
@app.post("/add_metadata")
async def add_metadata_pdfs(files: List[UploadFile] = File(...), x_origin: str = Depends(validate_origin)):
    processed_files = []
    errors = []
    for file in files:
        temp_path = f"./temp_{file.filename}"
        with open(temp_path, "wb") as f:
            f.write(await file.read())
        pdf_name = os.path.splitext(file.filename)[0]
        doc = fitz.open(temp_path)
        for i in range(len(doc)):
            page_num = i + 1
            raw_text = doc[i].get_text().strip()
            if not raw_text:
                continue
            clean_text = re.sub(r'\s+', ' ', raw_text)
            chunks = split_into_chunks(clean_text)
            for idx, chunk in enumerate(chunks):
                try:
                    embedding = client.embeddings.create(
                        model="text-embedding-3-large",
                        input=chunk
                    ).data[0].embedding
                    chunk_id = f"metadata_{pdf_name}_page_{page_num}_chunk_{idx}"
                    collection=META_MAP[x_origin]
                    collection.add(
                        documents=[chunk],
                        embeddings=[embedding],
                        ids=[chunk_id],
                        metadatas=[{
                            "page": page_num,
                            "pdf_name": pdf_name,
                            "chunk_index": idx
                        }]
                    )
                except Exception as e:
                    errors.append({
                        "file": file.filename,
                        "chunk_id": chunk_id,
                        "error": str(e)
                    })
        os.remove(temp_path)
        processed_files.append(file.filename)
    return {
        "status": "completed",
        "files_processed": processed_files,
        "errors": errors
    }
    
@app.get("/list-pdfs")
async def list_pdfs(x_origin: str = Depends(validate_origin)):
    collection = DOCS_MAP[x_origin]
    all_items = collection.get(include=["metadatas"])
    unique_pdfs = set()
    for meta in all_items["metadatas"]:
        if meta and "pdf_name" in meta:
            unique_pdfs.add(meta["pdf_name"])
    result = []
    for pdf_name in unique_pdfs:
        key = f"{PDF_PREFIX}/{pdf_name}.pdf"
        view_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=3600  # 1 hour, matches your Azure version
        )
        result.append({"name": pdf_name, "viewUrl": view_url})
    return {"pdfs": result}

@app.get("/getViewUrl")
def get_view_url(filename: str):
    key = f"{PDF_PREFIX}/{filename}"
    view_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=3600
    )
    return {"viewUrl": view_url}

@app.delete("/delete-pdf")
def delete_pdf(pdf_name: str, x_origin: str = Header(None)):
    if x_origin not in DOCS_MAP:
        raise HTTPException(status_code=400, detail="Invalid origin")
    key = f"{PDF_PREFIX}/{pdf_name}.pdf"
    try:
        s3_client.delete_object(Bucket=S3_BUCKET, Key=key)
    except Exception as e:
        print(f"S3 delete warning: {e}")  # don't block vector cleanup on this
    for collection in (DOCS_MAP[x_origin], META_MAP[x_origin]):
        items = collection.get(include=["metadatas"])
        ids_to_delete = [
            id_ for id_, meta in zip(items["ids"], items["metadatas"])
            if meta and meta.get("pdf_name") == pdf_name
        ]
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
    return {"status": "deleted", "pdf_name": pdf_name}

@app.get("/getUploadSas")
def get_upload_sas(filename: str):
    key = f"pdfs/{filename}"
    upload_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=300
    )
    return {"uploadUrl": upload_url}

@app.get("/generate-upload-url")
def generate_upload_url(filename: str):
    key = f"images/{filename}"
    upload_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=600
    )
    return {"uploadUrl": upload_url}