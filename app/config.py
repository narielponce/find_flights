from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://buscaviajes:supersecretpassword@db:5432/buscaviajes_db"
    search_provider: str = "serpapi"  # "serpapi" | "amadeus" | "hybrid"

    amadeus_client_id: str = ""
    amadeus_client_secret: str = ""
    amadeus_base_url: str = "https://test.api.amadeus.com"

    serpapi_api_key: str = ""

    max_layover_minutes: int = 360  # 6 horas
    min_savings_percentage: float = 10.0
    default_currency: str = "USD"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

