"""
Configuration module for managing secrets and settings.
Uses environment variables for security.
"""

import json
import os
from pathlib import Path

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv not installed, skip loading .env file
    pass


class Config:
    """Configuration class that loads settings from environment variables"""

    def __init__(self):
        # Silae credentials
        self.SILAE_BASE_URL = os.getenv("SILAE_BASE_URL")
        self.SILAE_USERNAME = os.getenv("SILAE_USERNAME")
        self.SILAE_PASSWORD = os.getenv("SILAE_PASSWORD")
        self.EMPLOYEE_ID = os.getenv("EMPLOYEE_ID", "1689166228")
        self.SILAE_VERIFY_SSL = (
            os.getenv("SILAE_VERIFY_SSL", "true").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self.SILAE_CA_BUNDLE = os.getenv("SILAE_CA_BUNDLE")

        # Google Calendar settings (service account)
        # Either a path to the service account JSON key file...
        self.GOOGLE_CREDENTIALS_FILE = os.getenv(
            "GOOGLE_CREDENTIALS_FILE", "credentials/google_credentials.json"
        )
        # ...or the JSON content itself (takes precedence when set)
        self.GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
        self.HOTEL_CALENDAR_ID = os.getenv("HOTEL_CALENDAR_ID")

        # App settings
        self.TIMEZONE = os.getenv("TIMEZONE", "Europe/Paris")
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        # Validate required settings
        self._validate_config()

    def _validate_config(self):
        """Validate that all required configuration is present"""
        required_vars = [
            ("SILAE_USERNAME", self.SILAE_USERNAME),
            ("SILAE_PASSWORD", self.SILAE_PASSWORD),
            ("HOTEL_CALENDAR_ID", self.HOTEL_CALENDAR_ID),
        ]

        missing_vars = [
            var_name for var_name, var_value in required_vars if not var_value
        ]

        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )

    def get_credentials_path(self) -> str:
        """Get the full path to the Google service account key file"""
        return os.path.abspath(self.GOOGLE_CREDENTIALS_FILE)

    def get_credentials_info(self):
        """Parse GOOGLE_CREDENTIALS_JSON if set, else return None"""
        if not self.GOOGLE_CREDENTIALS_JSON:
            return None
        try:
            return json.loads(self.GOOGLE_CREDENTIALS_JSON)
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_CREDENTIALS_JSON is not valid JSON") from exc

    def ensure_credentials_dir(self):
        """Ensure the credentials directory exists"""
        Path(self.GOOGLE_CREDENTIALS_FILE).parent.mkdir(parents=True, exist_ok=True)


# Global config instance
config = Config()
