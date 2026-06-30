import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_TYPE = os.getenv("DATABASE_TYPE", "sqlite")

if DATABASE_TYPE == "postgres":
    from .postgres import get_connection
else:
    from .sqlite import get_connection