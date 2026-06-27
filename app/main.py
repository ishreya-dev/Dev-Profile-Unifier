import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.routers import health, profiles
from app.services.database import init_db_client
from app.services.observer import metrics

from app.core.limiter import limiter


# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logger = logging.getLogger("api")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        logger.info(
            f"{request.method} {request.url.path} from {request.client.host}"
        )

        response = await call_next(request)

        logger.info(
            f"{request.method} {request.url.path} → {response.status_code}"
        )

        return response


# -----------------------------------------------------------------------------
# Rate Limiter
# -----------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)


# -----------------------------------------------------------------------------
# Lifespan
# -----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_client()
    metrics.start_timer()

    yield

    # Shutdown tasks (if needed)


# -----------------------------------------------------------------------------
# FastAPI App
# -----------------------------------------------------------------------------

app = FastAPI(
    title="Dev Profile Unifier",
    description="Pull, merge, and summarise public developer identities.",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter

# -----------------------------------------------------------------------------
# Middleware
# -----------------------------------------------------------------------------

app.add_middleware(LoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SlowAPIMiddleware)


# -----------------------------------------------------------------------------
# Exception Handlers
# -----------------------------------------------------------------------------

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded",
            "error": str(exc),
        },
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "message": "Dev Profile Unifier API",
        "docs": "/docs",
        "health": "/health",
    }


app.include_router(profiles.router, prefix="/profiles", tags=["profiles"])
app.include_router(health.router, tags=["observability"])