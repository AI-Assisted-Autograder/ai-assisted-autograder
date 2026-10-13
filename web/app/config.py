from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://autograder:autograder@db:5432/autograder"
    integration_api_token: str | None = None
    feedback_state_path: str | None = None


settings = Settings()
