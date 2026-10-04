import datetime as dt
import logging

from jose import jwt
from passlib.context import CryptContext

from .config import settings

logger = logging.getLogger("security")

# passlib 1.7.4 reads bcrypt.__about__.__version__, which bcrypt >= 4.1 no
# longer has. passlib traps the AttributeError and carries on - hashing and
# verifying are unaffected - but it logs the whole traceback at WARNING on
# the first password check after every start. That one line made every
# "grep Traceback" on the backend log look like something had broken.
logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.ERROR)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def create_access_token(subject: str) -> str:
    expire = dt.datetime.utcnow() + dt.timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload.get("sub")
    except Exception as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None
