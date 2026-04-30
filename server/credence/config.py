"""Runtime config — loaded once from env, hard-fails on missing required keys.

Reads `.env.local` from the repo root so the server and frontend share the
same Supabase project and Anthropic key without duplication.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env.local", REPO_ROOT / "server" / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres — same DB the frontend reads via supabase-js. We bypass PostgREST
    # for write paths and complex joins.
    database_url: str = Field(..., alias="DATABASE_URL")

    # Anthropic Claude. Server-side only, never shipped to browser.
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    # CORS origins for /chat etc. Local dev only for v0.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8080",
            "http://localhost:5173",
            "http://127.0.0.1:8080",
        ]
    )

    # Connection pool sizing. Supabase pooler caps at ~15 client conns on free.
    db_pool_min: int = 2
    db_pool_max: int = 8


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
