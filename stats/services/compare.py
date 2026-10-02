"""Pure comparison logic between two ImportSessions. "No scipy" was an earlier
rule here; it's since been dropped in favor of reaching for a real stats
technique wherever it's actually the better tool (see _preference_deltas' own
empirical Bayes shrinkage) -- that particular one only needed a population
variance, which the standard library's own statistics.pvariance already
covers, so no new dependency followed from it. Most of the rest of this file
still doesn't need one: the 'agreement' metric, for instance, is a simple
hand-rolled % of shared films rated within 0.5 stars of each other, which is
easy to explain and good enough for a learning project as is."""

import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from itertools import groupby
from statistics import pvariance

from django.db.models import Avg, Count, Min
from django.db.models.functions import ExtractYear

from imports.models import DiaryEntry, RatingEntry, WatchedEntry, WatchlistEntry
from stats.services.filters import SHORT_FILM_MAX_RUNTIME_MINUTES, exclude_short_entries, exclude_tv_shows
from tmdb.models import Credit, Movie

AGREEMENT_THRESHOLD = Decimal('0.5')
# The largest possible per-film rating gap on Letterboxd's 0.5-5.0 scale (one of you
# rated it the minimum, the other the maximum) -- used to normalize avg_delta into a
# 0-100 "closeness" percentage for compatibility_pct (see build_compare_context),
# the same way overlap_pct/agreement_pct already are.
MAX_RATING_DELTA = Decimal('4.5')
TOP_N = 10
# Radius of the Overall alignment gauge's ring in compare.html (r="60" on a 150x150
# SVG, cx/cy 75) -- the circumference here is that ring's stroke-dasharray, kept as
# one computed value rather than a second hardcoded 377-ish constant in the
# template, so the two can't quietly drift apart if the ring's radius ever changes.
ALIGNMENT_GAUGE_RADIUS = 60
ALIGNMENT_GAUGE_CIRCUMFERENCE = round(2 * math.pi * ALIGNMENT_GAUGE_RADIUS, 2)
# Same rating, Most different ratings, and Watchlist matches render as a
# fixed-width poster grid (see the site-wide .favs--six in base.css), not a
# table -- capped at 2 full rows of 6 (12) rather than TOP_N's 10, since 10
# left an awkward sparse second row of 4.
GRID_DISPLAY_CAP = 12
# Same rating, Most different ratings and Watchlist matches each ship up to this
# many -- double the default grid -- and a "View more" button reveals the extra
# rows (they start hidden), so expanding needs no request.
GRID_EXPANDED_CAP = GRID_DISPLAY_CAP * 2
# Top unseen (formerly "five-star exclusives") renders the same kind of poster grid,
# but inside a .two-col half-width card rather than a full-width one -- 3 rows of 4
# (12) fits that narrower card the way GRID_DISPLAY_CAP's 2 rows of 6 fits a
# full-width one.
GRID_DISPLAY_CAP_NARROW = 12
# Favorite directors' "Shared" grid -- its own cap, not GRID_DISPLAY_CAP or
# GRID_DISPLAY_CAP_NARROW, even though all three now share both the same
# .favs--six shape and the same value: this is a headshot grid (people), not
# a poster grid (films), a different kind of card that could change its own
# cap independently without implying anything about the poster grids' caps
# (or vice versa).
SHARED_PEOPLE_GRID_CAP = 12
# Cap for the 'same_day_logs' context list -- no longer rendered directly (the
# template shows the heatmap built from the uncapped same_day_logs_all instead),
# but kept capped and covered by its own tests rather than removed outright, since
# same_day_logs_all's computation is still load-bearing for that heatmap.
SAME_DAY_LOGS_GRID_CAP = 12
# Qualifying bar for _top_unseen_by_other -- "X loved it, Y hasn't seen it" needs to
# stay a genuine "loved it" claim, not just whatever happens to be the highest-rated
# film left after excluding what the other person's seen.
TOP_UNSEEN_MIN_RATING = Decimal('4.0')
# Same rating's grid is weighted toward higher ratings rather than an even spread --
# up to GRID_HIGH_RATING_SLOTS of the GRID_DISPLAY_CAP slots go to 4.0+ tiers, the
# rest to whatever's left. See _same_rating_display.
GRID_HIGH_RATING_SLOTS = 8
# Same rating's high-rating share of GRID_EXPANDED_CAP, scaled with the cap.
GRID_HIGH_RATING_SLOTS_EXPANDED = GRID_HIGH_RATING_SLOTS * 2
GRID_HIGH_RATING_THRESHOLD = Decimal('4.0')
# An average of a single shared rated film isn't meaningful -- avg_delta requires at
# least this many shared rated films, or it's left out entirely.
MIN_COUNT_FOR_AVERAGE = 2
# Values match dashboard.py's own MIN_COUNT_FOR_FAVORITE_DIRECTOR/_ACTOR exactly
# (same reasoning there) -- copied rather than imported, per this file's convention
# of reimplementing small shared constants locally to keep the two services decoupled.
MIN_COUNT_FOR_FAVORITE_DIRECTOR = 3
MIN_COUNT_FOR_FAVORITE_ACTOR = 4
# Cameo-filtering constants, copied from dashboard.py for the same reason as the two
# thresholds above -- 'favorite actors' should mean the same thing on both pages. See
# _cameo_credit_ids for how they're used.
MIN_CAST_SIZE_FOR_CAMEO_FILTER = 30
CAMEO_RELATIVE_BILLING_THRESHOLD = 0.4
# Deliberately its own constant, not reused from Director's Cut and not applied to
# _actor_averages above -- see _lead_cast_credit_ids' own docstring for the full
# reasoning (a taste-preference signal wants a narrower, flat "lead-ish billing
# only" cutoff than the wider "exclude just the clearly-minor cameos" bar
# _cameo_credit_ids uses for "has this session watched films with this actor at
# all" purposes).
ACTOR_TOP_BILLING_FRACTION = 0.15
# The fixed 0.5-5.0 rating scale, hardcoded as exact strings rather than derived via
# Decimal division -- division's "ideal exponent" rules can silently produce
# Decimal('2') instead of Decimal('2.0') for a clean half-integer result, which
# compares/hashes equal (fine for dict lookups) but formats inconsistently via str()
# (not fine for the chart's x-axis labels, which need uniform '0.5'/'1.0'/... text).
RATING_BUCKETS = [Decimal(v) for v in ('0.5', '1.0', '1.5', '2.0', '2.5', '3.0', '3.5', '4.0', '4.5', '5.0')]
# Short weekday labels for the Watching Habits' weekday-distribution chart, indexed
# 1-7 to match Django's ExtractWeekDay/dashboard.py's own WEEKDAY_NAMES convention
# (1=Sunday ... 7=Saturday) -- reimplemented locally per this file's own convention
# (see _tmdb_image_url's own comment). Short forms rather than full names (dashboard.py's
# own WEEKDAY_NAMES), since this is a chart axis label, not table-row prose.
WEEKDAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']


def _film_map(import_session, exclude_shorts=False):
    """One row per (title, year) for this session, from exactly two sources:
    ratings.csv (RatingEntry, the authoritative rating) and watched.csv
    (WatchedEntry, the authoritative superset of "did this person watch this
    film at all", same definition dashboard.py's own _films_watched_total
    uses) -- ratings.csv is itself a subset of watched.csv (every rated film
    was watched), so this is really just watched.csv's identity set with
    ratings layered on where they exist.

    diary.csv is deliberately NOT a source here, for every stat this page
    builds from this map (shared/only-A/only-B counts, rating agreement, the
    per-film rating gap, "X hasn't seen it" claims): it's a log of *when* you
    watched something, a separate concern from *whether* you watched it or
    *what* you rated it, and a film logged in diary.csv without also
    appearing in watched.csv or ratings.csv (an incomplete export, or a
    diary-only rating that never made it into ratings.csv) shouldn't count as
    "watched" or "rated" here just because it happened to be logged with a
    date. See _same_day_logs/_films_by_date and _rating_curve elsewhere in
    this file for the date-based features diary.csv legitimately does drive.

    Keyed by (title, year), not letterboxd_uri: Letterboxd's "Letterboxd URI" column is
    a per-log-entry short link, not a stable per-film id -- the same film gets a
    *different* boxd.it code in ratings.csv than in watched.csv. (title, year) is the
    same key TMDB matching already uses (see tmdb/services/enrichment.py), so this
    keeps film identity consistent across the whole app.

    exclude_shorts drops any row whose resolved movie has a confirmed runtime under
    SHORT_FILM_MAX_RUNTIME_MINUTES -- the include/exclude shorts toggle on this
    page. Unresolved/unknown runtimes are kept either way (see
    exclude_short_entries)."""
    films = {}

    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    watched = exclude_tv_shows(WatchedEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        rated = exclude_short_entries(rated)
        watched = exclude_short_entries(watched)

    for r in rated.select_related('movie'):
        films[(r.title, r.year)] = {'title': r.title, 'year': r.year, 'rating': r.rating, 'movie_id': r.movie_id}

    # No rating to contribute -- just fills in the movie_id when the film is
    # genuinely new to the map, and otherwise only shows up here at all (an
    # unrated "yes I've seen this"), same fallback role watched.csv plays in
    # _films_watched_total.
    for w in watched.select_related('movie'):
        key = (w.title, w.year)
        entry = films.setdefault(key, {'title': w.title, 'year': w.year, 'rating': None, 'movie_id': None})
        if w.movie_id and not entry.get('movie_id'):
            entry['movie_id'] = w.movie_id

    return films


def _watchlist_map(import_session, exclude_shorts=False):
    """(title, year) -> {title, year, movie_id} for this session's watchlist.csv.
    Its own identity space, not merged with _film_map's rated/diary data (a film on
    the watchlist hasn't been watched) -- intersected independently between the two
    sessions in build_compare_context. exclude_shorts -- see _film_map's own
    comment."""
    films = {}
    watchlist = exclude_tv_shows(WatchlistEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        watchlist = exclude_short_entries(watchlist)
    for w in watchlist:
        films[(w.title, w.year)] = {'title': w.title, 'year': w.year, 'movie_id': w.movie_id}
    return films


def _films_by_date(import_session, exclude_shorts=False) -> dict:
    """watched_date -> [{'title', 'year', 'movie_id'}, ...] for this session's
    diary.csv -- a date can have more than one entry (a marathon day, or a rewatch
    logged the same day as something else), so every value is a list, never a single
    film. Dicts rather than formatted strings so a poster can be resolved per film,
    same as every other film list on this page. exclude_shorts -- see _film_map's
    own comment."""
    entries = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session)).order_by('title')
    if exclude_shorts:
        entries = exclude_short_entries(entries)
    by_date = defaultdict(list)
    for title, year, watched_date, movie_id in entries.values_list('title', 'year', 'watched_date', 'movie_id'):
        by_date[watched_date].append({'title': title, 'year': year, 'movie_id': movie_id})
    return by_date


