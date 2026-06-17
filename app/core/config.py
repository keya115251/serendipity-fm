from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    lastfm_api_key: str
    lastfm_api_secret: str = ""
    database_path: str = "./data/graph_cache.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
