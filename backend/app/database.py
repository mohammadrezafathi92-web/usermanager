from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

connect_args = {}
engine_kwargs = {}
is_sqlite = settings.database_url.startswith("sqlite")
is_mysql = settings.database_url.startswith("mysql")
if is_sqlite:
    # timeout: how long a connection waits on a locked db before raising
    # "database is locked" - raised to make room for concurrent writers
    # (web requests + background poller + RADIUS auth/accounting threads).
    connect_args = {"check_same_thread": False, "timeout": 15}
elif is_mysql:
    # MySQL/MariaDB close idle connections server-side (wait_timeout,
    # default 8h but often lower on managed hosts) - pool_pre_ping avoids
    # "MySQL server has gone away" by testing the connection before use,
    # and pool_recycle proactively retires connections before the server
    # would drop them. charset=utf8mb4 matches the pymysql driver default
    # for full unicode (emoji etc.) support.
    connect_args = {"charset": "utf8mb4"}
    engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 280}

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

if is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        # WAL lets readers and a writer work concurrently instead of
        # blocking each other - important here since the RADIUS server
        # (auth + accounting, each its own thread) and the usage-polling
        # scheduler all hit the same SQLite file as the web API.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
