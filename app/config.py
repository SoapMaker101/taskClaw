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
    upload_dir: str = "./data/files"
    max_file_size_mb: int = 20

    # Background reminder loop interval (seconds).
    reminder_poll_seconds: int = 60
    # Gentle nudges for still-open (sent) tasks so assignees do not forget.
    # First nudge after this many hours from delivery (sent_at); repeats every interval.
    # Set max to 0 to disable idle nudges (overdue-only reminders still apply when due_at is set).
    idle_reminder_interval_hours: int = 24
    idle_reminder_max_per_task: int = 5

    # Reminders only in local wall-clock window [start, end), e.g. 08:00–18:59.
    reminder_timezone: str = "Europe/Moscow"
    reminder_window_start_hour: int = 8
    reminder_window_end_hour: int = 19

    # Pre-deadline idle nudges only if (due_at - sent_at) >= this many hours (default 72).
    long_task_reminder_min_hours: int = 72

    max_group_members: int = 200


settings = Settings()
