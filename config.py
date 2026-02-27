import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    main_admin_id: int
    db_path: str = "bot.db"
    user_data_dir: str = "user_data"
    logs_dir: str = "logs"


def get_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN")
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    main_admin_id = os.getenv("MAIN_ADMIN_ID")

    if not bot_token or not api_id or not api_hash or not main_admin_id:
        raise RuntimeError(
            "BOT_TOKEN, API_ID, API_HASH and MAIN_ADMIN_ID must be set in environment or .env"
        )

    db_path = os.getenv("DB_PATH", "bot.db")
    user_data_dir = os.getenv("USER_DATA_DIR", "user_data")
    logs_dir = os.getenv("LOGS_DIR", "logs")

    return Settings(
        bot_token=bot_token,
        api_id=int(api_id),
        api_hash=api_hash,
        main_admin_id=int(main_admin_id),
        db_path=db_path,
        user_data_dir=user_data_dir,
        logs_dir=logs_dir,
    )


settings = get_settings()
