from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from episode_calendar.api import router as api_router
from episode_calendar.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="Episode Calendar", version="0.1.0")
    origins = [
        origin.strip() for origin in get_settings().cors_origins.split(",") if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
