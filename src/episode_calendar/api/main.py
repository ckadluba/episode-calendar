from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from episode_calendar.api.auth import router as auth_router
from episode_calendar.api.config import get_api_settings
from episode_calendar.api.routes import router as api_router
from episode_calendar.api.user_routes import router as user_router


def create_app() -> FastAPI:
    app = FastAPI(title="Episode Calendar", version="0.1.0")
    origins = [
        origin.strip() for origin in get_api_settings().cors_origins.split(",") if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    app.include_router(auth_router)
    app.include_router(user_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
