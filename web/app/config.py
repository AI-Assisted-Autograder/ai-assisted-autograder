from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://autograder:autograder@db:5432/autograder"
    integration_api_token: str | None = None
    feedback_state_path: str | None = None
    pitt_key_encryption_key: str | None = None
    # Signs the instructor session cookie. Unset means a random key per
    # process, so every restart signs everyone out.
    session_secret: str | None = None
    # Set true once the UI is served over HTTPS.
    session_cookie_secure: bool = False


settings = Settings()
