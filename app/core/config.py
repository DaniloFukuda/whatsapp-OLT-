from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OLT Gestão de Resíduos & Demolições"
    env: str = "development"

    database_url: str = "sqlite:///./olt_entulhos.db"
    media_dir: str = "./media"

    whatsapp_verify_token: str = "troque_este_token"
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_api_version: str = "v25.0"

    owner_name: str = "Lucas"
    owner_whatsapp: str = ""

    whatsapp_owner_phone: str = ""
    authorized_operator_phone: str = ""
    authorized_operator_phones: str = ""
    timezone: str = "Europe/Lisbon"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