def _same_day_logs(session_a, session_b, exclude_shorts=False) -> dict:
    """{'logs': [...], 'exact_matches': [...]}. logs = dates both sessions logged at
    least one film on -- not necessarily the *same* film (that's shared_films/
    same_rating elsewhere on this page); a same-day viewing pattern, not a
    same-taste one. exact_matches is the strict subset of that: every specific
    instance where the *same* (title, year) was logged by both people on the same
    date -- surfaced separately via the page's 'Exact matches' toggle rather than
    mixed into logs, since it's a different (stronger) claim than 'you both watched
    something that day'. exclude_shorts -- see _film_map's own comment."""
    dates_a = _films_by_date(session_a, exclude_shorts)
    dates_b = _films_by_date(session_b, exclude_shorts)
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


def _same_day_heatmap(logs, exact_matches) -> dict:
    """Per-day shared-log state bucketed by year, for the calendar-heatmap view of
    Same day logs -- the two-session counterpart to Director's Cut's own
    _viewing_heatmap (that one buckets a single session's watch counts; this
    buckets whether two sessions' calendars overlapped instead). Categorical, not
    volume-based -- state is 2 on a date with at least one exact match (see
    _same_day_logs' own docstring for what makes a match "exact"), 1 on a date
    that's shared but not exact; there's no "how many films" gradient here the way
    _viewing_heatmap has, so the grid's coloring is a lookup, not a bucketed ratio.
    films_a/films_b are row['films_a']/row['films_b'] themselves (title/year/
    movie_id dicts), not copies or pre-joined display strings -- the same objects
    same_day_films_a/_b flatten elsewhere in build_compare_context, so running
    _resolve_posters over those flat lists also mutates poster_url onto these same
    dicts, and the cell's click-through popup gets real posters for free. Takes
    logs/exact_matches directly rather than calling _same_day_logs itself, so the
    caller (build_compare_context) can reuse the same uncapped lists it already
    computed for the card's other views instead of querying twice."""
    exact_dates = {row['date'] for row in exact_matches}
    by_year = defaultdict(dict)
    for row in logs:
        date = row['date']
        by_year[date.year][date.isoformat()] = {
            'state': 2 if date in exact_dates else 1,
            'films_a': row['films_a'],
            'films_b': row['films_b'],
        }

    # Oldest -> newest, matching _viewing_heatmap's own ordering exactly -- see that
    # function's comment for why (a left-to-right year toggle, default_year taken
    # from the end of the list).
    years = sorted(by_year.keys())
    return {
        'years': years,
        'default_year': years[-1] if years else None,
        'data': {str(year): cells for year, cells in by_year.items()},
    }


def _resolve_same_day_ratings(map_a, map_b, films_a, films_b):
    """Mutates each film dict in films_a/films_b (the flattened same_day_films_a/_b
    -- the same objects _same_day_heatmap's nested films_a/films_b already
    reference, not copies) to add 'rating', that session's own rating for the film
    if they rated it. Same "single pass over the already-built objects" shape as
    _resolve_posters, keyed against map_a/map_b (build_compare_context's own
    _film_map results) rather than a fresh query -- feeds the day-detail popup's
    star rating; a film logged but never rated shows no rating there, same as
    everywhere else on the site treats an unrated film."""
    for f in films_a:
        entry = map_a.get((f['title'], f['year']))
        f['rating'] = entry['rating'] if entry else None
    for f in films_b:
        entry = map_b.get((f['title'], f['year']))
        f['rating'] = entry['rating'] if entry else None


def _top_unseen_by_other(import_session, other_watched_keys, exclude_shorts=False):
    """This session's TOP_UNSEEN_MIN_RATING+ films, ranked highest rating first, that
    the other session has no record of watching at all -- not restricted to a
    perfect 5.0 (that returned nothing for anyone who rarely hands out perfect
    scores), but still a real "loved it" bar, not just "the best of whatever's left."
    other_watched_keys is the other session's _film_map key set (rated union watched),
    reusing the same 'watched' identity this whole file already establishes rather
    than a separate WatchedEntry-based definition just for this one list. Returns
    every match, uncapped -- the caller slices to GRID_DISPLAY_CAP_NARROW and tracks
    the true total, same cap-with-total pattern as watchlist_matches.
    exclude_shorts -- see _film_map's own comment.

    Sorted by rating descending, (title, year) as the tiebreak for determinism --
    `rated` is a QuerySet with unspecified DB ordering otherwise, and two different
    films can also share a title (a remake)."""
    rated = exclude_tv_shows(
        RatingEntry.objects.filter(import_session=import_session, rating__gte=TOP_UNSEEN_MIN_RATING)
    )
    if exclude_shorts:
        rated = exclude_short_entries(rated)
    films = [
        {'title': r.title, 'year': r.year, 'movie_id': r.movie_id, 'rating': r.rating}
        for r in rated
        if (r.title, r.year) not in other_watched_keys
    ]
    films.sort(key=lambda f: (-f['rating'], f['title'], f['year']))
    return films


def _rating_curve(import_session, exclude_shorts=False) -> dict:
    """Per-session rating distribution, zero-filled across every RATING_BUCKETS value.
    Not the same shape as dashboard.py's rating_distribution (which only returns
    buckets that actually have data) -- the two-session grouped bar chart this feeds
    needs both sessions plotted against one identical, gap-free x-axis, so it's
    reimplemented locally rather than imported, the same way this file already
    reimplements its own MIN_COUNT_FOR_AVERAGE instead of importing dashboard.py's.
    exclude_shorts -- see _film_map's own comment."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        rated = exclude_short_entries(rated)
    counts_by_rating = {row['rating']: row['count'] for row in rated.values('rating').annotate(count=Count('id'))}
    counts = [counts_by_rating.get(bucket, 0) for bucket in RATING_BUCKETS]

    rated_count = rated.count()
    avg = float(rated.aggregate(avg=Avg('rating'))['avg']) if rated_count >= MIN_COUNT_FOR_AVERAGE else None

    return {'counts': counts, 'avg': avg, 'count': rated_count}


def _films_per_year(import_session, exclude_shorts=False) -> dict:
    """year -> diary.csv log count for this session. Returns the raw per-year
    counts, not zero-filled -- unlike _rating_curve's fixed RATING_BUCKETS scale,
    the years worth showing depend on both sessions' own diary history, so
    build_compare_context unions the two sessions' own year sets and zero-fills
    against that shared range itself, the same split Director's Cut's own
    films_per_year leaves to its template (a continuous range there too, just
    single-session so nothing needs unioning first). exclude_shorts -- see
    _film_map's own comment."""
    diary = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        diary = exclude_short_entries(diary)
    rows = diary.annotate(y=ExtractYear('watched_date')).values('y').annotate(count=Count('id'))
    return {row['y']: row['count'] for row in rows}


def _most_watched_films(import_session, cap, exclude_shorts=False) -> list:
    """This session's own most-rewatched films (diary.csv, watch_count > 1) --
    same definition as Director's Cut's own most_rewatched_films, reimplemented
    locally per this file's own convention. Independent per session, not
    intersected -- unlike Favorite directors/actors' own Side by side/Shared
    split, "films you've each rewatched" doesn't have an obviously meaningful
    shared reading (rewatching the exact same film the same number of times
    each is a coincidence, not a taste signal), so there's no Shared view here.
    Sorted by watch_count descending, (title, year) as the tiebreak for
    determinism -- diary is a QuerySet with unspecified DB ordering otherwise.
    exclude_shorts -- see _film_map's own comment."""
    diary = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        diary = exclude_short_entries(diary)
    films = list(
        diary.values('title', 'year')
        .annotate(watch_count=Count('id'), movie_id=Min('movie_id'))
        .filter(watch_count__gt=1)
    )
    films.sort(key=lambda f: (-f['watch_count'], f['title'], f['year']))
    return films[:cap]


def _streak_and_gap(dates: list) -> tuple:
    """Given a sorted list of distinct watch dates, return (longest consecutive-day
    streak, longest gap between watches) in days. Mirrors dashboard.py's own
    _streak_and_gap exactly, reimplemented locally per this file's own
    convention."""
    if not dates:
        return 0, 0

    longest_streak = current_streak = 1
    longest_gap = 0

    for prev, curr in zip(dates, dates[1:]):
        gap = (curr - prev).days
        if gap == 1:
            current_streak += 1
        else:
            longest_streak = max(longest_streak, current_streak)
            current_streak = 1
        longest_gap = max(longest_gap, gap - 1)

    longest_streak = max(longest_streak, current_streak)
    return longest_streak, longest_gap


