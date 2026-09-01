"""Pure comparison logic between two ImportSessions. No scipy dependency -- the
'agreement' metric is a simple hand-rolled % of shared films rated within 0.5 stars
of each other, which is easy to explain and good enough for a learning project."""

from collections import defaultdict
from decimal import Decimal
from itertools import groupby

from django.db.models import Avg, Count, Min

from imports.models import DiaryEntry, RatingEntry, WatchlistEntry
from stats.services.filters import exclude_tv_shows
from tmdb.models import Credit, Movie

AGREEMENT_THRESHOLD = Decimal('0.5')
TOP_N = 10
# Same rating and Watchlist matches render as a fixed-width poster grid (see the
# site-wide .favs--eight in base.css), not a table -- capped at 2 full rows of 8 (16)
# rather than TOP_N's 10, since 10 left an awkward sparse second row of 2.
GRID_DISPLAY_CAP = 16
# Top unseen (formerly "five-star exclusives") renders the same kind of poster grid,
# but inside a .two-col half-width card rather than a full-width one -- 3 rows of 4
# (12) fits that narrower card the way GRID_DISPLAY_CAP's 2 rows of 8 fits a
# full-width one.
GRID_DISPLAY_CAP_NARROW = 12
# Qualifying bar for _top_unseen_by_other -- "X loved it, Y hasn't seen it" needs to
# stay a genuine "loved it" claim, not just whatever happens to be the highest-rated
# film left after excluding what the other person's seen.
TOP_UNSEEN_MIN_RATING = Decimal('4.0')
# Same rating's grid is weighted toward higher ratings rather than an even spread --
# up to GRID_HIGH_RATING_SLOTS of the GRID_DISPLAY_CAP slots go to 4.0+ tiers, the
# rest to whatever's left (typically low-to-mid tiers, since same_rating_all is
# already sorted highest-first). See _same_rating_display.
GRID_HIGH_RATING_SLOTS = 10
GRID_HIGH_RATING_THRESHOLD = Decimal('4.0')
# An average of a single shared rated film isn't meaningful -- avg_delta requires at
# least this many shared rated films, or it's left out entirely.
MIN_COUNT_FOR_AVERAGE = 2
# Values match dashboard.py's own MIN_COUNT_FOR_FAVORITE_DIRECTOR/_ACTOR exactly --
# same reasoning (a director/actor needs enough films watched to call them a real
# favorite rather than a one-off high rating; actors need a higher bar than directors
# since a single film credits many more actors than directors). Copied rather than
# imported, same as MIN_COUNT_FOR_AVERAGE above -- this file's own convention is to
# reimplement small shared constants locally instead of importing dashboard.py's, to
# keep the two services decoupled.
MIN_COUNT_FOR_FAVORITE_DIRECTOR = 3
MIN_COUNT_FOR_FAVORITE_ACTOR = 4
# Cameo-filtering constants, copied from dashboard.py for the same reason as the two
# thresholds above -- 'favorite actors' should mean the same thing on both pages. See
# _cameo_credit_ids for how they're used.
MIN_CAST_SIZE_FOR_CAMEO_FILTER = 30
CAMEO_RELATIVE_BILLING_THRESHOLD = 0.4
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
    """watched_date -> [{'title', 'year', 'movie_id'}, ...] for this session's
    diary.csv -- a date can have more than one entry (a marathon day, or a rewatch
    logged the same day as something else), so every value is a list, never a single
    film. Dicts rather than formatted strings so a poster can be resolved per film,
    same as every other film list on this page."""
    entries = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session)).order_by('title')
    by_date = defaultdict(list)
    for title, year, watched_date, movie_id in entries.values_list('title', 'year', 'watched_date', 'movie_id'):
        by_date[watched_date].append({'title': title, 'year': year, 'movie_id': movie_id})
    return by_date


