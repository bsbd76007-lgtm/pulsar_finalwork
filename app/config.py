from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    bot_token: str = ""
    admin_telegram_ids: str = ""
    app_env: str = "development"
    payments_enabled: bool = False
    crypto_pay_api_token: str = ""
    crypto_pay_api_base_url: str = "https://pay.crypt.bot/api"
    crypto_pay_webhook_secret: str = ""
    database_url: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def admin_ids(self) -> set[int]:
        result = set()
        for item in self.admin_telegram_ids.split(","):
            item = item.strip()
            if item.isdigit():
                result.add(int(item))
        return result

settings = Settings()
