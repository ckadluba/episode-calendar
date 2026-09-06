from fastapi import FastAPI

from episode_calendar.api import router as api_router


def create_app() -> FastAPI:
    app = FastAPI(title="Episode Calendar", version="0.1.0")
    app.include_router(api_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
