from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    qq_app_id: str = ""
    qq_app_secret: str = ""
    zkill_user_agent: str = ""

    database_url: str = "postgresql+asyncpg://eve_risk:eve_risk@postgres:5432/eve_risk"
    redis_url: str = "redis://redis:6379/0"
    log_level: str = "INFO"

    health_host: str = "0.0.0.0"
    health_port: int = 8080

    analysis_window_days: int = 90
    recent_weight_days: int = 30
    analysis_fetch_deadline_seconds: int = 240
    analysis_reply_deadline_seconds: int = 270
    zkill_request_interval_seconds: float = 1.2
    zkill_cache_ttl_seconds: int = 1800
    qq_context_ttl_seconds: int = 600
    friendly_character_ids: str = ""
    friendly_corporation_ids: str = ""
    friendly_alliance_ids: str = ""

    esi_base_url: str = "https://esi.evetech.net/latest"
    sde_url: str = (
        "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
    )
    sde_index_path: str = "/data/sde.sqlite3"
    zkill_base_url: str = "https://zkillboard.com/api"
    qq_token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    qq_api_base_url: str = "https://api.sgroup.qq.com"

    max_characters: int = 30
    global_max_jobs: int = 3
    member_rate_limit_seconds: int = 60
    group_job_ttl_seconds: int = 330
    report_width: int = 1440
    report_max_height: int = 4096
    font_path: str | None = None

    @field_validator("zkill_user_agent")
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        normalized = value.lower()
        if value and ("contact-pending" in normalized or "email@example.com" in normalized):
            raise ValueError("ZKILL_USER_AGENT must contain real maintainer contact details")
        return value

    def require_qq(self) -> None:
        missing = [
            name
            for name, value in (
                ("QQ_APP_ID", self.qq_app_id),
                ("QQ_APP_SECRET", self.qq_app_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required QQ settings: {', '.join(missing)}")

    def require_zkill(self) -> None:
        if not self.zkill_user_agent:
            raise RuntimeError("ZKILL_USER_AGENT is required")

    @property
    def friendly_character_id_set(self) -> set[int]:
        return _parse_id_list(self.friendly_character_ids)

    @property
    def friendly_corporation_id_set(self) -> set[int]:
        return _parse_id_list(self.friendly_corporation_ids)

    @property
    def friendly_alliance_id_set(self) -> set[int]:
        return _parse_id_list(self.friendly_alliance_ids)


def _parse_id_list(value: str) -> set[int]:
    if not value.strip():
        return set()
    result: set[int] = set()
    for item in value.replace("，", ",").replace(";", ",").split(","):
        normalized = item.strip()
        if not normalized:
            continue
        entity_id = int(normalized)
        if entity_id <= 0:
            raise ValueError("Friendly entity IDs must be positive integers")
        result.add(entity_id)
    return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
