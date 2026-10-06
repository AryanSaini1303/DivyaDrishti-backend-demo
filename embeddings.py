import os
from openai import AsyncOpenAI #type:ignore

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


async def embed_text(text: str):
    response = await client.embeddings.create(model="text-embedding-3-large", input=text)
    return response.data[0].embedding