def _viewing_habits(import_session, exclude_shorts=False) -> dict:
    """Busiest month (film count only, not which month -- the two sessions'
    busiest months rarely coincide, so naming both would need twice the label
    space for a fact that's mostly interesting as a number), longest streak,
    longest dry spell, and a weekday distribution (indexed 1-7 against
    WEEKDAY_LABELS, Django's ExtractWeekDay convention) -- the two-session
    comparison version of Director's Cut's own _viewing_calendar. Only the
    non-heatmap stats: a full two-person calendar heatmap would be a much
    heavier feature (two grids, or an awkward merged one) for what "Same day
    logs" above already covers from the angle that matters for a comparison
    page -- whether your calendars overlap, not each one's own shape.
    Reimplemented locally per this file's own convention. exclude_shorts --
    see _film_map's own comment."""
    diary = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        diary = exclude_short_entries(diary)
    watched_dates = list(diary.values_list('watched_date', flat=True))

    month_counts = defaultdict(int)
    weekday_counts = defaultdict(int)
    for watched_date in watched_dates:
        month_counts[watched_date.replace(day=1)] += 1
        # Match WEEKDAY_LABELS' indexing -- see dashboard.py's own identical
        # conversion comment for why (Python's date.weekday() uses a different
        # convention than Django's ExtractWeekDay).
        weekday_counts[((watched_date.weekday() + 1) % 7) + 1] += 1

    busiest_month_count = max(month_counts.values()) if month_counts else 0
    longest_streak, longest_gap = _streak_and_gap(sorted(set(watched_dates)))

    return {
        'busiest_month_count': busiest_month_count,
        'longest_streak_days': longest_streak,
        'longest_gap_days': longest_gap,
        'weekday_counts': [weekday_counts[w] for w in range(1, 8)],
        # Total logs backing weekday_counts -- feeds the weekday chart's own
        # Films/Percent toggle (same pattern as _rating_curve's count, see
        # chart_data.weekday_distribution below), so a person who logs far more
        # overall doesn't just visually dominate every day.
        'total_count': len(watched_dates),
    }


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


def _director_averages(import_session, min_count, exclude_shorts=False):
    """{name: (avg, count, profile_url, tmdb_id)} for directors this session has
    rated at least min_count films from. min_count is MIN_COUNT_FOR_FAVORITE_DIRECTOR,
    not MIN_COUNT_FOR_AVERAGE -- 'favorite' has a higher bar than a merely-averageable
    sample size. tmdb_id is threaded through for the person-filmography modal (same
    dashboard.py feature, reused as-is here since it's keyed by session_id alone --
    session_a and session_b are each just an ImportSession id, so the existing
    endpoint needs no Double-Feature-specific changes). exclude_shorts -- see
    _film_map's own comment."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        rated = exclude_short_entries(rated)
    rows = (
        rated
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
    defines (see MIN_COUNT_FOR_AVERAGE, _rating_curve, _tmdb_image_url). Used by
    _actor_averages (Favorite Actors/Top Actors/Shared Actors) only -- the
    preference/recommendation model's own actor axis uses the narrower
    _lead_cast_credit_ids below instead, a deliberate divergence (see that
    function's own docstring for why), not an oversight."""
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


def _lead_cast_credit_ids(movie_ids) -> set:
    """Credit ids billed within the top ACTOR_TOP_BILLING_FRACTION of their own
    movie's full cast list -- the only actor credits that count toward the
    preference/recommendation model's 'actor' axis. NOT used by
    _actor_averages (Favorite/Top/Shared Actors), which keeps the wider
    _cameo_credit_ids rule on purpose: that card answers "watched a film with
    this actor at all", where excluding only clear cameos is the right bar,
    while a taste signal is stronger the more it's restricted to actors who
    actually carried the film. A flat proportional cutoff, applied regardless
    of cast size (unlike _cameo_credit_ids' own minimum-cast-size gate) -- 20%
    of a 10-person cast is its top 2, 20% of a 200-person cast is its top 40."""
    cast_sizes = defaultdict(int)
    rows = list(Credit.objects.filter(movie_id__in=movie_ids).values_list('id', 'movie_id', 'order'))
    for _, movie_id, _ in rows:
        cast_sizes[movie_id] += 1

    return {
        credit_id
        for credit_id, movie_id, order in rows
        if order / cast_sizes[movie_id] < ACTOR_TOP_BILLING_FRACTION
    }


def _actor_averages(import_session, min_count, exclude_shorts=False):
    """{name: (avg, count, profile_url, tmdb_id)} for actors this session has rated
    at least min_count *non-cameo* films from -- same cameo exclusion as
    dashboard.py's favorite_actors/top_actors (a big-cast film's one-scene bit part
    shouldn't count as 'you rated a film with this actor'). Can't reuse
    _director_averages' plain M2M query shape here: excluding specific Credit rows by
    id needs an explicit query through Credit, not the cast_members M2M field, so this
    is its own function rather than a shared 'filter_field' parameter. tmdb_id is
    threaded through for the person-filmography modal, same reasoning as
    _director_averages above. exclude_shorts -- see _film_map's own comment."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session)).exclude(movie__isnull=True)
    if exclude_shorts:
        rated = exclude_short_entries(rated)
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


def _genre_averages(import_session, exclude_shorts=False) -> dict:
    """{genre_name: (avg, count)} for genres this session has rated at least
    MIN_COUNT_FOR_AVERAGE films in. Same shape as _director_averages/_actor_averages
    minus the profile_url/tmdb_id (a genre isn't a person, nothing to link to a
    filmography modal) -- feeds _genre_agreement below."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        rated = exclude_short_entries(rated)
    rows = (
        rated.filter(movie__genres__isnull=False)
        .values('movie__genres__name')
        .annotate(avg=Avg('rating'), count=Count('id'))
    )
    return {
        row['movie__genres__name']: (float(row['avg']), row['count'])
        for row in rows if row['count'] >= MIN_COUNT_FOR_AVERAGE
    }


def _genre_agreement(stats_a, stats_b) -> list:
    """Genres both sessions have a real average for (see _genre_averages), sorted
    by the absolute gap between their averages ascending -- genres you agree on
    lead the list, genres you clash over trail it. Genre name is the tiebreak for
    an exact-tie gap (e.g. two genres both at a 0.0 gap), same "why a real
    tiebreak matters" reasoning as this file's other set-derived sorts (see
    biggest_disagreements_all's own comment in build_compare_context) -- shared
    is a set intersection, so its iteration order is affected by Python's
    per-process string hash randomization."""
    shared = set(stats_a) & set(stats_b)
    rows = [
        {
            'genre': genre, 'avg_a': stats_a[genre][0], 'avg_b': stats_b[genre][0],
            'gap': abs(stats_a[genre][0] - stats_b[genre][0]),
        }
        for genre in shared
    ]
    rows.sort(key=lambda r: (r['gap'], r['genre']))
    return rows


# Watchlist matches' best-fit ranking: each person's baseline average plus a
# weighted SUM (not average, so favorite signals stack) of confidence-shrunk
# deltas per signal -- same per-signal model as Director's Cut's own
# _watchlist_recommendations, reimplemented locally per this file's
# reimplement-don't-import convention. 'keyword' earns 0.30 of the total, the
# other 7 axes scaled down proportionally; checked via leave-one-out holdout
# validation against real already-rated films before being added.
RECOMMENDATION_WEIGHTS = {
    'genre': 0.21,
    'director': 0.175,
    'actor': 0.105,
    'country': 0.07,
    'language': 0.035,
    'decade': 0.07,
    'runtime': 0.035,
    'keyword': 0.30,
}
# Only _generosity_score's shrinkage still uses a fixed K -- the 7 axis-delta
# maps below moved to empirical Bayes shrinkage, derived from each axis's own
# spread. Generosity is a single aggregate score, not a set of per-value
# estimates with a between-value variance to derive from, so there's no
# empirical Bayes formulation for it the way there is for a multi-value axis.
RECOMMENDATION_SHRINKAGE_K = 3
# How much a film's TMDB community rating (adjusted by each person's own
# generosity score) nudges their score on top of the taste-based signals --
# same value as Director's Cut's own TMDB_WEIGHT. Small and fixed: TMDB
# rating is an external quality prior, not a personal-taste axis, so it
# shouldn't grow the way a real taste signal can.
TMDB_WEIGHT = 0.05
# How much weight _adaptive_weights gives to each person's own variance-
# derived weights versus the fixed RECOMMENDATION_WEIGHTS above -- same value
# as Director's Cut's own ADAPTIVE_WEIGHT_BLEND.
ADAPTIVE_WEIGHT_BLEND = 0.5
# How much each category's confidence-shrunk *peak* rating (not its average)
# contributes to that category's delta -- same value as Director's Cut's own
# PEAK_BLEND. A category's average can be mediocre while it still contains a
# genuine outlier favorite; averaging alone erases that favorite.
PEAK_BLEND = 0.35
# Least-misery (min of the two people's scores) dominates the watchlist-match
# ranking, but blending in a small fraction of the average lets combined
# enthusiasm make a real, continuous difference between two similarly-fine
# films -- large enough to matter, small enough that one person's love still
# can't drown out the other's dislike. See _rank_watchlist_matches.
LEAST_MISERY_BLEND_WEIGHT = 0.15
# At most this many watchlist-match picks can credit the same director/actor
# -- same idea as Director's Cut's own PERSON_CREDIT_CAP. Without it, one
# favorite director's filmography could dominate the grid.
PERSON_CREDIT_CAP = 2
# A signal's delta has to clear this before its director/actor counts toward
# PERSON_CREDIT_CAP -- otherwise anyone who's ever shared even one rated film
# could exhaust a genuine favorite's cap slots.
CREDIT_THRESHOLD = 0.15
# Weight for _shared_trait_bonus's own contribution to the ranking score.
# Holdout validation shows raising this costs measured prediction accuracy,
# since the metric can only measure "would you have liked this film," never
# "did this feel like a genuinely mutual pick" -- the thing this bonus exists
# to reward. Kept small and non-zero as a deliberate trade.
SHARED_TRAIT_BONUS_WEIGHT = 0.1
# _shared_trait_bonus only applies its combo multiplier once a film qualifies
# on at least this many axes. Checked against real data first: most eligible
# candidates already clear 2 qualifying axes on their own, so rewarding 2
# wouldn't identify anything distinctive. 3+ is closer to "several genuinely
# separate signals corroborate this," not "shares a couple of common traits."
SHARED_TRAIT_COMBO_MIN_AXES = 3
# How much extra _shared_trait_bonus's total gets multiplied per qualifying
# axis at or beyond SHARED_TRAIT_COMBO_MIN_AXES -- several independent
# signals agreeing (shared director AND country AND genre) is stronger
# evidence than the same total magnitude in one axis alone, which plain
# addition can't distinguish.
SHARED_TRAIT_COMBO_BONUS_PER_AXIS = 0.5
# 'language' folds into 'country's slot when counting qualifying axes for the
# combo multiplier -- for a country with one dominant language (France/
# French, Japan/Japanese), both qualifying is usually the same fact told
# twice, not two independent confirmations. Doesn't affect each axis's own
# contribution to the base per-axis sum, only the combo-multiplier's tally.
COMBO_COUNT_AXIS_GROUP = {'language': 'country'}
# How much _rank_watchlist_matches' axis-agreement pass can boost the axis
# both people agree on most (see _axis_agreement/_boosted_weights) -- the
# strongest axis's weight is multiplied by 1 + this before renormalizing.
# Checked against real data: a modest, genuine effect, not a wholesale
# reshuffle.
AXIS_AGREEMENT_BOOST = 0.5


