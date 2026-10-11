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

    cursor_api_key: str = ""
    reply_model: str = ""
    agy_bin: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    tgstat_token: str = ""

    # Jev (TypeSafe System One) — оценка заявок. Ключ из console.typesafe.ai
    # Допускается и JEV_API_KEY, и TYPESAFE_API_KEY (ниже через validator не нужен —
    # читаем оба имени полями).
    jev_api_key: str = ""
    typesafe_api_key: str = ""
    jev_base_url: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"

    bot_token: str = ""

    @property
    def jev_key(self) -> str:
        return (self.jev_api_key or self.typesafe_api_key or "").strip()

    host: str = "127.0.0.1"
    port: int = 8787

    @property
    def has_telegram_api(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def session_file_exists(self) -> bool:
        return Path(f"{self.session_path}.session").exists()


settings = Settings()
