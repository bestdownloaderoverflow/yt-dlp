"""Configuration settings for the server"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Settings:
    """Application settings"""
    
    # Server settings
    PORT: int = int(os.getenv('PORT', '3021'))
    BASE_URL: str = os.getenv('BASE_URL', 'http://localhost:3021')
    
    # Security
    ENCRYPTION_KEY: str = os.getenv('ENCRYPTION_KEY', 'overflow')
    
    # Paths
    TEMP_DIR: Path = Path(os.getenv('TEMP_DIR', './temp'))
    COOKIES_PATH: Path = Path(os.getenv('COOKIES_PATH', './cookies/www.tiktok.com_cookies.txt'))
    
    # Performance
    MAX_WORKERS: int = int(os.getenv('MAX_WORKERS', '20'))
    
    # Timeouts
    YTDLP_TIMEOUT: int = int(os.getenv('YTDLP_TIMEOUT', '30'))
    DOWNLOAD_TIMEOUT: int = int(os.getenv('DOWNLOAD_TIMEOUT', '120'))


# Global settings instance
settings = Settings()