def _decade_bucket(year) -> str:
    return f'{(year // 10) * 10}s'


def _runtime_bucket(minutes) -> str:
    if minutes < 90:
        return 'Under 90 min'
    if minutes <= 150:
        return '90-150 min'
    return 'Over 150 min'


def _rarity_factor(count, total) -> float:
    """How much a signal value's rarity within this session's own rated history
    should scale its contribution to a match score -- close to 1.0 for a value only
    a handful of rated films share, fading toward 0 for a value shared by nearly
    every rated film. Same idea and formula as Director's Cut's own _rarity_factor
    in dashboard.py, reimplemented locally per this file's own convention.

    Applied as a plain multiply in _axis_deltas_from_records's own _shrink
    closure -- a confidence-gated version was tried and reverted after real-
    outcome validation favored this simpler one.

    (A cardinality-normalized version of this -- measuring each key's count against
    its own axis's average instead of this session's total rated-film count -- was
    tried and reverted; see dashboard.py's own _rarity_factor for why.)"""
    return math.log((total + 1) / (count + 1)) / math.log(total + 1)


def _variance(values) -> float:
    """Population variance of an iterable of numbers, or 0 for fewer than 2 values.
    Same as Director's Cut's own _variance in dashboard.py, reimplemented locally."""
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _adaptive_weights(axis_deltas: dict) -> dict:
    """Per-person axis weights, blended with the fixed RECOMMENDATION_WEIGHTS (see
    ADAPTIVE_WEIGHT_BLEND) rather than replacing them outright -- same idea and
    formula as Director's Cut's own _adaptive_weights in dashboard.py, reimplemented
    locally per this file's own convention. Derived from how much each axis's own
    deltas vary for this person: an axis whose deltas all cluster near 0 isn't
    telling us anything about their taste, while one that swings between strongly
    positive and negative clearly is. Both RECOMMENDATION_WEIGHTS and the variance-
    derived weights sum to 1.0 on their own, so blending them at a fixed ratio does
    too, with no separate renormalization step needed."""
    variances = {axis: _variance(deltas.values()) for axis, deltas in axis_deltas.items()}
    total_variance = sum(variances.values())
    if total_variance == 0:
        return dict(RECOMMENDATION_WEIGHTS)
    return {
        axis: (
            (1 - ADAPTIVE_WEIGHT_BLEND) * RECOMMENDATION_WEIGHTS[axis]
            + ADAPTIVE_WEIGHT_BLEND * (variances[axis] / total_variance)
        )
        for axis in RECOMMENDATION_WEIGHTS
    }


def _generosity_score(rated) -> tuple:
    """This session's average delta between their own rating and TMDB's community
    rating (normalized to a 5-point scale), for every TMDB-enriched rated film --
    positive means more generous than the crowd, negative harsher. Same computation
    as dashboard.py's own _taste_vs_crowd (just the generosity_score part -- Double
    Feature has no use for its overrates/underrates lists), reimplemented locally
    per this file's own convention. Returns (score, count); score is None if fewer
    than MIN_COUNT_FOR_AVERAGE rated films are TMDB-enriched. count is how many
    backed it either way, for confidence-shrinking downstream (see
    _preference_deltas)."""
    rows = rated.filter(movie__tmdb_rating__isnull=False).values_list('rating', 'movie__tmdb_rating')
    deltas = [float(rating) - float(tmdb_rating) / 2 for rating, tmdb_rating in rows]
    if len(deltas) < MIN_COUNT_FOR_AVERAGE:
        return None, len(deltas)
    return sum(deltas) / len(deltas), len(deltas)


# The 8 signal axes _preference_deltas' own model is built from -- named once
# here since _film_trait_records/_raw_stats_from_records/_axis_deltas_from_records
# (the pieces _preference_deltas' own final delta computation is split into)
# all iterate the same fixed set.
_PREFERENCE_AXES = ('genre', 'director', 'actor', 'country', 'language', 'decade', 'runtime', 'keyword')


def _film_trait_records(rated_qs) -> list:
    """One dict per row in `rated_qs`: {'rating', 'movie_id', <axis>: [values],
    ...} for every _PREFERENCE_AXES entry -- the shared data
    _preference_deltas' own final delta computation works from, fetched via
    one query pass over `rated_qs` (plus one Credit query for lead-billed
    cast) rather than a separate query per axis. actor values come from
    _lead_cast_credit_ids -- top-billed credits only, not every credited cast
    member (see that function's own docstring for why this differs from
    _actor_averages' wider cameo-only exclusion)."""
    movie_ids = list(rated_qs.values_list('movie_id', flat=True))
    lead_cast_ids = _lead_cast_credit_ids(movie_ids)
    actor_names_by_movie = defaultdict(list)
    for movie_id, person_name in (
        Credit.objects.filter(movie_id__in=movie_ids, id__in=lead_cast_ids)
        .values_list('movie_id', 'person__name')
    ):
        actor_names_by_movie[movie_id].append(person_name)

    records = []
    for entry in rated_qs.select_related('movie').prefetch_related(
        'movie__genres', 'movie__directors', 'movie__countries', 'movie__keywords',
    ):
        movie = entry.movie
        records.append({
            'rating': float(entry.rating),
            'movie_id': movie.tmdb_id,
            'genre': [g.name for g in movie.genres.all()],
            'director': [d.name for d in movie.directors.all()],
            'country': [c.name for c in movie.countries.all()],
            'language': [movie.original_language] if movie.original_language else [],
            'decade': [_decade_bucket(movie.release_year)] if movie.release_year else [],
            'runtime': [_runtime_bucket(movie.runtime_minutes)] if movie.runtime_minutes else [],
            'actor': actor_names_by_movie.get(movie.tmdb_id, []),
            'keyword': [k.name for k in movie.keywords.all()],
        })
    return records


def _raw_stats_from_records(records, axis, overall_avg) -> dict:
    """{value: (raw_mean_delta, raw_peak_delta, count, residual_ss)} for one
    axis, built from already-fetched film trait records (see
    _film_trait_records) instead of a fresh DB query -- the raw material
    _axis_deltas_from_records shrinks into the final per-value deltas."""
    ratings_by_key = defaultdict(list)
    for record in records:
        for value in record[axis]:
            ratings_by_key[value].append(record['rating'])
    result = {}
    for key, ratings in ratings_by_key.items():
        mean = sum(ratings) / len(ratings)
        residual_ss = sum((r - mean) ** 2 for r in ratings)
        result[key] = (mean - overall_avg, max(ratings) - overall_avg, len(ratings), residual_ss)
    return result


