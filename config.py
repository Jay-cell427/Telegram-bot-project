import os
from dotenv import load_dotenv
from typing import List

load_dotenv()

class Config:
    # Telegram
    TOKEN = os.getenv('TOKEN')
    ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
    ADMIN_CHANNEL_ID = os.getenv('ADMIN_CHANNEL_ID')
    ADVERTISING_CHANNEL = os.getenv('ADVERTISING_CHANNEL')
    ADVERTISING_CHANNEL_INVITE_LINK = os.getenv('ADVERTISING_CHANNEL_INVITE_LINK')
    ADVERTISING_CHANNEL_ID = os.getenv('ADVERTISING_CHANNEL_ID')
    
    # Database
    DB_NAME = os.getenv('DB_NAME')
    DB_USER = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_HOST = os.getenv('DB_HOST')
    DB_PORT = os.getenv('DB_PORT')
    
    DATABASE = f"dbname='{DB_NAME}' user='{DB_USER}' " \
               f"password='{DB_PASSWORD}' host='{DB_HOST}' " \
               f"port='{DB_PORT}'"
    
    # Payments
    PAYMENT_PROVIDER_TOKEN = os.getenv('PAYMENT_PROVIDER_TOKEN')
    CURRENCY = os.getenv('CURRENCY')
    PRICE_AMOUNT = int(os.getenv('PRICE_AMOUNT', 1))
    
    # System
    REQUEST_EXPIRY_HOURS = int(os.getenv('REQUEST_EXPIRY_HOURS', 24))
    MEMBERSHIP_CHECK_INTERVAL = int(os.getenv('MEMBERSHIP_CHECK_INTERVAL', 86400))
    CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', 3600))
    
    GOOGLE_DRIVE_CREDENTIALS_PATH = os.getenv('GOOGLE_DRIVE_CREDENTIALS_PATH')
    GOOGLE_DRIVE_CONTENT_FOLDER_ID = os.getenv('GOOGLE_DRIVE_CONTENT_FOLDER_ID')
    
    @staticmethod
    def validate():
        required = {
            'Telegram': ['TOKEN', 'ADMIN_ID', 'ADMIN_CHANNEL_ID', 'ADVERTISING_CHANNEL', 'ADVERTISING_CHANNEL_INVITE_LINK'],
            'Database': ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT'],
            'Payments': ['PAYMENT_PROVIDER_TOKEN']
        }
        
        errors = []
        for category, vars in required.items():
            for var in vars:
                if not getattr(Config, var):
                    errors.append(f"{category} - {var}")
        
        if errors:
            raise EnvironmentError(
                "Missing required configuration:\n" +
                "\n".join(errors) +
                "\n\nPlease check your .env file"
            )

try:
    Config.validate()
except EnvironmentError as e:
    print(f"❌ Configuration error: {e}")
    exit(1)