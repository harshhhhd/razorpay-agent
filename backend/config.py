"""
config.py

Single source of truth for all environment-driven configuration.
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)


class Settings:
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")

    DAILY_BUDGET_INR: float = float(os.getenv("DAILY_BUDGET_INR", "8000"))
    MAX_DISCOUNT_PCT: float = float(os.getenv("MAX_DISCOUNT_PCT", "25"))

    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    CORS_ORIGINS: list = os.getenv("CORS_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500").split(",")

    DB_PATH: str = os.getenv("DB_PATH", "copilot.db")

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.RAZORPAY_KEY_ID and self.RAZORPAY_KEY_SECRET)

    @property
    def groq_configured(self) -> bool:
        return bool(self.GROQ_API_KEY)


settings = Settings()

logger = logging.getLogger("growth_copilot")
if not settings.razorpay_configured:
    logger.warning(
        "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. "
        "Campaign approval (create_offer) will fail until they're added to .env."
    )
if not settings.groq_configured:
    logger.info(
        "GROQ_API_KEY not set — campaign copy will use deterministic templates. "
        "This is a safe, expected fallback, not an error."
    )