def _axis_deltas_from_records(records, overall_avg, rated_count) -> dict:
    """{axis: {value: empirical-Bayes-shrunk delta}} for all 8
    _PREFERENCE_AXES, computed from already-fetched film trait records (see
    _film_trait_records) -- the shrinkage core _preference_deltas' own final
    computation calls. See that function's own docstring for the full
    empirical Bayes reasoning (between_var/within_value_variance/rarity)."""
    all_ratings = [record['rating'] for record in records]
    rating_variance = pvariance(all_ratings) if len(all_ratings) >= 2 else 0.0
    if rating_variance == 0.0:
        rating_variance = 1.0

    raw_stats_by_axis = {axis: _raw_stats_from_records(records, axis, overall_avg) for axis in _PREFERENCE_AXES}

    total_residual_ss = sum(
        residual_ss for stats in raw_stats_by_axis.values() for _, _, _, residual_ss in stats.values()
    )
    total_n = sum(count for stats in raw_stats_by_axis.values() for _, _, count, _ in stats.values())
    total_groups = sum(len(stats) for stats in raw_stats_by_axis.values())
    pooled_dof = total_n - total_groups
    within_value_variance = (total_residual_ss / pooled_dof) if pooled_dof > 0 else rating_variance
    if within_value_variance == 0.0:
        within_value_variance = rating_variance

    all_raw_means = [raw_mean for stats in raw_stats_by_axis.values() for raw_mean, _, _, _ in stats.values()]
    all_counts = [count for stats in raw_stats_by_axis.values() for _, _, count, _ in stats.values()]
    observed_var = pvariance(all_raw_means) if len(all_raw_means) >= 2 else 0.0
    avg_sampling_var = (
        sum(within_value_variance / count for count in all_counts) / len(all_counts) if all_counts else 0.0
    )
    between_var = max(0.0, observed_var - avg_sampling_var)

    def _shrink(axis):
        result = {}
        for key, (raw_mean, peak_delta, count, _residual_ss) in raw_stats_by_axis[axis].items():
            sampling_var = within_value_variance / count
            shrink = between_var / (between_var + sampling_var) if (between_var + sampling_var) > 0 else 0.0
            avg_delta = shrink * raw_mean
            shrunk_peak_delta = shrink * peak_delta
            blended = (1 - PEAK_BLEND) * avg_delta + PEAK_BLEND * shrunk_peak_delta
            # Plain _rarity_factor multiply, not scaled by shrink/confidence.
            # A confidence-gated version was tried and reverted: holdout
            # validation against real already-rated films, on two independent
            # pairs, consistently favored this simpler version.
            result[key] = blended * _rarity_factor(count, rated_count)
        return result

    return {axis: _shrink(axis) for axis in _PREFERENCE_AXES}


def _preference_deltas(import_session, exclude_shorts=False):
    """This session's own preference model: {'overall_avg', 'genre', 'director',
    'actor', 'country', 'language', 'decade', 'runtime', 'keyword', 'weights',
    'shrunk_generosity'}, where each of the 8 signal maps is {key: empirical-
    Bayes-shrunk delta from this session's overall average, blended with a
    peak-rating delta and scaled by rarity} -- the same per-signal model
    Director's Cut's own recommender builds, reimplemented locally per this
    file's convention (dashboard.py's version still uses a fixed shrinkage
    constant, a fixed PEAK_BLEND, and no keyword axis -- free to diverge on
    internal methodology like everything else this file reimplements).
    'weights' is _adaptive_weights derived from the 8 maps above.
    'shrunk_generosity' is the confidence-shrunk _generosity_score, used by
    _preference_score for the TMDB_WEIGHT nudge. Returns None if this session
    doesn't have MIN_COUNT_FOR_AVERAGE rated films -- no baseline to compute
    a delta against otherwise."""
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session)).exclude(movie__isnull=True)
    if exclude_shorts:
        rated = exclude_short_entries(rated)
    rated_count = rated.count()
    if rated_count < MIN_COUNT_FOR_AVERAGE:
        return None
    overall_avg = float(rated.aggregate(avg=Avg('rating'))['avg'])

    records = _film_trait_records(rated)
    deltas_by_axis = _axis_deltas_from_records(records, overall_avg, rated_count)

    weights = _adaptive_weights(deltas_by_axis)

    # Confidence-shrunk toward 0 (crowd-aligned) the same way every other thin-
    # evidence signal here is -- generosity_score itself can be None (fewer than
    # MIN_COUNT_FOR_AVERAGE TMDB-enriched rated films), in which case there's
    # nothing to shrink and this just stays 0.
    generosity_score, rated_and_enriched_count = _generosity_score(rated)
    if generosity_score is not None:
        generosity_confidence = rated_and_enriched_count / (rated_and_enriched_count + RECOMMENDATION_SHRINKAGE_K)
        shrunk_generosity = generosity_confidence * generosity_score
    else:
        shrunk_generosity = 0.0

    return {
        'overall_avg': overall_avg,
        **deltas_by_axis,
        'weights': weights, 'shrunk_generosity': shrunk_generosity,
    }


def _preference_score(movie, deltas, actor_names) -> tuple:
    """One person's fit score for `movie` under their own preference-delta maps (see
    _preference_deltas): their overall average plus a weighted SUM of whichever
    signals this film actually has, weighted by deltas['weights'] (see
    _adaptive_weights, not the fixed RECOMMENDATION_WEIGHTS directly) -- within one
    signal, e.g. several genres, values ARE averaged (see RECOMMENDATION_WEIGHTS'
    comment above for why summing across signals but averaging within one). Unlike
    Director's Cut's own _watchlist_recommendations, a film with no matching signal
    on any axis still gets a score here (just its baseline average) rather than being dropped --
    this re-ranks an existing 'you both already want to watch this' list, it doesn't
    build a new discovery list, so nothing should disappear just for not being
    personalized.

    Returns (score, credited_people) -- credited_people is the set of directors/
    actors whose delta cleared CREDIT_THRESHOLD, for PERSON_CREDIT_CAP in
    _rank_watchlist_matches (same reasoning as Director's Cut's own reason-
    threshold-gated people tracking: an actor who's merely shared one rated film
    shouldn't exhaust a genuine favorite's cap slots)."""
    axis_values = defaultdict(list)
    credited_people = set()
    for genre_name in movie.genres.all():
        if genre_name.name in deltas['genre']:
            axis_values['genre'].append(deltas['genre'][genre_name.name])
    for director in movie.directors.all():
        if director.name in deltas['director']:
            delta = deltas['director'][director.name]
            axis_values['director'].append(delta)
            if delta >= CREDIT_THRESHOLD:
                credited_people.add(director.name)
    for actor_name in actor_names:
        if actor_name in deltas['actor']:
            delta = deltas['actor'][actor_name]
            axis_values['actor'].append(delta)
            if delta >= CREDIT_THRESHOLD:
                credited_people.add(actor_name)
    for country in movie.countries.all():
        if country.name in deltas['country']:
            axis_values['country'].append(deltas['country'][country.name])
    if movie.original_language in deltas['language']:
        axis_values['language'].append(deltas['language'][movie.original_language])
    if movie.release_year:
        decade = _decade_bucket(movie.release_year)
        if decade in deltas['decade']:
            axis_values['decade'].append(deltas['decade'][decade])
    if movie.runtime_minutes:
        bucket = _runtime_bucket(movie.runtime_minutes)
        if bucket in deltas['runtime']:
            axis_values['runtime'].append(deltas['runtime'][bucket])
    for keyword in movie.keywords.all():
        if keyword.name in deltas['keyword']:
            axis_values['keyword'].append(deltas['keyword'][keyword.name])

    taste_score = sum(
        deltas['weights'][axis] * (sum(values) / len(values)) for axis, values in axis_values.items()
    )

    # TMDB_WEIGHT's own comment explains why this is fixed rather than adaptively
    # weighted. Unlike Director's Cut, this applies even when axis_values is empty
    # (nothing here ever gets dropped for lacking a taste signal -- see this
    # function's own docstring), so a film with no TMDB rating just uses the plain
    # taste_score, unscaled.
    if movie.tmdb_rating is not None:
        crowd_rating = float(movie.tmdb_rating) / 2
        tmdb_delta = (crowd_rating + deltas['shrunk_generosity']) - deltas['overall_avg']
        score = deltas['overall_avg'] + (1 - TMDB_WEIGHT) * taste_score + TMDB_WEIGHT * tmdb_delta
    else:
        score = deltas['overall_avg'] + taste_score

    return score, credited_people


def _shared_trait_bonus(movie, deltas_a, deltas_b, actor_names) -> float:
    """How much `movie`'s own traits land on values BOTH people have an
    independently well-evidenced, genuinely positive delta for (a shared
    favorite director, say) -- not just "both scores happen to be high for
    unrelated reasons," which the least-misery blend already rewards on its
    own. Fed into that blend as its own small additive term (see
    SHARED_TRAIT_BONUS_WEIGHT).

    A value counts as shared once it clears CREDIT_THRESHOLD on *both* sides,
    contributing min(delta_a, delta_b) -- same least-misery logic, one layer
    deeper. Averaged within an axis, weighted by that axis's average adaptive
    weight, then summed across axes.

    That sum is then scaled up by SHARED_TRAIT_COMBO_BONUS_PER_AXIS once a
    film clears SHARED_TRAIT_COMBO_MIN_AXES qualifying axes -- several
    independently-confirmed shared favorites (director AND country AND
    genre) is stronger evidence than the same total concentrated in one axis,
    which plain summation alone doesn't reward. Country and language don't
    count as two separate axes toward that minimum -- see
    COMBO_COUNT_AXIS_GROUP."""
    axis_shared_deltas = defaultdict(list)

    def _check(axis, value):
        delta_a = deltas_a[axis].get(value)
        delta_b = deltas_b[axis].get(value)
        if delta_a is not None and delta_b is not None and delta_a > CREDIT_THRESHOLD and delta_b > CREDIT_THRESHOLD:
            axis_shared_deltas[axis].append(min(delta_a, delta_b))

    for genre_name in movie.genres.all():
        _check('genre', genre_name.name)
    for director in movie.directors.all():
        _check('director', director.name)
    for actor_name in actor_names:
        _check('actor', actor_name)
    for country in movie.countries.all():
        _check('country', country.name)
    if movie.original_language:
        _check('language', movie.original_language)
    if movie.release_year:
        _check('decade', _decade_bucket(movie.release_year))
    if movie.runtime_minutes:
        _check('runtime', _runtime_bucket(movie.runtime_minutes))
    for keyword in movie.keywords.all():
        _check('keyword', keyword.name)

    if not axis_shared_deltas:
        return 0.0

    base_bonus = sum(
        ((deltas_a['weights'][axis] + deltas_b['weights'][axis]) / 2) * (sum(values) / len(values))
        for axis, values in axis_shared_deltas.items()
    )
    # 'language' collapses into 'country's slot here, so qualifying on both
    # only counts once toward the combo floor (still contributes its own term
    # to base_bonus above, though).
    qualifying_axis_count = len({COMBO_COUNT_AXIS_GROUP.get(axis, axis) for axis in axis_shared_deltas})
    combo_multiplier = 1 + SHARED_TRAIT_COMBO_BONUS_PER_AXIS * max(
        0, qualifying_axis_count - (SHARED_TRAIT_COMBO_MIN_AXES - 1),
    )
    return base_bonus * combo_multiplier


