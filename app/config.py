from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    staff_bot_token: str
    chairman_bot_token: str
    chairman_chat_id: int
    broker_api_secret: str

    broker_host: str = "127.0.0.1"
    broker_port: int = 8089
    database_path: str = "./data/tasks.db"


settings = Settings()