def _same_day_logs(session_a, session_b) -> dict:
    """{'logs': [...], 'exact_matches': [...]}. logs = dates both sessions logged at
    least one film on -- not necessarily the *same* film (that's shared_films/
    same_rating elsewhere on this page); a same-day viewing pattern, not a
    same-taste one. exact_matches is the strict subset of that: every specific
    instance where the *same* (title, year) was logged by both people on the same
    date -- surfaced separately via the page's 'Exact matches' toggle rather than
    mixed into logs, since it's a different (stronger) claim than 'you both watched
    something that day'."""
    dates_a = _films_by_date(session_a)
    dates_b = _films_by_date(session_b)
    shared_dates = set(dates_a) & set(dates_b)

    logs = []
    exact_matches = []
    for date in shared_dates:
        films_a, films_b = dates_a[date], dates_b[date]
        logs.append({'date': date, 'films_a': films_a, 'films_b': films_b})

        keys_b = {(f['title'], f['year']) for f in films_b}
        exact_matches.extend(
            {'date': date, 'title': f['title'], 'year': f['year'], 'movie_id': f['movie_id']}
            for f in films_a if (f['title'], f['year']) in keys_b
        )

    logs.sort(key=lambda r: r['date'], reverse=True)
    exact_matches.sort(key=lambda r: r['date'], reverse=True)
    return {'logs': logs, 'exact_matches': exact_matches}


def _top_unseen_by_other(import_session, other_watched_keys):
    """This session's TOP_UNSEEN_MIN_RATING+ films, ranked highest rating first, that
    the other session has no record of watching at all -- not restricted to a
    perfect 5.0 (that returned nothing for anyone who rarely hands out perfect
    scores), but still a real "loved it" bar, not just "the best of whatever's left."
    other_watched_keys is the other session's _film_map key set (rated union diary),
    reusing the same 'watched' identity this whole file already establishes rather
    than a separate WatchedEntry-based definition just for this one list. Returns
    every match, uncapped -- the caller slices to GRID_DISPLAY_CAP_NARROW and tracks
    the true total, same cap-with-total pattern as watchlist_matches/only_a_films.

    Sorted by rating descending, (title, year) as the tiebreak for determinism --
    `rated` is a QuerySet with unspecified DB ordering otherwise, and two different
    films can also share a title (a remake)."""
    rated = exclude_tv_shows(
        RatingEntry.objects.filter(import_session=import_session, rating__gte=TOP_UNSEEN_MIN_RATING)
    )
    films = [
        {'title': r.title, 'year': r.year, 'movie_id': r.movie_id, 'rating': r.rating}
        for r in rated
        if (r.title, r.year) not in other_watched_keys
    ]
    films.sort(key=lambda f: (-f['rating'], f['title'], f['year']))
    return films


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


def _tmdb_image_url(path: str, size: str) -> str:
    """Builds a TMDB image URL from a raw path string pulled via .values()/Min()
    aggregation rather than a model instance -- Person.profile_url is a proper model
    property, but that only helps when a query returns real instances, not dict rows.
    Mirrors dashboard.py's own _tmdb_image_url exactly; reimplemented locally rather
    than imported, the same way this file already reimplements MIN_COUNT_FOR_AVERAGE
    and _rating_curve instead of importing dashboard.py's."""
    return f'https://image.tmdb.org/t/p/{size}{path}' if path else ''


