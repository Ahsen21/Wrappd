"""Thin wrapper around the TMDB v3 REST API. No caching here -- that's enrichment.py's job."""

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.themoviedb.org/3'
REQUEST_TIMEOUT = 5  # seconds
MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_RETRY_AFTER = 1  # seconds, used if TMDB doesn't send a Retry-After header


class TMDBClientError(Exception):
    """Raised when TMDB is unreachable, misconfigured, or still rate-limited after
    retrying. Callers should treat this as 'enrichment unavailable right now' -- NOT
    as 'this film doesn't exist on TMDB' -- so a caller must never cache this as a
    negative match; see enrichment.py's _resolve_and_cache."""


def _session():
    session = requests.Session()
    session.params = {'api_key': settings.TMDB_API_KEY}
    return session


def _get(path: str, params: dict) -> requests.Response:
    """GET with a bounded retry-with-backoff on HTTP 429 (rate limited). A bulk
    enrichment run can fire hundreds of sequential requests, which is exactly the
    scenario that trips TMDB's rate limit -- retrying here means a rate limit hit
    doesn't get mistaken for 'this film doesn't exist'."""
    session = _session()
    response = None
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        try:
            response = session.get(f'{BASE_URL}{path}', params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning('TMDB request to %s failed: %s', path, exc)
            raise TMDBClientError(str(exc)) from exc

        if response.status_code != 429:
            break
        retry_after = float(response.headers.get('Retry-After', DEFAULT_RETRY_AFTER))
        logger.info('TMDB rate limit hit on %s, retrying in %.1fs (attempt %d)', path, retry_after, attempt + 1)
        time.sleep(retry_after)

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('TMDB request to %s failed: %s', path, exc)
        raise TMDBClientError(str(exc)) from exc

    return response


def search_movie(title: str, year: int | None) -> list:
    """Return every TMDB search result for a title (+optional year), best-ranked
    first, or an empty list if no match. Returns the full list rather than just the
    top hit because TMDB's relevance ranking doesn't always put the exact title+year
    match first -- e.g. searching 'Harry Potter and the Deathly Hallows: Part 2'
    (2011) ranks 'Part 1' (2010) above it -- so the caller (_is_real_match in
    enrichment.py) needs to scan candidates itself rather than trust result #1."""
    if not settings.TMDB_API_KEY:
        raise TMDBClientError('TMDB_API_KEY is not configured.')

    params = {'query': title}
    if year:
        params['year'] = year

    response = _get('/search/movie', params)
    return response.json().get('results') or []


def search_tv(title: str, year: int | None) -> list:
    """Return every TMDB TV search result for a title (+optional first-air year),
    best-ranked first, or an empty list if no match. Used only as a follow-up when
    search_movie finds no exact match, to positively confirm 'this is TV, not an
    unmatched movie' -- see enrichment.py."""
    if not settings.TMDB_API_KEY:
        raise TMDBClientError('TMDB_API_KEY is not configured.')

    params = {'query': title}
    if year:
        params['first_air_date_year'] = year

    response = _get('/search/tv', params)
    return response.json().get('results') or []


def get_movie_details(tmdb_id: int) -> dict:
    """Fetch full movie details plus credits (director + top cast) in one call."""
    if not settings.TMDB_API_KEY:
        raise TMDBClientError('TMDB_API_KEY is not configured.')

    response = _get(f'/movie/{tmdb_id}', {'append_to_response': 'credits'})
    return response.json()
