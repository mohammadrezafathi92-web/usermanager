# The licence control server - a small, self-contained FastAPI app that runs
# ONLY on the vendor's own machine. Nothing here is shared with the panel
# image on purpose (separate service, separate database, separate login).
FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY store.py app.py signing.py ledger.py console_auth.py console_deps.py ui.py views.py detail_views.py issue_views.py issue_routes.py ./
COPY static ./static

# The DB lives on a mounted volume so revoke state and the console password
# survive restarts and rebuilds.
ENV LICENSE_DB_URL=sqlite:////data/license.db

EXPOSE 9000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
