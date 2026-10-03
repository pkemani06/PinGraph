from fastapi import FastAPI

from app.routes import health

app = FastAPI(title="PinGraph")
app.include_router(health.router)
