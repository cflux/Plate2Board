from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import generate, parse, version


app = FastAPI(title="Keyboard KiCad AutoDesigner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8011"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(parse.router, prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(version.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok"}
