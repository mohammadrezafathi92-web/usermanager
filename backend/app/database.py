from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

connect_args = {}
is_sqlite = settings.database_url.startswith("sqlite")
if is_sqlite:
    # timeout: how long a connection waits on a locked db before raising
    # "database is locked" - raised to make room for concurrent writers
    # (web requests + background poller + RADIUS auth/accounting threads).
    connect_args = {"check_same_thread": False, "timeout": 15}

engine = create_engine(settings.database_url, connect_args=connect_args)
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
