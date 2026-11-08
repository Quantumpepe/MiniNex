
from fastapi import FastAPI

app = FastAPI(
    title="MiniNex API",
    version="0.1.0"
)

@app.get("/")
def home():
    return {
        "app": "MiniNex",
        "status": "online",
        "trading": "disabled"
    }

@app.get("/health")
def health():
    return {"status": "ok"}
