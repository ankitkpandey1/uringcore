from fastapi import FastAPI
import asyncio
import os

app = FastAPI()

@app.get("/ping")
async def ping():
    await asyncio.sleep(0.001)
    return {"ok": True}

@app.get("/echo/{x}")
async def echo(x: int):
    return {"x": x}

@app.on_event("startup")
async def startup():
    print(f"Running on pid {os.getpid()}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