def _eligible_watchlist_matches(matches: list) -> list:
    """Filters `matches` (shared-watchlist films, see _watchlist_map) down to films
    that have actually released (a confirmed future release_year -- there's no exact
    release_date stored, just the year TMDB gave it, so a same-year film that hasn't
    actually come out yet can still slip through) and aren't shorts (a confirmed
    runtime under 60 minutes). An unenriched match (no movie_id at all) can't be
    checked either way, so it's left in rather than excluded on missing data -- same
    as every NULL in these two checks, "unconfirmed" isn't treated as "guilty". This
    is what determines watchlist_matches_total (how many genuine, watchable matches
    exist) -- separate from _rank_watchlist_matches' own PERSON_CREDIT_CAP, which is
    a display-diversity concern, not a question of whether a match "counts"."""
    movie_ids = {f['movie_id'] for f in matches if f.get('movie_id')}
    movies_by_id = Movie.objects.in_bulk(movie_ids)
    current_year = date.today().year

    def _is_eligible(f):
        movie = movies_by_id.get(f.get('movie_id'))
        if movie is None:
            return True
        if movie.release_year is not None and movie.release_year > current_year:
            return False
        if movie.runtime_minutes is not None and movie.runtime_minutes < SHORT_FILM_MAX_RUNTIME_MINUTES:
            return False
        return True

    return [f for f in matches if _is_eligible(f)]


def _axis_agreement(deltas_a, deltas_b) -> dict:
    """{axis: strength} for all 8 _PREFERENCE_AXES, where strength is the single
    STRONGEST shared favorite on that axis -- max(min(delta_a[v], delta_b[v]))
    over every value clearing CREDIT_THRESHOLD on both sides, or 0.0 if
    nothing qualifies. Feeds _rank_watchlist_matches' axis-agreement weight
    boost -- a per-pair measure of which categories these two demonstrably
    agree on, not just this one film.

    Max, not sum or average: checked against real data first. Summing
    rewards whichever axis has the most distinct values (actor dwarfed every
    other axis purely from having more things to add up, not from agreeing
    more); averaging overcorrects, diluting one genuinely strong match by
    averaging it against weak ones until every axis looks equally
    unremarkable. Max answers the actual question -- "is there a genuine
    standout mutual favorite here" -- without either distortion."""
    agreement = {}
    for axis in _PREFERENCE_AXES:
        qualifying_mins = [
            min(delta_a, deltas_b[axis][value])
            for value, delta_a in deltas_a[axis].items()
            if value in deltas_b[axis] and delta_a > CREDIT_THRESHOLD and deltas_b[axis][value] > CREDIT_THRESHOLD
        ]
        agreement[axis] = max(qualifying_mins) if qualifying_mins else 0.0
    return agreement


def _boosted_weights(base_weights: dict, agreement: dict) -> dict:
    """`base_weights` (either person's own _adaptive_weights) with each axis
    scaled up by how much that axis's own _axis_agreement strength compares to
    the pair's single strongest axis, then renormalized back to sum to 1.0 --
    the axis both people agree on most gets the full AXIS_AGREEMENT_BOOST, an
    axis with no shared favorites at all is left untouched, everything else
    scales smoothly in between. Returns `base_weights` unchanged if nothing
    qualified on any axis (max agreement is 0) -- nothing to boost against."""
    max_agreement = max(agreement.values()) if agreement else 0.0
    if max_agreement == 0.0:
        return dict(base_weights)
    boosted = {
        axis: base_weights[axis] * (1 + AXIS_AGREEMENT_BOOST * (agreement[axis] / max_agreement))
        for axis in base_weights
    }
    total = sum(boosted.values())
    return {axis: value / total for axis, value in boosted.items()}


def _rank_watchlist_matches(session_a, session_b, matches: list, display_cap: int, exclude_shorts=False) -> list:
    """Ranks `matches` (already eligibility-filtered, see _eligible_watchlist_matches)
    by joint best-fit for both people, instead of (title, year) order, and
    returns at most `display_cap` of them. PERSON_CREDIT_CAP is a hard
    exclusion: whichever directors/actors either person is credited to can
    drive at most PERSON_CREDIT_CAP picks, backfilling a capped-out slot with
    the next-best real candidate rather than ever padding back up with a
    capped-out one just because there was room left.

    Least-misery-dominant blend -- mostly min(score_a, score_b), with a small
    fraction of the average blended in (LEAST_MISERY_BLEND_WEIGHT) so combined
    enthusiasm can make a real, continuous difference rather than only
    mattering on an exact tie. _shared_trait_bonus adds one more small term on
    top -- a distinct signal from "both scores happen to be high" (the same
    specific director/genre, not two unrelated reasons it works for each of
    you). (title, year) is the final tiebreak for full determinism.

    Before scoring, each person's own _adaptive_weights are boosted toward
    whichever axis this specific pair agrees on most (see
    _axis_agreement/_boosted_weights/AXIS_AGREEMENT_BOOST) -- the same idea
    as _shared_trait_bonus, applied at the axis level instead of per film.
    Only affects the weights used for this ranking, not either person's own
    solo weights elsewhere.

    Falls back to (title, year) order if either session doesn't have enough
    rated films to build a preference profile at all."""
    deltas_a = _preference_deltas(session_a, exclude_shorts)
    deltas_b = _preference_deltas(session_b, exclude_shorts)
    if deltas_a is None or deltas_b is None:
        return sorted(matches, key=lambda f: (f['title'], f['year']))[:display_cap]

    agreement = _axis_agreement(deltas_a, deltas_b)
    deltas_a['weights'] = _boosted_weights(deltas_a['weights'], agreement)
    deltas_b['weights'] = _boosted_weights(deltas_b['weights'], agreement)

    movie_ids = {f['movie_id'] for f in matches if f.get('movie_id')}
    movies_by_id = {
        m.tmdb_id: m
        for m in Movie.objects.filter(tmdb_id__in=movie_ids)
        .prefetch_related('genres', 'directors', 'countries', 'keywords')
    }
    lead_cast_ids = _lead_cast_credit_ids(movie_ids)
    actor_names_by_movie = defaultdict(list)
    for movie_id, person_name in (
        Credit.objects.filter(movie_id__in=movie_ids, id__in=lead_cast_ids)
        .values_list('movie_id', 'person__name')
    ):
        actor_names_by_movie[movie_id].append(person_name)

    scored = []
    for f in matches:
        movie = movies_by_id.get(f.get('movie_id'))
        if movie is None:
            # Unenriched watchlist entry -- no TMDB data to score against, so it
            # falls back to each person's own baseline (least-misery of two equal
            # baselines is still deterministic, just uninformative). Nothing to
            # check a shared trait against either.
            score_a, score_b = deltas_a['overall_avg'], deltas_b['overall_avg']
            people = set()
            shared_bonus = 0.0
        else:
            actor_names = actor_names_by_movie.get(movie.tmdb_id, [])
            score_a, people_a = _preference_score(movie, deltas_a, actor_names)
            score_b, people_b = _preference_score(movie, deltas_b, actor_names)
            people = people_a | people_b
            shared_bonus = _shared_trait_bonus(movie, deltas_a, deltas_b, actor_names)
        least_misery = min(score_a, score_b)
        blended = (
            (1 - LEAST_MISERY_BLEND_WEIGHT) * least_misery
            + LEAST_MISERY_BLEND_WEIGHT * ((score_a + score_b) / 2)
            + SHARED_TRAIT_BONUS_WEIGHT * shared_bonus
        )
        scored.append((f, blended, people))

    scored.sort(key=lambda row: (-row[1], row[0]['title'], row[0]['year']))

    selected = []
    person_credit_counts = defaultdict(int)
    for f, blended, people in scored:
        if len(selected) >= display_cap:
            break
        if any(person_credit_counts[name] >= PERSON_CREDIT_CAP for name in people):
            continue
        for name in people:
            person_credit_counts[name] += 1
        selected.append(f)

    return selected


