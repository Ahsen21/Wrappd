"""Pure comparison logic between two ImportSessions. No scipy dependency -- the
'agreement' metric is a simple hand-rolled % of shared films rated within 0.5 stars
of each other, which is easy to explain and good enough for a learning project."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg, Count

from imports.models import DiaryEntry, RatingEntry, WatchlistEntry
from stats.services.filters import exclude_tv_shows
from tmdb.models import Movie

AGREEMENT_THRESHOLD = Decimal('0.5')
TOP_N = 10
# An average of a single shared rated film isn't meaningful -- avg_delta requires at
# least this many shared rated films, or it's left out entirely.
MIN_COUNT_FOR_AVERAGE = 2
# The fixed 0.5-5.0 rating scale, hardcoded as exact strings rather than derived via
# Decimal division -- division's "ideal exponent" rules can silently produce
# Decimal('2') instead of Decimal('2.0') for a clean half-integer result, which
# compares/hashes equal (fine for dict lookups) but formats inconsistently via str()
# (not fine for the chart's x-axis labels, which need uniform '0.5'/'1.0'/... text).
RATING_BUCKETS = [Decimal(v) for v in ('0.5', '1.0', '1.5', '2.0', '2.5', '3.0', '3.5', '4.0', '4.5', '5.0')]


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


def _watchlist_map(import_session):
    """(title, year) -> {title, year, movie_id} for this session's watchlist.csv.
    Its own identity space, not merged with _film_map's rated/diary data (a film on
    the watchlist hasn't been watched) -- intersected independently between the two
    sessions in build_compare_context."""
    films = {}
    watchlist = exclude_tv_shows(WatchlistEntry.objects.filter(import_session=import_session))
    for w in watchlist:
        films[(w.title, w.year)] = {'title': w.title, 'year': w.year, 'movie_id': w.movie_id}
    return films


def _films_by_date(import_session) -> dict:
    """watched_date -> ['Title (Year)', ...] for this session's diary.csv -- a date
    can have more than one entry (a marathon day, or a rewatch logged the same day
    as something else), so every value is a list, never a single film."""
    entries = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session)).order_by('title')
    by_date = defaultdict(list)
    for title, year, watched_date in entries.values_list('title', 'year', 'watched_date'):
        by_date[watched_date].append(f'{title} ({year})' if year else title)
    return by_date


def _same_day_logs(session_a, session_b) -> list:
    """Dates both sessions logged at least one film on -- not necessarily the *same*
    film (that's shared_films/same_rating elsewhere on this page). A same-day viewing
    pattern, not a same-taste one."""
    dates_a = _films_by_date(session_a)
    dates_b = _films_by_date(session_b)
    shared_dates = set(dates_a) & set(dates_b)

    results = [
        {'date': date, 'films_a': dates_a[date], 'films_b': dates_b[date]}
        for date in shared_dates
    ]
    results.sort(key=lambda r: r['date'], reverse=True)
    return results


def _five_star_only(import_session, other_watched_keys):
    """Films this session rated a perfect 5.0 that the other session has no record of
    at all -- not just a different rating. other_watched_keys is the other session's
    _film_map key set (rated union diary), reusing the same 'watched' identity this
    whole file already establishes rather than a separate WatchedEntry-based
    definition just for this one list."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session, rating=Decimal('5.0')))
    films = [
        {'title': r.title, 'year': r.year, 'movie_id': r.movie_id}
        for r in rated
        if (r.title, r.year) not in other_watched_keys
    ]
    films.sort(key=lambda f: f['title'])
    return films[:TOP_N]


def _rating_curve(import_session) -> dict:
    """Per-session rating distribution, zero-filled across every RATING_BUCKETS value.
    Not the same shape as dashboard.py's rating_distribution (which only returns
    buckets that actually have data) -- the two-session grouped bar chart this feeds
    needs both sessions plotted against one identical, gap-free x-axis, so it's
    reimplemented locally rather than imported, the same way this file already
    reimplements its own MIN_COUNT_FOR_AVERAGE instead of importing dashboard.py's."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    counts_by_rating = {row['rating']: row['count'] for row in rated.values('rating').annotate(count=Count('id'))}
    counts = [counts_by_rating.get(bucket, 0) for bucket in RATING_BUCKETS]

    rated_count = rated.count()
    avg = float(rated.aggregate(avg=Avg('rating'))['avg']) if rated_count >= MIN_COUNT_FOR_AVERAGE else None

    return {'counts': counts, 'avg': avg, 'count': rated_count}


def _resolve_posters(movies_by_id, *film_lists):
    """Mutates each film dict in place to add poster_url, resolved from a single bulk
    Movie lookup (movies_by_id) rather than one query per list. sorted()/slicing only
    reorder references, never copy the dicts themselves, so running this once over the
    underlying objects flows through automatically to every derived/sliced list built
    from the same dicts (e.g. same_rating and biggest_disagreements both draw from
    shared_films)."""
    for films in film_lists:
        for film in films:
            movie = movies_by_id.get(film.get('movie_id'))
            film['poster_url'] = movie.poster_url if movie else ''


def _shared_people(session_a, session_b, values_field, filter_field):
    """Directors or actors both sessions have rated at least MIN_COUNT_FOR_AVERAGE
    films from -- values_field is the group-by field ('movie__directors__name' or
    'movie__cast_members__name'), filter_field is its M2M relation ('movie__directors'
    / 'movie__cast_members') used to exclude unenriched films from the grouping.

    Ranked by whichever of the two averages is *lower*, not the combined/mean average
    -- a shared favorite has to be genuinely well-regarded by both people, not one
    person loving them enough to drag a blended average up while the other is
    lukewarm. No cameo-filtering for the actor case (unlike dashboard.py's actor
    stats) -- this file has never touched Credit/billing-order data before, and
    adding that here would be a bigger lift than this feature warrants; a known,
    easy-to-revisit simplification, not an oversight."""
    def _averages(import_session):
        rows = (
            exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
            .filter(**{f'{filter_field}__isnull': False})
            .values(values_field)
            .annotate(avg=Avg('rating'), count=Count('id'))
        )
        return {
            row[values_field]: (float(row['avg']), row['count'])
            for row in rows
            if row['count'] >= MIN_COUNT_FOR_AVERAGE
        }

    stats_a = _averages(session_a)
    stats_b = _averages(session_b)

    results = [
        {'name': name, 'avg_a': stats_a[name][0], 'avg_b': stats_b[name][0],
         'count_a': stats_a[name][1], 'count_b': stats_b[name][1]}
        for name in set(stats_a) & set(stats_b)
    ]
    results.sort(key=lambda r: (min(r['avg_a'], r['avg_b']), r['count_a'] + r['count_b']), reverse=True)
    return results[:TOP_N]


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
            'movie_id': a['movie_id'] or b['movie_id'],
            'rating_a': rating_a,
            'rating_b': rating_b,
            'delta': delta,
        })
        if delta is not None:
            rated_deltas.append(delta)

    rated_shared = [f for f in shared_films if f['delta'] is not None]
    biggest_disagreements = sorted(rated_shared, key=lambda f: f['delta'], reverse=True)[:TOP_N]
    # delta is uniformly 0 for every candidate here, so it carries no ordering signal
    # -- sorted by rating value descending instead, surfacing "films you both loved"
    # ahead of "films you both hated"; title is the tiebreak when ratings also match.
    same_rating = sorted(
        (f for f in rated_shared if f['delta'] == Decimal('0.0')),
        key=lambda f: (-f['rating_a'], f['title']),
    )[:TOP_N]

    agree_count = sum(1 for d in rated_deltas if d <= AGREEMENT_THRESHOLD)
    union_size = len(keys_a | keys_b)

    only_a_films_all = sorted((map_a[k] for k in only_a_keys), key=lambda f: f['title'])
    only_b_films_all = sorted((map_b[k] for k in only_b_keys), key=lambda f: f['title'])

    watchlist_a = _watchlist_map(session_a)
    watchlist_b = _watchlist_map(session_b)
    shared_watchlist_keys = set(watchlist_a) & set(watchlist_b)
    watchlist_matches_all = sorted((watchlist_a[k] for k in shared_watchlist_keys), key=lambda f: f['title'])

    same_day_logs_all = _same_day_logs(session_a, session_b)

    five_star_only_a = _five_star_only(session_a, keys_b)
    five_star_only_b = _five_star_only(session_b, keys_a)

    curve_a = _rating_curve(session_a)
    curve_b = _rating_curve(session_b)

    shared_directors = _shared_people(session_a, session_b, 'movie__directors__name', 'movie__directors')
    shared_actors = _shared_people(session_a, session_b, 'movie__cast_members__name', 'movie__cast_members')

    # One bulk lookup spanning every film list on the page rather than a query per
    # list. Resolves onto shared_films (and therefore biggest_disagreements/
    # same_rating too, since they're built via sorted() on the same dict objects),
    # only_a/b_films, five_star_only_a/b, and watchlist_matches_all (and therefore its
    # capped watchlist_matches slice).
    movie_ids = {
        f['movie_id']
        for f in (
            shared_films + only_a_films_all + only_b_films_all
            + five_star_only_a + five_star_only_b + watchlist_matches_all
        )
        if f.get('movie_id')
    }
    movies_by_id = Movie.objects.in_bulk(movie_ids)
    _resolve_posters(
        movies_by_id, shared_films, only_a_films_all, only_b_films_all,
        five_star_only_a, five_star_only_b, watchlist_matches_all,
    )

    overlap_pct = round(len(shared_keys) / union_size * 100, 1) if union_size else 0
    agreement_pct = round(agree_count / len(rated_shared) * 100, 1) if rated_shared else None
    # A single headline number blending "how much do you watch the same things" with
    # "when you do, do you feel the same way" -- a plain average of the two, in
    # keeping with this file's own stated no-scipy, simple-hand-rolled-metric
    # philosophy (see the module docstring). None when agreement_pct is unavailable
    # (no shared rated films) rather than falling back to overlap alone, since a
    # "compatibility" score that ignores taste entirely isn't really answering the
    # question it claims to.
    compatibility_pct = round((overlap_pct + agreement_pct) / 2, 1) if agreement_pct is not None else None

    return {
        'session_a': session_a,
        'session_b': session_b,
        'shared_count': len(shared_keys),
        'only_a_count': len(only_a_keys),
        'only_b_count': len(only_b_keys),
        'overlap_pct': overlap_pct,
        'agreement_pct': agreement_pct,
        'compatibility_pct': compatibility_pct,
        'avg_delta': (
            round(float(sum(rated_deltas) / len(rated_deltas)), 1)
            if len(rated_deltas) >= MIN_COUNT_FOR_AVERAGE
            else None
        ),
        'biggest_disagreements': biggest_disagreements,
        'same_rating': same_rating,
        'only_a_films': only_a_films_all[:TOP_N],
        'only_b_films': only_b_films_all[:TOP_N],
        'genres_a': _top_genres(session_a),
        'genres_b': _top_genres(session_b),
        'shared_directors': shared_directors,
        'shared_actors': shared_actors,
        'watchlist_matches': watchlist_matches_all[:TOP_N],
        'watchlist_matches_total': len(watchlist_matches_all),
        'same_day_logs': same_day_logs_all[:TOP_N],
        'same_day_logs_total': len(same_day_logs_all),
        'five_star_only_a': five_star_only_a,
        'five_star_only_b': five_star_only_b,
        'avg_rating_a': curve_a['avg'],
        'avg_rating_b': curve_b['avg'],
        'chart_data': {
            'rating_curve': {
                'labels': [str(b) for b in RATING_BUCKETS],
                'data_a': curve_a['counts'],
                'data_b': curve_b['counts'],
                'label_a': session_a.display_name or 'Person A',
                'label_b': session_b.display_name or 'Person B',
            },
        },
    }
