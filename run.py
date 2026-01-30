#!/usr/bin/env python3
"""
Run script for WhatsApp Bot Dashboard
"""
import os
import sys
import asyncio
from pathlib import Path

# Fix for Windows asyncio subprocess issue (must be before any async operations)
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Add the project directory to Python path
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))


def generate_secret_key():
    """Generate a secure secret key"""
    import secrets
    return secrets.token_urlsafe(32)


def generate_encryption_key():
    """Generate a Fernet encryption key"""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def setup_env():
    """Setup .env file if it doesn't exist"""
    env_file = PROJECT_DIR / ".env"
    env_example = PROJECT_DIR / ".env.example"

    if not env_file.exists() and env_example.exists():
        print("Creating .env file from .env.example...")

        with open(env_example, 'r') as f:
            content = f.read()

        # Generate keys
        content = content.replace(
            'SECRET_KEY=your-super-secret-key-change-this-in-production',
            f'SECRET_KEY={generate_secret_key()}'
        )
        content = content.replace(
            'ENCRYPTION_KEY=',
            f'ENCRYPTION_KEY={generate_encryption_key()}'
        )

        with open(env_file, 'w') as f:
            f.write(content)

        print(".env file created with generated keys.")


def check_dependencies():
    """Check if required dependencies are installed"""
    # Map of pip package name -> import name
    required = {
        'fastapi': 'fastapi',
        'uvicorn': 'uvicorn',
        'sqlalchemy': 'sqlalchemy',
        'passlib': 'passlib',
        'python-jose': 'jose',
        'jinja2': 'jinja2',
        'cryptography': 'cryptography',
        'python-dotenv': 'dotenv'
    }
    missing = []

    for package, import_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(package)

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Install with: pip install -r requirements.txt")
        sys.exit(1)


def main():
    """Main entry point"""
    # Setup environment
    setup_env()

    # Check dependencies
    check_dependencies()

    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()

    # Configure logging - output to both console and file
    import logging

    # Create logs directory
    os.makedirs("logs", exist_ok=True)

    # Setup logging to file and console
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("logs/bot.log", encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    logging.info("=== Server Starting ===")

    # Import and run the app
    import uvicorn
    from app.config import settings

    print(f"""
================================================================
            WhatsApp Bot Dashboard
================================================================
  Server running at: http://{settings.HOST}:{settings.PORT}
  Debug mode: {str(settings.DEBUG).lower()}
  Press Ctrl+C to stop
================================================================
    """)

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info"
    )


if __name__ == "__main__":
    main()
