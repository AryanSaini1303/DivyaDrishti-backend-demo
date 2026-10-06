import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from orchestrator import stream_answer
from industry_config import INDUSTRIES
from db import close_pool

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://aivoiceagent-bya9.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown():
    await close_pool()


class QueryRequest(BaseModel):
    question: str
    conversation: list
    origin: str


@app.post("/ask")
async def ask_question(req: QueryRequest):
    if req.origin not in INDUSTRIES:
        raise HTTPException(status_code=400, detail=f"Invalid industry: {req.origin}")

    async def event_generator():
        async for event, data in stream_answer(req.question, req.conversation, req.origin):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")