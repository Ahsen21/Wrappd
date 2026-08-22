# Wrappd

A Django app for visualizing stats from a [Letterboxd](https://letterboxd.com) data export, with optional TMDB enrichment (genre/director/cast/runtime). Two tools: **Director's Cut** (your personal dashboard from one export) and **Double Feature** (a side-by-side comparison from two exports).

## Setup

Requires Python 3.12+.

```bash
git clone https://github.com/Ahsen21/Wrappd.git
cd Wrappd
python -m venv venv

# Windows
venv\Scripts\pip install -r requirements-dev.txt   # or requirements.txt for prod-only deps
copy .env.example .env

# macOS / Linux
venv/bin/pip install -r requirements-dev.txt
cp .env.example .env

# then, either OS -- swap venv/Scripts/python for venv/bin/python on macOS/Linux
# (fill in TMDB_API_KEY in .env first -- get one free at themoviedb.org)
venv/Scripts/python manage.py migrate
venv/Scripts/python manage.py createsuperuser  # optional, for /admin/
venv/Scripts/python manage.py runserver
```

Visit `http://127.0.0.1:8000/` once the server's running. Without a `TMDB_API_KEY`, uploads still work — films just won't be enriched with genre/director/cast/runtime, and the dashboard will show a "not yet enriched" count.

## Tests

```bash
venv/Scripts/python manage.py test
```

## App layout

- `core` — landing page, shared base template/CSS
- `imports` — upload form, zip validation, CSV parsing, the parsed per-import models
- `tmdb` — the global TMDB metadata cache (shared across every import) and the API client
- `stats` — dashboard and comparison views, computed on the fly via the ORM

## Deferred to later

- **Auth**: `ImportSession` is anonymous (tracked by browser session key) by design. When accounts are added, add an `owner` FK to `AUTH_USER_MODEL` on `ImportSession` (see the model's docstring in [imports/models.py](imports/models.py)) — additive migration only, nothing else needs to change.
- Celery/background jobs for TMDB enrichment (current v1 is synchronous with a per-request cap, see `TMDB_ENRICHMENT_CAP` in settings).
- `lists/*.csv` and `comments.csv` from the export aren't parsed yet.
- Stale TMDB cache refresh policy (`Movie.fetched_at` exists for this).
