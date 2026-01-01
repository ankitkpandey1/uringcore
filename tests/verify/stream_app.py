from starlette.applications import Starlette
from starlette.responses import StreamingResponse
import asyncio

async def stream():
    for i in range(1000):
        yield f"{i}\n".encode()
        await asyncio.sleep(0)

app = Starlette()
app.add_route("/stream", lambda req: StreamingResponse(stream()))
