import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_TYPE = os.getenv("DATABASE_TYPE")
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_TYPE == "postgres" or DATABASE_URL:
    from .postgres import get_connection
else:
    from .sqlite import get_connection