def _top_people(stats, cap=TOP_N):
    """This session's own top directors/actors by avg rating, from an already-built
    {name: (avg, count, profile_url)} map (_director_averages or _actor_averages) --
    independent of the other session, unlike _shared_people. Sorted by avg
    descending, count as the tiebreak (same ordering shape as _shared_people, just
    single-session).

    cap defaults to TOP_N (the table view actors/directors still use) but the grid
    views pass GRID_DISPLAY_CAP/GRID_DISPLAY_CAP_NARROW instead, same as this file's
    other list-vs-grid caps (see GRID_DISPLAY_CAP's own comment) -- a grid needs to
    fill its shape evenly, which isn't the same number as "top N by ranking" just
    because a table happened to also stop at 10.

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
    return results[:cap]


def _shared_people(stats_a, stats_b, cap=TOP_N):
    """Directors or actors both sessions qualify as a favorite for, from two
    already-built averages maps (same source functions as _top_people). Ranked by
    whichever of the two averages is *lower*, not the combined/mean average -- a
    shared favorite has to be genuinely well-regarded by both people, not one person
    loving them enough to drag a blended average up while the other is lukewarm.
    Ties on that (displayed, 1dp -- see _top_people) lower-average value fall back to
    combined film count.

    cap defaults to TOP_N (the table view) -- see _top_people's own cap docstring for
    why the grid view passes GRID_DISPLAY_CAP instead."""
    results = [
        {
            'name': name, 'avg_a': stats_a[name][0], 'avg_b': stats_b[name][0],
            'count_a': stats_a[name][1], 'count_b': stats_b[name][1],
            # Either session's copy works equally well here -- both were rated by the
            # same real person, so their profile photo/tmdb_id can't differ between
            # sessions. tmdb_id feeds the Shared panel's own click-through modal
            # (both sessions' filmography for this one person, fetched by the same
            # id from each side).
            'profile_url': stats_a[name][2] or stats_b[name][2],
            'tmdb_id': stats_a[name][3] or stats_b[name][3],
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
    return results[:cap]


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


def _alignment_blurb(overlap_pct, agreement_pct):
    """{'overlap_clause', 'connector', 'agreement_clause'} -- the pieces of one
    plain-language sentence translating overlap_pct/agreement_pct into words, for
    the hero's Overall alignment row. Returned as separate pieces rather than one
    joined string so the template can highlight the two clauses (the actual
    "what does this mean" content) differently from the connector/punctuation
    around them, without reaching for mark_safe/|safe on server-built HTML.

    Each stat is bucketed into its own tier independently (not derived from the
    blended compatibility_pct), since a low-overlap/high-agreement pair and a
    high-overlap/low-agreement pair are both real, different stories that one
    blended number can't tell apart on its own. The two clauses are joined with
    'but' only when exactly one of the two tiers is the bottom tier (ordinal 0)
    and the other isn't -- e.g. "rarely overlap... but usually agree on them" is
    a genuine contrast worth flagging, but two merely-mediocre tiers (ordinal 1
    vs ordinal 2, say) don't read as one, so they still get 'and'; both at the
    bottom tier is also 'and' (rock-bottom on both isn't a contrast either).
    Returns None when agreement_pct itself is None (no shared rated films yet),
    the same gate compatibility_pct's own None already reflects -- nothing to
    describe yet."""
    if agreement_pct is None:
        return None

    def _tier(pct, breakpoints, clauses):
        for ordinal, (breakpoint, clause) in enumerate(zip(breakpoints, clauses)):
            if pct < breakpoint:
                return ordinal, clause
        return len(clauses) - 1, clauses[-1]

    overlap_ordinal, overlap_clause = _tier(
        overlap_pct, [20, 45, 70],
        [
            "rarely overlap in what you've watched",
            "have some overlap in what you've watched",
            "watch a lot of the same things",
            "watch almost the exact same things",
        ],
    )
    agreement_ordinal, agreement_clause = _tier(
        agreement_pct, [40, 60, 85],
        [
            'rate them pretty differently',
            'sometimes agree on them',
            'usually agree on them',
            'rate them almost identically',
        ],
    )
    same_side = (overlap_ordinal >= 1) == (agreement_ordinal >= 1)
    connector = 'and' if same_side else 'but'
    return {'overlap_clause': overlap_clause, 'connector': connector, 'agreement_clause': agreement_clause}


def build_compare_context(session_a, session_b, exclude_shorts=False) -> dict:
    map_a = _film_map(session_a, exclude_shorts)
    map_b = _film_map(session_b, exclude_shorts)

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

    watchlist_a = _watchlist_map(session_a, exclude_shorts)
    watchlist_b = _watchlist_map(session_b, exclude_shorts)
    shared_watchlist_keys = set(watchlist_a) & set(watchlist_b)
    same_rating_expanded = _same_rating_display(same_rating_all, GRID_EXPANDED_CAP, GRID_HIGH_RATING_SLOTS_EXPANDED)
    same_rating_collapsed_ids = {
        id(f) for f in _same_rating_display(same_rating_all, GRID_DISPLAY_CAP, GRID_HIGH_RATING_SLOTS)
    }
    same_rating_collapsed_positions = [
        position for position, f in enumerate(same_rating_expanded) if id(f) in same_rating_collapsed_ids
    ]

    # Ranked by joint best-fit for both people -- see _rank_watchlist_matches for
    # the least-misery combination and its fallback. Eligibility (released,
    # feature-length) is filtered once here, separate from ranking, since it's
    # what watchlist_matches_total counts, not a PERSON_CREDIT_CAP display concern.
    # Always excludes shorts regardless of the exclude_shorts toggle -- an
    # unconditional "is this actually watchable" rule, not the toggle's concern.
    watchlist_eligible = _eligible_watchlist_matches([watchlist_a[k] for k in shared_watchlist_keys])
    watchlist_matches_ranked = _rank_watchlist_matches(
        session_a, session_b, watchlist_eligible, GRID_EXPANDED_CAP, exclude_shorts,
    )

    same_day = _same_day_logs(session_a, session_b, exclude_shorts)
    same_day_logs_all = same_day['logs']
    same_day_exact_matches_all = same_day['exact_matches']
    same_day_heatmap = _same_day_heatmap(same_day_logs_all, same_day_exact_matches_all)
    # Flattened views over the same nested film dicts inside same_day_logs_all --
    # _resolve_posters mutates dicts in place, so resolving through these flat lists
    # still resolves onto the nested per-date films_a/films_b lists too.
    same_day_films_a = [f for entry in same_day_logs_all for f in entry['films_a']]
    same_day_films_b = [f for entry in same_day_logs_all for f in entry['films_b']]
    _resolve_same_day_ratings(map_a, map_b, same_day_films_a, same_day_films_b)

    top_unseen_a_all = _top_unseen_by_other(session_a, keys_b, exclude_shorts)
    top_unseen_b_all = _top_unseen_by_other(session_b, keys_a, exclude_shorts)

    curve_a = _rating_curve(session_a, exclude_shorts)
    curve_b = _rating_curve(session_b, exclude_shorts)

    # Watching Habits: films per year (zero-filled across the union of both
    # sessions' own diary years -- see _films_per_year's own comment), most
    # rewatched films (independent per session, no Shared view -- see
    # _most_watched_films' own comment), and the non-heatmap viewing-calendar
    # stats (see _viewing_habits' own comment on why there's no two-person
    # heatmap here).
    films_per_year_a = _films_per_year(session_a, exclude_shorts)
    films_per_year_b = _films_per_year(session_b, exclude_shorts)
    films_per_year_years = sorted(set(films_per_year_a) | set(films_per_year_b))
    most_watched_films_a = _most_watched_films(session_a, GRID_DISPLAY_CAP_NARROW, exclude_shorts)
    most_watched_films_b = _most_watched_films(session_b, GRID_DISPLAY_CAP_NARROW, exclude_shorts)
    viewing_habits_a = _viewing_habits(session_a, exclude_shorts)
    viewing_habits_b = _viewing_habits(session_b, exclude_shorts)

    # Each session's director/actor averages computed once and reused by both
    # _top_people (this session alone) and _shared_people (the intersection) --
    # avoids querying the same session's stats twice over.
    director_stats_a = _director_averages(session_a, MIN_COUNT_FOR_FAVORITE_DIRECTOR, exclude_shorts)
    director_stats_b = _director_averages(session_b, MIN_COUNT_FOR_FAVORITE_DIRECTOR, exclude_shorts)
    actor_stats_a = _actor_averages(session_a, MIN_COUNT_FOR_FAVORITE_ACTOR, exclude_shorts)
    actor_stats_b = _actor_averages(session_b, MIN_COUNT_FOR_FAVORITE_ACTOR, exclude_shorts)

    # Directors and actors both render as grids now (SHARED_PEOPLE_GRID_CAP/
    # GRID_DISPLAY_CAP_NARROW), same as this file's other poster grids -- not TOP_N,
    # which is the table view's own cap.
    shared_directors = _shared_people(director_stats_a, director_stats_b, cap=SHARED_PEOPLE_GRID_CAP)
    shared_actors = _shared_people(actor_stats_a, actor_stats_b, cap=SHARED_PEOPLE_GRID_CAP)
    top_directors_a = _top_people(director_stats_a, cap=GRID_DISPLAY_CAP_NARROW)
    top_directors_b = _top_people(director_stats_b, cap=GRID_DISPLAY_CAP_NARROW)
    top_actors_a = _top_people(actor_stats_a, cap=GRID_DISPLAY_CAP_NARROW)
    top_actors_b = _top_people(actor_stats_b, cap=GRID_DISPLAY_CAP_NARROW)

    # _genre_agreement sorts gap-ascending (agreement first) -- genre_agreement is
    # that order's front TOP_N, the card's default view. genre_agreement_least
    # re-sorts the same rows gap-descending (worst clashes first) for the "Least
    # agreed" toggle, rather than re-querying.
    genre_agreement_all = _genre_agreement(
        _genre_averages(session_a, exclude_shorts), _genre_averages(session_b, exclude_shorts),
    )
    genre_agreement = genre_agreement_all[:TOP_N]
    genre_agreement_least = sorted(genre_agreement_all, key=lambda r: (-r['gap'], r['genre']))[:TOP_N]

    # One bulk lookup spanning every film list on the page rather than a query per
    # list: shared_films (and biggest_disagreements/same_rating, built from the
    # same dict objects), top_unseen_a/b, watchlist_matches_ranked (the already-
    # capped display list, not the wider watchlist_eligible pool), same-day films,
    # same_day_exact_matches_all, and most_watched_films_a/b.
    movie_ids = {
        f['movie_id']
        for f in (
            shared_films
            + top_unseen_a_all + top_unseen_b_all + watchlist_matches_ranked
            + same_day_films_a + same_day_films_b + same_day_exact_matches_all
            + most_watched_films_a + most_watched_films_b
        )
        if f.get('movie_id')
    }
    movies_by_id = Movie.objects.in_bulk(movie_ids)
    _resolve_posters(
        movies_by_id, shared_films,
        top_unseen_a_all, top_unseen_b_all, watchlist_matches_ranked,
        same_day_films_a, same_day_films_b, same_day_exact_matches_all,
        most_watched_films_a, most_watched_films_b,
    )

    overlap_pct = round(len(shared_keys) / union_size * 100, 1) if union_size else 0
    agreement_pct = round(agree_count / len(rated_shared) * 100, 1) if rated_shared else None
    # Same MIN_COUNT_FOR_AVERAGE gate avg_delta itself uses below -- a single
    # mutually-rated film gives agreement_pct something to show (100% or 0%,
    # trivially) but isn't enough of a sample for "average gap" to mean anything.
    avg_delta = (
        round(float(sum(rated_deltas) / len(rated_deltas)), 1)
        if len(rated_deltas) >= MIN_COUNT_FOR_AVERAGE else None
    )
    # A single headline number blending "how much do you watch the same things"
    # (overlap_pct) with "when you do, do you feel the same way" -- the second
    # half averages agreement_pct (a coarse within-0.5-stars threshold) with
    # avg_delta (normalized to 0-100), since the threshold alone can't tell
    # "always juuust misses the cutoff" apart from "rates everything wildly
    # differently." Kept at a 50/50 overlap/taste split so avg_delta enriches the
    # taste half rather than letting two taste measures outweigh overlap.
    #
    # Falls back to plain overlap/agreement when there's only one mutually-rated
    # film, and to None entirely when there's none.
    if avg_delta is not None:
        gap_pct = round(float(100 * (1 - Decimal(str(avg_delta)) / MAX_RATING_DELTA)), 1)
        taste_pct = round((agreement_pct + gap_pct) / 2, 1)
        compatibility_pct = round((overlap_pct + taste_pct) / 2, 1)
    elif agreement_pct is not None:
        compatibility_pct = round((overlap_pct + agreement_pct) / 2, 1)
    else:
        compatibility_pct = None
    # SVG stroke-dashoffset for the Overall alignment ring -- 0 offset draws the
    # full circumference (a full ring), the full circumference as offset draws
    # none of it, so this is just that scale run in reverse against the percent.
    # Falls back to a full offset (empty ring) when there's no score to show,
    # matching compatibility_pct's own None.
    compatibility_gauge_offset = (
        round(ALIGNMENT_GAUGE_CIRCUMFERENCE * (1 - compatibility_pct / 100), 2)
        if compatibility_pct is not None else ALIGNMENT_GAUGE_CIRCUMFERENCE
    )
    alignment_blurb = _alignment_blurb(overlap_pct, agreement_pct)

    return {
        'session_a': session_a,
        'session_b': session_b,
        'exclude_shorts': exclude_shorts,
        'shared_count': len(shared_keys),
        'only_a_count': len(only_a_keys),
        'only_b_count': len(only_b_keys),
        # Every distinct (title, year) either session has a rating.csv or
        # watched.csv record for (see _film_map) -- shared_keys + only_a_keys/
        # only_b_keys is the same set partitioned three ways, so watched_count_a
        # always equals shared_count + only_a_count (and likewise for b). For the
        # hero's "# films" stat, not previously surfaced anywhere on this page.
        'watched_count_a': len(keys_a),
        'watched_count_b': len(keys_b),
        'rated_higher_a_count': rated_higher_a_count,
        'rated_higher_b_count': rated_higher_b_count,
        'overlap_pct': overlap_pct,
        'agreement_pct': agreement_pct,
        'compatibility_pct': compatibility_pct,
        'compatibility_gauge_offset': compatibility_gauge_offset,
        'alignment_blurb': alignment_blurb,
        'avg_delta': avg_delta,
        'grid_display_cap': GRID_DISPLAY_CAP,
        'biggest_disagreements': biggest_disagreements_all[:GRID_EXPANDED_CAP],
        'biggest_disagreements_total': len(biggest_disagreements_all),
        'same_rating': same_rating_expanded,
        # Positions in same_rating that belong to the default (collapsed) 12 --
        # that selection spreads across rating tiers on its own, so it isn't just
        # the first 12 of the expanded list.
        'same_rating_collapsed_positions': same_rating_collapsed_positions,
        'same_rating_total': len(same_rating_all),
        'shared_directors': shared_directors,
        'shared_actors': shared_actors,
        'top_directors_a': top_directors_a,
        'top_directors_b': top_directors_b,
        'top_actors_a': top_actors_a,
        'top_actors_b': top_actors_b,
        'most_watched_films_a': most_watched_films_a,
        'most_watched_films_b': most_watched_films_b,
        'viewing_habits_a': viewing_habits_a,
        'viewing_habits_b': viewing_habits_b,
        'genre_agreement': genre_agreement,
        'watchlist_matches': watchlist_matches_ranked,
        'watchlist_matches_total': len(watchlist_eligible),
        # SAME_DAY_LOGS_GRID_CAP, not GRID_DISPLAY_CAP -- own cap, kept independent
        # of the poster grids' (see that constant's own comment for why this key
        # isn't rendered directly either, same as same_day_exact_matches below).
        'same_day_logs': same_day_logs_all[:SAME_DAY_LOGS_GRID_CAP],
        'same_day_logs_total': len(same_day_logs_all),
        # Top-level, not just inside chart_data below -- same_day_heatmap.years
        # drives the server-rendered year-toggle buttons (Django can't reach into
        # chart_data, which only exists client-side via json_script), the same
        # "in both places" split Director's Cut's own calendar.heatmap uses.
        'same_day_heatmap': same_day_heatmap,
        # GRID_DISPLAY_CAP, sharing Same rating/Most different ratings/Watchlist
        # matches' own cap -- not rendered as its own grid (the heatmap replaced
        # the old day-grid display this fed; see same_day_logs' own comment
        # above), but same_day_exact_matches_all's computation still feeds that
        # heatmap, so this key is kept and still capped for its own test coverage.
        'same_day_exact_matches': same_day_exact_matches_all[:GRID_DISPLAY_CAP],
        'same_day_exact_matches_total': len(same_day_exact_matches_all),
        'top_unseen_a': top_unseen_a_all[:GRID_DISPLAY_CAP_NARROW],
        'top_unseen_b': top_unseen_b_all[:GRID_DISPLAY_CAP_NARROW],
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
            # Same {labels, data_a, data_b} shape as rating_curve above, but feeds a
            # horizontal bar chart (indexAxis: 'y') since genre names read better as
            # a Y-axis list. Two pre-built views, 'most' and 'least', since unlike
            # rating_curve's Percent toggle, most-vs-least agreed are two different
            # genre subsets in two different orders -- both sent ready-to-render,
            # already capped to TOP_N and sorted.
            'genre_agreement': {
                'most': {
                    'labels': [row['genre'] for row in genre_agreement],
                    'data_a': [row['avg_a'] for row in genre_agreement],
                    'data_b': [row['avg_b'] for row in genre_agreement],
                },
                'least': {
                    'labels': [row['genre'] for row in genre_agreement_least],
                    'data_a': [row['avg_a'] for row in genre_agreement_least],
                    'data_b': [row['avg_b'] for row in genre_agreement_least],
                },
                'label_a': session_a.display_name or 'Person A',
                'label_b': session_b.display_name or 'Person B',
            },
            # Same {years, default_year, data: {year: {...}}} shape as Director's
            # Cut's own chartData.heatmap and the same client-side script, except
            # each cell's payload is a {state, films_a, films_b} here instead of a
            # count, since this heatmap is categorical, not volume-based. films_a/
            # films_b are full film dicts (resolved above) so the day-detail popup
            # can render real posters and ratings. label_a/label_b added here since
            # _same_day_heatmap itself has no session to name.
            'same_day_heatmap': {
                **same_day_heatmap,
                'label_a': session_a.display_name or 'Person A',
                'label_b': session_b.display_name or 'Person B',
            },
            # Watching Habits' films-per-year chart -- same {labels, data_a,
            # data_b} grouped-bar shape as rating_curve/genre_agreement above,
            # zero-filled across films_per_year_years (the union of both
            # sessions' own diary years, computed above) rather than each
            # session's own sparse year set, so a year either of them logged
            # anything in gets a real 0 bar for the other, not a silently
            # missing column.
            'films_per_year': {
                'labels': [str(year) for year in films_per_year_years],
                'data_a': [films_per_year_a.get(year, 0) for year in films_per_year_years],
                'data_b': [films_per_year_b.get(year, 0) for year in films_per_year_years],
                'label_a': session_a.display_name or 'Person A',
                'label_b': session_b.display_name or 'Person B',
            },
            # Watching Habits' weekday-distribution chart -- same grouped-bar
            # shape again, fixed WEEKDAY_LABELS scale (always all 7 days, same
            # "gap-free axis" reasoning as _rating_curve's own RATING_BUCKETS).
            # count_a/count_b feed its own Films/Percent toggle, same purpose
            # and client-side derivation (see toPercent in compare.html) as
            # rating_curve's count_a/count_b above.
            'weekday_distribution': {
                'labels': WEEKDAY_LABELS,
                'data_a': viewing_habits_a['weekday_counts'],
                'data_b': viewing_habits_b['weekday_counts'],
                'count_a': viewing_habits_a['total_count'],
                'count_b': viewing_habits_b['total_count'],
                'label_a': session_a.display_name or 'Person A',
                'label_b': session_b.display_name or 'Person B',
            },
        },
    }
