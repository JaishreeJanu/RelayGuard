from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.notifications import router as notifications_router
from app.core.arq import close_arq_pool, init_arq_pool

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_arq_pool()
    yield
    await close_arq_pool()


app = FastAPI(
    title="RelayGuard API Engine",
    description="A Standalone, Fault-Tolerant Notification Engine & Webhook Gateway",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(notifications_router, prefix="/api/v1")


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "healthy"}
