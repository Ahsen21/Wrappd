"""Pure comparison logic between two ImportSessions. No scipy dependency -- the
'agreement' metric is a simple hand-rolled % of shared films rated within 0.5 stars
of each other, which is easy to explain and good enough for a learning project."""

from decimal import Decimal

from django.db.models import Count

from imports.models import DiaryEntry, RatingEntry
from stats.services.filters import exclude_tv_shows

AGREEMENT_THRESHOLD = Decimal('0.5')
TOP_N = 10
# An average of a single shared rated film isn't meaningful -- avg_delta requires at
# least this many shared rated films, or it's left out entirely.
MIN_COUNT_FOR_AVERAGE = 2


def _film_map(import_session):
    """One row per (title, year) for this session, combining the authoritative rating
    (ratings.csv) with the movie FK / title fallback from diary.csv.

    Keyed by (title, year), not letterboxd_uri: Letterboxd's "Letterboxd URI" column is
    a per-log-entry short link, not a stable per-film id -- the same film gets a
    *different* boxd.it code in diary.csv than in ratings.csv. (title, year) is the
    same key TMDB matching already uses (see tmdb/services/enrichment.py), so this
    keeps film identity consistent across the whole app."""
    films = {}

    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    for r in rated.select_related('movie'):
        films[(r.title, r.year)] = {'title': r.title, 'year': r.year, 'rating': r.rating, 'movie_id': r.movie_id}

    diary = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session))
    for d in diary.select_related('movie').order_by('watched_date'):
        key = (d.title, d.year)
        entry = films.setdefault(key, {'title': d.title, 'year': d.year, 'rating': None, 'movie_id': None})
        if d.movie_id and not entry.get('movie_id'):
            entry['movie_id'] = d.movie_id
        if entry.get('rating') is None and d.rating is not None:
            entry['rating'] = d.rating

    return films


def _top_genres(import_session, limit=5):
    return list(
        DiaryEntry.objects.filter(import_session=import_session, movie__genres__isnull=False)
        .values('movie__genres__name')
        .annotate(count=Count('id'))
        .order_by('-count')[:limit]
    )


def build_compare_context(session_a, session_b) -> dict:
    map_a = _film_map(session_a)
    map_b = _film_map(session_b)

    keys_a, keys_b = set(map_a), set(map_b)
    shared_keys = keys_a & keys_b
    only_a_keys = keys_a - keys_b
    only_b_keys = keys_b - keys_a

    shared_films = []
    rated_deltas = []
    for key in shared_keys:
        a, b = map_a[key], map_b[key]
        rating_a, rating_b = a['rating'], b['rating']
        delta = abs(rating_a - rating_b) if (rating_a is not None and rating_b is not None) else None
        shared_films.append({
            'title': a['title'] or b['title'],
            'year': a['year'] or b['year'],
            'rating_a': rating_a,
            'rating_b': rating_b,
            'delta': delta,
        })
        if delta is not None:
            rated_deltas.append(delta)

    rated_shared = [f for f in shared_films if f['delta'] is not None]
    biggest_disagreements = sorted(rated_shared, key=lambda f: f['delta'], reverse=True)[:TOP_N]
    most_agreed = sorted(rated_shared, key=lambda f: f['delta'])[:TOP_N]

    agree_count = sum(1 for d in rated_deltas if d <= AGREEMENT_THRESHOLD)
    union_size = len(keys_a | keys_b)

    return {
        'session_a': session_a,
        'session_b': session_b,
        'shared_count': len(shared_keys),
        'only_a_count': len(only_a_keys),
        'only_b_count': len(only_b_keys),
        'overlap_pct': round(len(shared_keys) / union_size * 100, 1) if union_size else 0,
        'agreement_pct': round(agree_count / len(rated_shared) * 100, 1) if rated_shared else None,
        'avg_delta': (
            round(float(sum(rated_deltas) / len(rated_deltas)), 1)
            if len(rated_deltas) >= MIN_COUNT_FOR_AVERAGE
            else None
        ),
        'biggest_disagreements': biggest_disagreements,
        'most_agreed': most_agreed,
        'only_a_films': sorted((map_a[k] for k in only_a_keys), key=lambda f: f['title']),
        'only_b_films': sorted((map_b[k] for k in only_b_keys), key=lambda f: f['title']),
        'genres_a': _top_genres(session_a),
        'genres_b': _top_genres(session_b),
    }
