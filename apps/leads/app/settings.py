from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    session_path: Path = ROOT / "data" / "telegram"
    db_path: Path = ROOT / "data" / "leads.db"
    sources_path: Path = ROOT / "config" / "sources.yaml"
    profile_path: Path = ROOT / "config" / "profile.yaml"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    bot_token: str = ""

    host: str = "127.0.0.1"
    port: int = 8787

    @property
    def has_telegram_api(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def session_file_exists(self) -> bool:
        return Path(f"{self.session_path}.session").exists()


settings = Settings()