def _director_averages(import_session, min_count):
    """{name: (avg, count, profile_url, tmdb_id)} for directors this session has
    rated at least min_count films from. min_count is MIN_COUNT_FOR_FAVORITE_DIRECTOR,
    not MIN_COUNT_FOR_AVERAGE -- 'favorite' has a higher bar than a merely-averageable
    sample size. tmdb_id is threaded through for the person-filmography modal (same
    dashboard.py feature, reused as-is here since it's keyed by session_id alone --
    session_a and session_b are each just an ImportSession id, so the existing
    endpoint needs no Double-Feature-specific changes)."""
    rows = (
        exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
        .filter(movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(
            avg=Avg('rating'), count=Count('id'), profile_path=Min('movie__directors__profile_path'),
            tmdb_id=Min('movie__directors__tmdb_id'),
        )
    )
    return {
        row['movie__directors__name']: (
            float(row['avg']), row['count'], _tmdb_image_url(row['profile_path'], 'w185'), row['tmdb_id'],
        )
        for row in rows
        if row['count'] >= min_count
    }


def _cameo_credit_ids(movie_ids) -> set:
    """Credit ids that count as a cameo under CAMEO_RELATIVE_BILLING_THRESHOLD /
    MIN_CAST_SIZE_FOR_CAMEO_FILTER. Mirrors dashboard.py's own _cameo_credit_ids
    exactly (same constants, same formula) -- reimplemented locally rather than
    imported, this file's established convention for anything dashboard.py also
    defines (see MIN_COUNT_FOR_AVERAGE, _rating_curve, _tmdb_image_url)."""
    cast_sizes = defaultdict(int)
    rows = list(Credit.objects.filter(movie_id__in=movie_ids).values_list('id', 'movie_id', 'order'))
    for _, movie_id, _ in rows:
        cast_sizes[movie_id] += 1

    return {
        credit_id
        for credit_id, movie_id, order in rows
        if cast_sizes[movie_id] >= MIN_CAST_SIZE_FOR_CAMEO_FILTER
        and order / cast_sizes[movie_id] >= CAMEO_RELATIVE_BILLING_THRESHOLD
    }


def _actor_averages(import_session, min_count):
    """{name: (avg, count, profile_url, tmdb_id)} for actors this session has rated
    at least min_count *non-cameo* films from -- same cameo exclusion as
    dashboard.py's favorite_actors/top_actors (a big-cast film's one-scene bit part
    shouldn't count as 'you rated a film with this actor'). Can't reuse
    _director_averages' plain M2M query shape here: excluding specific Credit rows by
    id needs an explicit query through Credit, not the cast_members M2M field, so this
    is its own function rather than a shared 'filter_field' parameter. tmdb_id is
    threaded through for the person-filmography modal, same reasoning as
    _director_averages above."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session)).exclude(movie__isnull=True)
    rated_ratings_by_movie = dict(rated.values_list('movie_id', 'rating'))
    cameo_ids = _cameo_credit_ids(rated_ratings_by_movie.keys())

    actor_ratings = defaultdict(list)
    actor_profile_paths = {}
    actor_tmdb_ids = {}
    for person_name, movie_id, profile_path, tmdb_id in (
        Credit.objects.filter(movie_id__in=rated_ratings_by_movie)
        .exclude(id__in=cameo_ids)
        .values_list('person__name', 'movie_id', 'person__profile_path', 'person__tmdb_id')
    ):
        actor_ratings[person_name].append(rated_ratings_by_movie[movie_id])
        actor_profile_paths[person_name] = profile_path
        actor_tmdb_ids[person_name] = tmdb_id

    return {
        name: (
            float(sum(ratings) / len(ratings)), len(ratings),
            _tmdb_image_url(actor_profile_paths[name], 'w185'), actor_tmdb_ids[name],
        )
        for name, ratings in actor_ratings.items()
        if len(ratings) >= min_count
    }


def _top_people(stats):
    """This session's own top TOP_N directors/actors by avg rating, from an
    already-built {name: (avg, count, profile_url)} map (_director_averages or
    _actor_averages) -- independent of the other session, unlike _shared_people.
    Sorted by avg descending, count as the tiebreak (same ordering shape as
    _shared_people, just single-session).

    The tie check sorts on round(avg, 1), the *displayed* rating, not the raw one --
    two people can both show "4.6 ★" while their true averages are 4.625 vs 4.55, and
    sorting on the untruncated value would separate them by a difference the user
    can't even see, silently skipping the film-count tiebreak they're expecting.
    Mirrors dashboard.py's own favorite_directors/_actors sort exactly."""
    results = [
        {'name': name, 'avg': avg, 'count': count, 'profile_url': profile_url, 'tmdb_id': tmdb_id}
        for name, (avg, count, profile_url, tmdb_id) in stats.items()
    ]
    results.sort(key=lambda r: (round(r['avg'], 1), r['count']), reverse=True)
    return results[:TOP_N]


def _shared_people(stats_a, stats_b):
    """Directors or actors both sessions qualify as a favorite for, from two
    already-built averages maps (same source functions as _top_people). Ranked by
    whichever of the two averages is *lower*, not the combined/mean average -- a
    shared favorite has to be genuinely well-regarded by both people, not one person
    loving them enough to drag a blended average up while the other is lukewarm.
    Ties on that (displayed, 1dp -- see _top_people) lower-average value fall back to
    combined film count."""
    results = [
        {
            'name': name, 'avg_a': stats_a[name][0], 'avg_b': stats_b[name][0],
            'count_a': stats_a[name][1], 'count_b': stats_b[name][1],
            # Either session's copy works equally well here -- both were rated by the
            # same real person, so their profile photo can't differ between sessions.
            'profile_url': stats_a[name][2] or stats_b[name][2],
        }
        for name in set(stats_a) & set(stats_b)
    ]
    # Negating rather than reverse=True keeps `name` ascending as the final tiebreak
    # (reverse=True would flip it to descending too). name is a real tiebreak, not
    # decoration -- results is built by iterating a set intersection of person names,
    # whose order is affected by Python's per-process string hash randomization, so
    # ties on the numeric keys alone would visibly reorder across server restarts --
    # the exact same bug class fixed in biggest_disagreements_all above.
    results.sort(key=lambda r: (-round(min(r['avg_a'], r['avg_b']), 1), -(r['count_a'] + r['count_b']), r['name']))
    return results[:TOP_N]


def _spread_by_rating(films_sorted_desc, cap):
    """Selects up to `cap` films from `films_sorted_desc` (already sorted rating
    descending, same key as same_rating_all's own sort) spread across rating tiers,
    instead of just the literal top `cap` -- with 155 total exact ties, a plain slice
    is almost entirely 5.0/4.5-star films, which reads as 'films you both loved' when
    the section is really 'films you rated the same,' good or bad. Groups by rating
    value, then round-robins one film per tier per pass (highest tier first) until
    `cap` is reached or every tier is exhausted -- a tier with fewer films than others
    simply drops out of later passes rather than being padded. The selected subset is
    re-sorted by the same (rating desc, title) key before returning, since round-robin
    selection order interleaves tiers and doesn't itself read top-to-bottom."""
    tiers = [list(group) for _, group in groupby(films_sorted_desc, key=lambda f: f['rating_a'])]
    selected = []
    round_index = 0
    while len(selected) < cap and any(round_index < len(tier) for tier in tiers):
        for tier in tiers:
            if len(selected) >= cap:
                break
            if round_index < len(tier):
                selected.append(tier[round_index])
        round_index += 1
    selected.sort(key=lambda f: (-f['rating_a'], f['title']))
    return selected


def _same_rating_display(same_rating_all, cap, high_slots_target):
    """Splits same_rating_all at GRID_HIGH_RATING_THRESHOLD, spreads each half
    separately via _spread_by_rating, and concatenates -- high half first, since
    every high-tier film outranks every low-tier one by definition of the split, so
    no further merge/sort is needed. high_slots_target of the cap is reserved for
    4.0+ films (still spread across 4.0/4.5/5.0 rather than just the top few), the
    rest for whatever's left, so the grid reads as 'mostly films you both loved, with
    a handful you both didn't' rather than an even mix across the whole scale.

    Slots one half can't fill are handed to the other, capped at what's actually
    available on each side -- e.g. a pair with no exact ties below 4 stars still gets
    a full 16-film grid rather than 10 films and 6 empty slots."""
    high = [f for f in same_rating_all if f['rating_a'] >= GRID_HIGH_RATING_THRESHOLD]
    low = [f for f in same_rating_all if f['rating_a'] < GRID_HIGH_RATING_THRESHOLD]

    high_slots = min(high_slots_target, len(high))
    low_slots = min(cap - high_slots, len(low))
    high_slots = min(cap - low_slots, len(high))  # reclaim slots low couldn't use

    return _spread_by_rating(high, high_slots) + _spread_by_rating(low, low_slots)


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
    # Title is a real tiebreak, not decoration -- rated_shared is built by iterating
    # shared_keys (a set), so its order is affected by Python's per-process hash
    # randomization. Sorting on delta alone left every tie among equally-mismatched
    # films in that arbitrary order, which visibly changed which films appeared
    # across server restarts once GRID_DISPLAY_CAP (16) started reaching deep into
    # the tied-at-max-delta group.
    biggest_disagreements_all = sorted(rated_shared, key=lambda f: (-f['delta'], f['title']))
    # delta is uniformly 0 for every candidate here, so it carries no ordering signal
    # -- sorted by rating value descending instead, surfacing "films you both loved"
    # ahead of "films you both hated"; title is the tiebreak when ratings also match.
    same_rating_all = sorted(
        (f for f in rated_shared if f['delta'] == Decimal('0.0')),
        key=lambda f: (-f['rating_a'], f['title']),
    )
    # Every film in rated_shared falls into exactly one of these three buckets
    # (same_rating_all, rated_higher_a, rated_higher_b) -- their counts always sum to
    # len(rated_shared).
    rated_higher_a_count = sum(1 for f in rated_shared if f['rating_a'] > f['rating_b'])
    rated_higher_b_count = sum(1 for f in rated_shared if f['rating_b'] > f['rating_a'])

    agree_count = sum(1 for d in rated_deltas if d <= AGREEMENT_THRESHOLD)
    union_size = len(keys_a | keys_b)

    # (title, year), not title alone -- only_a_keys/only_b_keys are set differences,
    # so their iteration order is affected by Python's per-process string hash
    # randomization; a title-only tiebreak would leave two same-titled films (a
    # remake) in an order that changes across server restarts, the same bug class
    # fixed in biggest_disagreements_all/_shared_people above.
    only_a_films_all = sorted((map_a[k] for k in only_a_keys), key=lambda f: (f['title'], f['year']))
    only_b_films_all = sorted((map_b[k] for k in only_b_keys), key=lambda f: (f['title'], f['year']))

    watchlist_a = _watchlist_map(session_a)
    watchlist_b = _watchlist_map(session_b)
    shared_watchlist_keys = set(watchlist_a) & set(watchlist_b)
    # Same (title, year) tiebreak reasoning as only_a_films_all/only_b_films_all above
    # -- shared_watchlist_keys is also a set intersection.
    watchlist_matches_all = sorted(
        (watchlist_a[k] for k in shared_watchlist_keys), key=lambda f: (f['title'], f['year'])
    )

    same_day = _same_day_logs(session_a, session_b)
    same_day_logs_all = same_day['logs']
    same_day_exact_matches_all = same_day['exact_matches']
    # Flattened views over the same nested film dicts inside same_day_logs_all --
    # _resolve_posters mutates dicts in place, so resolving through these flat lists
    # still resolves onto the nested per-date films_a/films_b lists too.
    same_day_films_a = [f for entry in same_day_logs_all for f in entry['films_a']]
    same_day_films_b = [f for entry in same_day_logs_all for f in entry['films_b']]

    top_unseen_a_all = _top_unseen_by_other(session_a, keys_b)
    top_unseen_b_all = _top_unseen_by_other(session_b, keys_a)

    curve_a = _rating_curve(session_a)
    curve_b = _rating_curve(session_b)

    # Each session's director/actor averages computed once and reused by both
    # _top_people (this session alone) and _shared_people (the intersection) --
    # avoids querying the same session's stats twice over.
    director_stats_a = _director_averages(session_a, MIN_COUNT_FOR_FAVORITE_DIRECTOR)
    director_stats_b = _director_averages(session_b, MIN_COUNT_FOR_FAVORITE_DIRECTOR)
    actor_stats_a = _actor_averages(session_a, MIN_COUNT_FOR_FAVORITE_ACTOR)
    actor_stats_b = _actor_averages(session_b, MIN_COUNT_FOR_FAVORITE_ACTOR)

    shared_directors = _shared_people(director_stats_a, director_stats_b)
    shared_actors = _shared_people(actor_stats_a, actor_stats_b)
    top_directors_a = _top_people(director_stats_a)
    top_directors_b = _top_people(director_stats_b)
    top_actors_a = _top_people(actor_stats_a)
    top_actors_b = _top_people(actor_stats_b)

    # One bulk lookup spanning every film list on the page rather than a query per
    # list. Resolves onto shared_films (and therefore biggest_disagreements/
    # same_rating too, since they're built via sorted() on the same dict objects),
    # only_a/b_films, top_unseen_a/b, watchlist_matches_all (and therefore its
    # capped watchlist_matches slice), the same-day films (and therefore
    # same_day_logs_all's nested films_a/films_b), and same_day_exact_matches_all.
    movie_ids = {
        f['movie_id']
        for f in (
            shared_films + only_a_films_all + only_b_films_all
            + top_unseen_a_all + top_unseen_b_all + watchlist_matches_all
            + same_day_films_a + same_day_films_b + same_day_exact_matches_all
        )
        if f.get('movie_id')
    }
    movies_by_id = Movie.objects.in_bulk(movie_ids)
    _resolve_posters(
        movies_by_id, shared_films, only_a_films_all, only_b_films_all,
        top_unseen_a_all, top_unseen_b_all, watchlist_matches_all,
        same_day_films_a, same_day_films_b, same_day_exact_matches_all,
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
        'rated_higher_a_count': rated_higher_a_count,
        'rated_higher_b_count': rated_higher_b_count,
        'overlap_pct': overlap_pct,
        'agreement_pct': agreement_pct,
        'compatibility_pct': compatibility_pct,
        'avg_delta': (
            round(float(sum(rated_deltas) / len(rated_deltas)), 1)
            if len(rated_deltas) >= MIN_COUNT_FOR_AVERAGE
            else None
        ),
        'biggest_disagreements': biggest_disagreements_all[:GRID_DISPLAY_CAP],
        'biggest_disagreements_total': len(biggest_disagreements_all),
        'same_rating': _same_rating_display(same_rating_all, GRID_DISPLAY_CAP, GRID_HIGH_RATING_SLOTS),
        'same_rating_total': len(same_rating_all),
        'only_a_films': only_a_films_all[:TOP_N],
        'only_b_films': only_b_films_all[:TOP_N],
        'genres_a': _top_genres(session_a),
        'genres_b': _top_genres(session_b),
        'shared_directors': shared_directors,
        'shared_actors': shared_actors,
        'top_directors_a': top_directors_a,
        'top_directors_b': top_directors_b,
        'top_actors_a': top_actors_a,
        'top_actors_b': top_actors_b,
        'watchlist_matches': watchlist_matches_all[:GRID_DISPLAY_CAP],
        'watchlist_matches_total': len(watchlist_matches_all),
        # TOP_N, not GRID_DISPLAY_CAP -- same_day_logs renders as a flex-wrap flow of
        # variable-width day cards (each sized to how many films that date has), not
        # a fixed-column poster grid, so the "2 rows of 8" reasoning behind
        # GRID_DISPLAY_CAP doesn't apply here.
        'same_day_logs': same_day_logs_all[:TOP_N],
        'same_day_logs_total': len(same_day_logs_all),
        # GRID_DISPLAY_CAP here, unlike same_day_logs just above -- Exact matches
        # renders as the same fixed .favs--eight poster grid as Same rating/
        # Watchlist matches/Most different ratings, so it gets their cap, not
        # same_day_logs' TOP_N.
        'same_day_exact_matches': same_day_exact_matches_all[:GRID_DISPLAY_CAP],
        'same_day_exact_matches_total': len(same_day_exact_matches_all),
        'top_unseen_a': top_unseen_a_all[:GRID_DISPLAY_CAP_NARROW],
        'top_unseen_a_total': len(top_unseen_a_all),
        'top_unseen_b': top_unseen_b_all[:GRID_DISPLAY_CAP_NARROW],
        'top_unseen_b_total': len(top_unseen_b_all),
        'avg_rating_a': curve_a['avg'],
        'avg_rating_b': curve_b['avg'],
        'chart_data': {
            'rating_curve': {
                'labels': [str(b) for b in RATING_BUCKETS],
                'data_a': curve_a['counts'],
                'data_b': curve_b['counts'],
                # Rated totals per session, sent alongside the raw counts so the
                # template's "Percent" toggle can normalize count/total client-side
                # without a second request -- this is what makes two people with very
                # different totals-watched comparable by rating *tendency* rather than
                # raw volume.
                'count_a': curve_a['count'],
                'count_b': curve_b['count'],
                'label_a': session_a.display_name or 'Person A',
                'label_b': session_b.display_name or 'Person B',
            },
        },
    }
