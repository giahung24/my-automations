"""
Configuration module for managing secrets and settings.
Uses environment variables for security.
"""

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
        self.SILAE_USERNAME = os.getenv('SILAE_USERNAME')
        self.SILAE_PASSWORD = os.getenv('SILAE_PASSWORD')
        self.EMPLOYEE_ID = os.getenv('EMPLOYEE_ID', '1689166228')
        
        # Google Calendar settings
        self.GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials/google_credentials.json')
        self.GOOGLE_TOKEN_FILE = os.getenv('GOOGLE_TOKEN_FILE', 'credentials/token.pickle')
        self.HOTEL_CALENDAR_ID = os.getenv('HOTEL_CALENDAR_ID')
        
        # App settings
        self.TIMEZONE = os.getenv('TIMEZONE', 'Europe/Paris')
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
        
        # Validate required settings
        self._validate_config()
    
    def _validate_config(self):
        """Validate that all required configuration is present"""
        required_vars = [
            ('SILAE_USERNAME', self.SILAE_USERNAME),
            ('SILAE_PASSWORD', self.SILAE_PASSWORD),
            ('HOTEL_CALENDAR_ID', self.HOTEL_CALENDAR_ID),
        ]
        
        missing_vars = [var_name for var_name, var_value in required_vars if not var_value]
        
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
    
    def get_credentials_path(self) -> str:
        """Get the full path to Google credentials file"""
        return os.path.abspath(self.GOOGLE_CREDENTIALS_FILE)
    
    def get_token_path(self) -> str:
        """Get the full path to Google token file"""
        return os.path.abspath(self.GOOGLE_TOKEN_FILE)
    
    def ensure_credentials_dir(self):
        """Ensure the credentials directory exists"""
        cred_dir = Path(self.GOOGLE_CREDENTIALS_FILE).parent
        cred_dir.mkdir(exist_ok=True)
        
        token_dir = Path(self.GOOGLE_TOKEN_FILE).parent
        token_dir.mkdir(exist_ok=True)


# Global config instance
config = Config()
