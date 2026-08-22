"""Read-only aggregation for the single-import dashboard. Everything here is computed
on the fly via the ORM rather than cached -- simple and correct-by-construction, which
matters more than raw speed for a learning project at this scale.

Three data sources are used deliberately:
  - RatingEntry (one row per distinct film, authoritative rating) for anything computing
    an average rating per film -- using DiaryEntry here would double-count rewatches.
  - DiaryEntry (one row per watch event, has watched_date + rewatch) for anything about
    viewing activity over time.
  - WatchedEntry (one row per distinct film, logged or not) for "most watched" counts
    (genre/director/actor/country/language) -- see _watched_movies.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg, Count, Min, Sum
from django.db.models.functions import ExtractWeekDay, ExtractYear, TruncMonth

from imports.models import DiaryEntry, LikedFilmEntry, RatingEntry, ReviewEntry, WatchedEntry, WatchlistEntry
from stats.services.filters import exclude_tv_shows
from tmdb.models import Credit, Movie

WEEKDAY_NAMES = {1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday', 5: 'Thursday', 6: 'Friday', 7: 'Saturday'}
# TMDB's production_countries gives full formal names -- shortened to their common
# abbreviations for chart labels, which get cramped in the narrow three-across layout.
COUNTRY_NAME_OVERRIDES = {'United States of America': 'USA', 'United Kingdom': 'UK'}
TOP_N = 10
# An "average" of a single data point isn't meaningful -- every average-producing stat
# in this file requires at least this many entries, or it's left out / shown as None
# rather than asserting a fake average.
MIN_COUNT_FOR_AVERAGE = 2
# Favorite Directors/Actors' "Highest rated" tab needs a stronger signal than the
# general MIN_COUNT_FOR_AVERAGE -- a person you've only rated a couple films by can
# still look like a "favorite" by fluke. An actor appears in far more films than a
# director does (a film has one director but a whole cast), so it takes more rated
# appearances before an actor's average is as meaningful as a director's.
MIN_COUNT_FOR_FAVORITE_DIRECTOR = 3
MIN_COUNT_FOR_FAVORITE_ACTOR = 4
# A billing position at or past this fraction of a movie's total cast size is treated
# as a cameo and excluded from every actor stat -- e.g. 0.5 means "in the back half
# of the credited cast". Only applied to movies with at least this many total credited
# cast members (a small indie's "order 8 of 12" isn't a cameo the way a blockbuster's
# "order 25 of 75" is). Both numbers are deliberately tunable -- there's no ground
# truth for "is this a cameo", just real examples to sanity-check against (Stan Lee's
# Marvel cameos consistently land around a 0.4-0.6 relative billing in casts of
# 40-120; a lead actor stays well under 0.1 regardless of cast size).
MIN_CAST_SIZE_FOR_CAMEO_FILTER = 30
CAMEO_RELATIVE_BILLING_THRESHOLD = 0.4


def _avg_or_none(values) -> float | None:
    """Average of a list of Decimal/float values, or None if there aren't enough."""
    if len(values) < MIN_COUNT_FOR_AVERAGE:
        return None
    return float(sum(values) / len(values))


def _tmdb_image_url(path: str, size: str) -> str:
    """Builds a TMDB image URL from a raw path string, e.g. Movie.poster_path or
    Person.profile_path pulled via .values()/Min() aggregation rather than a model
    instance -- Movie.poster_url/Person.profile_url are proper model properties, but
    those only help when a query returns real instances, not dict rows."""
    return f'https://image.tmdb.org/t/p/{size}{path}' if path else ''


def _rounded_or_unrated(avg_rating):
    """Sort key for an avg_rating that may be None -- rounds to the same 1dp shown
    on screen (so a count tiebreak isn't silently skipped over a difference the user
    can't see, e.g. true averages 4.55 vs 4.625 both displaying as "4.6") and sorts
    unrated (None) lowest rather than crashing on a None comparison."""
    return round(avg_rating, 1) if avg_rating is not None else Decimal('-1')


def _cameo_credit_ids(movie_ids) -> set:
    """Credit ids that count as a cameo under CAMEO_RELATIVE_BILLING_THRESHOLD /
    MIN_CAST_SIZE_FOR_CAMEO_FILTER, for the given movie ids. Excluded from every
    actor stat -- 'most watched', its avg-rating column, and 'highest rated' all
    share this same exclusion set, so an actor's numbers stay consistent across
    every view rather than counting cameos in one place and not another."""
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


def _watched_movies(import_session, diary, rated):
    """Movie objects for every distinct film actually watched, logged or not -- the
    same 'watched.csv is authoritative, else union of diary+ratings' pattern as
    _films_watched_total (see its docstring). This is the source for every 'most
    watched' breakdown (genre/director/actor/country/language) so a rewatch doesn't
    inflate a count and a watched.csv-only film (never diary-logged) still counts."""
    watched = exclude_tv_shows(WatchedEntry.objects.filter(import_session=import_session))
    movie_ids = set(watched.exclude(movie__isnull=True).values_list('movie_id', flat=True))
    if not movie_ids:
        movie_ids = set(diary.exclude(movie__isnull=True).values_list('movie_id', flat=True))
        movie_ids |= set(rated.exclude(movie__isnull=True).values_list('movie_id', flat=True))
    return Movie.objects.filter(tmdb_id__in=movie_ids)


def build_dashboard_context(import_session) -> dict:
    diary_all = DiaryEntry.objects.filter(import_session=import_session)
    rated_all = RatingEntry.objects.filter(import_session=import_session)
    diary = exclude_tv_shows(diary_all)
    rated = exclude_tv_shows(rated_all)
    watched_movies = _watched_movies(import_session, diary, rated)
    # Deduped by (title, year) like every other distinct-film count on this page -- a
    # review or like logged against a rewatch shouldn't count twice.
    reviews_count = (
        exclude_tv_shows(ReviewEntry.objects.filter(import_session=import_session))
        .exclude(review='').values('title', 'year').distinct().count()
    )
    likes_count = (
        exclude_tv_shows(LikedFilmEntry.objects.filter(import_session=import_session))
        .values('title', 'year').distinct().count()
    )
    watchlist_count = (
        exclude_tv_shows(WatchlistEntry.objects.filter(import_session=import_session))
        .values('title', 'year').distinct().count()
    )
    # Distinct (title, year) pairs excluded from either source -- a title can be TV-
    # flagged and present in only one of diary/ratings (e.g. rated but never diary-
    # logged), so counting diary rows alone would undercount.
    excluded_pairs = set(diary_all.values_list('title', 'year')) - set(diary.values_list('title', 'year'))
    excluded_pairs |= set(rated_all.values_list('title', 'year')) - set(rated.values_list('title', 'year'))
    excluded_tv_count = len(excluded_pairs)
    excluded_tv_titles = sorted(
        ({'title': title, 'year': year} for title, year in excluded_pairs), key=lambda e: e['title']
    )

    films_per_year = list(
        diary.annotate(y=ExtractYear('watched_date'))
        .values('y')
        .annotate(count=Count('id'))
        .order_by('y')
    )

    # Sourced from rated (RatingEntry, i.e. ratings.csv), not diary -- one row per
    # distinct rated film, so a heavily rewatched film doesn't inflate its rating's bar.
    rating_distribution = list(
        rated.values('rating').annotate(count=Count('id')).order_by('rating')
    )

    # "Most watched" counts distinct films (watched_movies, sourced from watched.csv),
    # not diary rows -- a rewatch shouldn't inflate a genre/director/actor's count, and
    # a film that's in watched.csv but was never diary-logged still needs to count.
    top_genres = list(
        watched_movies.filter(genres__isnull=False)
        .values('genres__name')
        .annotate(count=Count('tmdb_id'))
        .order_by('-count')[:TOP_N]
    )

    # avg_rating is joined in separately from rated (RatingEntry, i.e. ratings.csv) --
    # the "most watched" count and the "highest rated" average necessarily come from
    # different sources (watched.csv has no rating column), so they're computed
    # independently and merged by name. rating_count enforces MIN_COUNT_FOR_AVERAGE
    # (a director with 5 watched films but only 1 rated shouldn't show a 1-data-point
    # "average").
    director_ratings = {
        row['movie__directors__name']: row
        for row in rated.filter(movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(rating_count=Count('id'), avg_rating=Avg('rating'))
    }
    # Sliced to TOP_N only after avg_rating is merged in below, not at the DB query
    # level -- a tie in count has to be broken by avg_rating before truncating, or a
    # higher-rated director could get cut in favor of a lower-rated one with the same
    # watch count. profile_path uses Min() since grouping by name -- everyone in a
    # given name group is the same Person, so it's just a way to carry one scalar
    # value through a GROUP BY, not a real aggregation choice.
    top_directors = list(
        watched_movies.filter(directors__isnull=False)
        .values('directors__name')
        .annotate(count=Count('tmdb_id'), profile_path=Min('directors__profile_path'))
    )
    for row in top_directors:
        stats = director_ratings.get(row['directors__name'])
        row['avg_rating'] = stats['avg_rating'] if stats and stats['rating_count'] >= MIN_COUNT_FOR_AVERAGE else None
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')
    top_directors.sort(key=lambda r: (r['count'], _rounded_or_unrated(r['avg_rating'])), reverse=True)
    top_directors = top_directors[:TOP_N]

    # Cameo exclusion is computed once over every movie either "most watched" or
    # "highest rated" could reference, then reused for both -- see _cameo_credit_ids.
    cameo_credit_ids = _cameo_credit_ids(
        set(watched_movies.values_list('tmdb_id', flat=True))
        | set(rated.exclude(movie__isnull=True).values_list('movie_id', flat=True))
    )

    # actor_rating_lists is per-movie ratings grouped by (non-cameo) actor -- shared
    # by top_actors' avg_rating column below and by _favorite_people's favorite_actors,
    # so an actor's "highest rated" and "most watched" numbers can never disagree
    # about which of their appearances actually count.
    rated_ratings_by_movie = dict(rated.exclude(movie__isnull=True).values_list('movie_id', 'rating'))
    actor_rating_lists = defaultdict(list)
    # Alongside each actor's per-movie ratings, also track one profile_path per name
    # so favorite_actors (built from actor_rating_lists in _favorite_people) can show
    # a headshot too, without a second query back through Credit.
    actor_profile_paths = {}
    for person_name, movie_id, profile_path in (
        Credit.objects.filter(movie_id__in=rated_ratings_by_movie)
        .exclude(id__in=cameo_credit_ids)
        .values_list('person__name', 'movie_id', 'person__profile_path')
    ):
        actor_rating_lists[person_name].append(rated_ratings_by_movie[movie_id])
        actor_profile_paths[person_name] = profile_path

    top_actors = list(
        Credit.objects.filter(movie__in=watched_movies)
        .exclude(id__in=cameo_credit_ids)
        .values('person__name')
        .annotate(count=Count('movie', distinct=True), profile_path=Min('person__profile_path'))
    )
    for row in top_actors:
        ratings = actor_rating_lists.get(row['person__name'], [])
        row['avg_rating'] = sum(ratings) / len(ratings) if len(ratings) >= MIN_COUNT_FOR_AVERAGE else None
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')
    top_actors.sort(key=lambda r: (r['count'], _rounded_or_unrated(r['avg_rating'])), reverse=True)
    top_actors = top_actors[:TOP_N]

    # Sourced from rated (RatingEntry, i.e. ratings.csv), not diary -- this sits right
    # above the rating_distribution chart (also ratings.csv-sourced now), so both
    # describe the same set of distinct rated films rather than a rewatch-weighted one.
    rated_count = rated.count()
    avg_rating = rated.aggregate(avg=Avg('rating'))['avg'] if rated_count >= MIN_COUNT_FOR_AVERAGE else None
    total_runtime_minutes = (
        diary.filter(movie__runtime_minutes__isnull=False).aggregate(total=Sum('movie__runtime_minutes'))['total']
        or 0
    )

    total_films = diary.count()
    unenriched_count = diary.filter(movie__isnull=True).count()
    films_watched_total = _films_watched_total(import_session, diary, rated)

    taste = _taste_vs_crowd(rated)
    genre_decade = _rating_by_genre_and_decade(rated)
    release_year_range = _release_year_range(diary, rated)
    release_year_distribution = _release_year_distribution(diary, release_year_range)
    rating_by_release_year = _rating_by_release_year(rated, release_year_range)
    country_distribution = _films_by_country(watched_movies)
    language_distribution = _films_by_language(watched_movies)
    rating_by_country = _rating_by_country(rated)
    rating_by_language = _rating_by_language(rated)
    rewatch = _rewatch_leaderboard(diary)
    calendar = _viewing_calendar(diary)
    favorite_people = _favorite_people(rated, actor_rating_lists, actor_profile_paths)
    favorites = _favorite_films(import_session)
    top_tags = _tag_distribution(diary)
    highlights = _highlights(watched_movies, rated, rewatch['most_rewatched_films'])
    # First favorite with a resolved poster, used as the header banner's backdrop --
    # not necessarily favorites[0] itself, since an earlier favorite might not have
    # resolved to a Movie (and therefore have no poster) while a later one did.
    hero_poster_url = next((f['movie'].poster_url for f in favorites if f['movie'] and f['movie'].poster_url), '')

    return {
        'import_session': import_session,
        'total_films': total_films,
        'films_watched_total': films_watched_total,
        'films_rated': rated_count,
        'reviews_count': reviews_count,
        'likes_count': likes_count,
        'watchlist_count': watchlist_count,
        'rewatch_count': diary.filter(rewatch=True).count(),
        'avg_rating': avg_rating,
        'total_runtime_minutes': total_runtime_minutes,
        'total_runtime_hours': round(total_runtime_minutes / 60) if total_runtime_minutes else 0,
        'unenriched_count': unenriched_count,
        'excluded_tv_count': excluded_tv_count,
        'excluded_tv_titles': excluded_tv_titles,
        'top_genres': top_genres,
        'top_directors': top_directors,
        'top_actors': top_actors,
        'taste': taste,
        'genre_decade': genre_decade,
        'release_year_distribution': release_year_distribution,
        'country_distribution': country_distribution,
        'language_distribution': language_distribution,
        'rewatch': rewatch,
        'calendar': calendar,
        'favorite_people': favorite_people,
        'favorites': favorites,
        'hero_poster_url': hero_poster_url,
        'top_tags': top_tags,
        'highlights': highlights,
        'chart_data': {
            'films_per_year': {
                'labels': [str(row['y']) for row in films_per_year],
                'data': [row['count'] for row in films_per_year],
            },
            'rating_distribution': {
                'labels': [str(row['rating']) for row in rating_distribution],
                'data': [row['count'] for row in rating_distribution],
            },
            'top_genres': {
                'labels': [row['genres__name'] for row in top_genres],
                'data': [row['count'] for row in top_genres],
            },
            'rating_by_genre': {
                'labels': [row['label'] for row in genre_decade['by_genre']],
                'data': [row['avg'] for row in genre_decade['by_genre']],
            },
            'rating_by_decade': {
                'labels': [row['label'] for row in genre_decade['by_decade']],
                'data': [row['avg'] for row in genre_decade['by_decade']],
            },
            'weekday_distribution': {
                'labels': [row['label'] for row in calendar['weekday_distribution']],
                'data': [row['count'] for row in calendar['weekday_distribution']],
            },
            'release_year_distribution': {
                'labels': [str(row['year']) for row in release_year_distribution],
                'data': [row['count'] for row in release_year_distribution],
            },
            'rating_by_release_year': {
                'labels': [str(row['year']) for row in rating_by_release_year],
                'data': [row['avg'] for row in rating_by_release_year],
            },
            'country_distribution': {
                'labels': [row['label'] for row in country_distribution],
                'data': [row['count'] for row in country_distribution],
            },
            'language_distribution': {
                'labels': [row['label'] for row in language_distribution],
                'data': [row['count'] for row in language_distribution],
            },
            'rating_by_country': {
                'labels': [row['label'] for row in rating_by_country],
                'data': [row['avg'] for row in rating_by_country],
            },
            'rating_by_language': {
                'labels': [row['label'] for row in rating_by_language],
                'data': [row['avg'] for row in rating_by_language],
            },
            'top_tags': {
                'labels': [row['label'] for row in top_tags],
                'data': [row['count'] for row in top_tags],
            },
        },
    }


def _taste_vs_crowd(rated) -> dict:
    """Your rating vs. TMDB's community average (normalized from a 10-point to a
    5-point scale), for every film you've rated that's been TMDB-enriched."""
    rows = rated.filter(movie__tmdb_rating__isnull=False).values(
        'title', 'year', 'rating', 'movie__tmdb_rating', 'movie__poster_path'
    )

    deltas = []
    for row in rows:
        crowd_rating = row['movie__tmdb_rating'] / Decimal('2')
        delta = row['rating'] - crowd_rating
        deltas.append({
            'title': row['title'],
            'year': row['year'],
            'your_rating': row['rating'],
            'crowd_rating': crowd_rating,
            'delta': delta,
            'poster_url': _tmdb_image_url(row['movie__poster_path'], 'w92'),
        })

    raw_avg = _avg_or_none([d['delta'] for d in deltas])
    generosity_score = round(raw_avg, 2) if raw_avg is not None else None
    overrates = sorted(deltas, key=lambda d: d['delta'], reverse=True)[:TOP_N]
    underrates = sorted(deltas, key=lambda d: d['delta'])[:TOP_N]

    return {
        'rated_and_enriched_count': len(deltas),
        'generosity_score': generosity_score,
        'overrates': overrates,
        'underrates': underrates,
    }


def _rating_by_genre_and_decade(rated) -> dict:
    # count__gte requires at least MIN_COUNT_FOR_AVERAGE rated films in that genre --
    # a genre you've only rated one film in gets left out rather than showing a
    # single-film "average".
    by_genre = list(
        rated.filter(movie__genres__isnull=False)
        .values('movie__genres__name')
        .annotate(avg=Avg('rating'), count=Count('id'))
        .filter(count__gte=MIN_COUNT_FOR_AVERAGE)
        .order_by('-avg')[:TOP_N]
    )
    for row in by_genre:
        row['label'] = row['movie__genres__name']
        row['avg'] = float(row['avg'])

    decade_ratings = defaultdict(list)
    for rating, year in rated.filter(movie__release_year__isnull=False).values_list('rating', 'movie__release_year'):
        decade_ratings[(year // 10) * 10].append(rating)

    by_decade = [
        {'label': f'{decade}s', 'avg': round(_avg_or_none(ratings), 1), 'count': len(ratings)}
        for decade, ratings in sorted(decade_ratings.items())
        if len(ratings) >= MIN_COUNT_FOR_AVERAGE
    ]

    return {'by_genre': by_genre, 'by_decade': by_decade}


def _release_year_range(diary, rated):
    """The full (min, max) release year span across both diary and rated films, so the
    two release-year charts (count / avg rating) share one continuous x-axis instead of
    each only showing the years it happens to have data for."""
    years = set(diary.filter(movie__release_year__isnull=False).values_list('movie__release_year', flat=True))
    years |= set(rated.filter(movie__release_year__isnull=False).values_list('movie__release_year', flat=True))
    return (min(years), max(years)) if years else None


def _release_year_distribution(diary, year_range) -> list:
    """Distinct-film count per release year -- deduped by (title, year) so a heavily
    rewatched film doesn't inflate its release year's bar. Uses diary (not RatingEntry)
    since this characterizes everything watched, rated or not. Every year in year_range
    is included (0 for years with no films) so the x-axis has no gaps."""
    if year_range is None:
        return []

    rows = diary.filter(movie__release_year__isnull=False).values('title', 'year', 'movie__release_year').distinct()
    counts = defaultdict(int)
    for row in rows:
        counts[row['movie__release_year']] += 1

    return [{'year': year, 'count': counts.get(year, 0)} for year in range(year_range[0], year_range[1] + 1)]


def _rating_by_release_year(rated, year_range) -> list:
    """Average rating per release year -- the same per-year granularity as
    _release_year_distribution, but averaged rating instead of count. Uses rated
    (RatingEntry) like the rest of the rating-averaging stats, not diary, so a
    rewatch can't skew a year's average. A year with no rated films gets avg=None
    (not 0 -- 0 would look like a real bottom rating) so Chart.js just leaves a gap.
    Deliberate exception to the global MIN_COUNT_FOR_AVERAGE rule: a release year with
    just 1 rated film still gets a real average here, since a year plausibly only ever
    has one entry and omitting it would blank out large stretches of the x-axis."""
    if year_range is None:
        return []

    year_ratings = defaultdict(list)
    for rating, year in rated.filter(movie__release_year__isnull=False).values_list('rating', 'movie__release_year'):
        year_ratings[year].append(rating)

    result = []
    for year in range(year_range[0], year_range[1] + 1):
        ratings = year_ratings.get(year)
        if ratings:
            # 2 decimal places here, not the usual 1 -- an explicit exception for this
            # stat (see dashboard.html's ratingAwareTooltip call for the matching UI side).
            result.append({'year': year, 'avg': round(float(sum(ratings) / len(ratings)), 2), 'count': len(ratings)})
        else:
            result.append({'year': year, 'avg': None, 'count': 0})
    return result


def _films_by_country(watched_movies) -> list:
    """Distinct-film count per production country, sourced from watched_movies (see
    _watched_movies) so a rewatch doesn't inflate a country's bar. A film with
    multiple production countries is counted once for each one it belongs to."""
    ranked = list(
        watched_movies.filter(countries__isnull=False)
        .values('countries__name')
        .annotate(count=Count('tmdb_id'))
        .order_by('-count')[:TOP_N]
    )
    return [
        {'label': COUNTRY_NAME_OVERRIDES.get(row['countries__name'], row['countries__name']), 'count': row['count']}
        for row in ranked
    ]


def _films_by_language(watched_movies) -> list:
    """Distinct-film count per original language, sourced from watched_movies."""
    ranked = list(
        watched_movies.exclude(original_language='')
        .values('original_language')
        .annotate(count=Count('tmdb_id'))
        .order_by('-count')[:TOP_N]
    )
    return [{'label': row['original_language'], 'count': row['count']} for row in ranked]


def _rating_by_country(rated) -> list:
    """Average rating per production country. Uses rated (RatingEntry), like the rest
    of the rating-averaging stats, so a rewatch can't skew a country's average. A film
    with multiple countries contributes to each one's average."""
    country_ratings = defaultdict(list)
    for rating, name in rated.filter(movie__countries__isnull=False).values_list('rating', 'movie__countries__name'):
        country_ratings[name].append(rating)

    ranked = sorted(
        (
            {'label': COUNTRY_NAME_OVERRIDES.get(name, name), 'avg': float(sum(r) / len(r)), 'count': len(r)}
            for name, r in country_ratings.items()
            if len(r) >= MIN_COUNT_FOR_AVERAGE
        ),
        key=lambda row: row['avg'],
        reverse=True,
    )
    return ranked[:TOP_N]


def _rating_by_language(rated) -> list:
    """Average rating per original language. Uses rated (RatingEntry) for the same
    reason as _rating_by_country."""
    language_ratings = defaultdict(list)
    rows = rated.filter(movie__isnull=False).exclude(movie__original_language='')
    for rating, name in rows.values_list('rating', 'movie__original_language'):
        language_ratings[name].append(rating)

    ranked = sorted(
        (
            {'label': name, 'avg': float(sum(r) / len(r)), 'count': len(r)}
            for name, r in language_ratings.items()
            if len(r) >= MIN_COUNT_FOR_AVERAGE
        ),
        key=lambda row: row['avg'],
        reverse=True,
    )
    return ranked[:TOP_N]


def _rewatch_leaderboard(diary) -> dict:
    # Grouped by (title, year), not letterboxd_uri -- a rewatch's diary row can get a
    # different boxd.it short link than the original watch, so uri isn't a safe
    # "same film" key here.
    most_rewatched_films = list(
        diary.values('title', 'year')
        .annotate(watch_count=Count('id'), poster_path=Min('movie__poster_path'))
        .filter(watch_count__gt=1)
        .order_by('-watch_count')[:TOP_N]
    )
    for row in most_rewatched_films:
        row['poster_url'] = _tmdb_image_url(row.pop('poster_path'), 'w185')

    most_rewatched_directors = list(
        diary.filter(rewatch=True, movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(count=Count('id'), profile_path=Min('movie__directors__profile_path'))
        .order_by('-count')[:TOP_N]
    )
    for row in most_rewatched_directors:
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')

    rewatch_qs = diary.filter(rewatch=True, rating__isnull=False)
    rewatch_avg = rewatch_qs.aggregate(avg=Avg('rating'))['avg'] if rewatch_qs.count() >= MIN_COUNT_FOR_AVERAGE else None

    first_watch_qs = diary.filter(rewatch=False, rating__isnull=False)
    first_watch_avg = (
        first_watch_qs.aggregate(avg=Avg('rating'))['avg']
        if first_watch_qs.count() >= MIN_COUNT_FOR_AVERAGE
        else None
    )

    return {
        'most_rewatched_films': most_rewatched_films,
        'most_rewatched_directors': most_rewatched_directors,
        'rewatch_avg_rating': rewatch_avg,
        'first_watch_avg_rating': first_watch_avg,
    }


def _tag_distribution(diary) -> list:
    """Most-used tags from diary.csv's Tags column (a comma-separated string per log
    entry). Counted per diary row, not deduped per film -- unlike genre/director/actor
    breakdowns, a tag is something you applied to a specific watch, so a rewatch
    tagged differently the second time round should count both applications."""
    tag_counts = defaultdict(int)
    for raw_tags in diary.exclude(tags='').values_list('tags', flat=True):
        for tag in raw_tags.split(','):
            tag = tag.strip()
            if tag:
                tag_counts[tag] += 1

    ranked = sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)[:TOP_N]
    return [{'label': tag, 'count': count} for tag, count in ranked]


def _viewing_calendar(diary) -> dict:
    busiest_months = list(
        diary.annotate(month=TruncMonth('watched_date'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('-count')[:1]
    )
    busiest_month = busiest_months[0] if busiest_months else None

    weekday_rows = list(
        diary.annotate(weekday=ExtractWeekDay('watched_date'))
        .values('weekday')
        .annotate(count=Count('id'))
        .order_by('weekday')
    )
    weekday_distribution = [
        {'label': WEEKDAY_NAMES[row['weekday']], 'count': row['count']} for row in weekday_rows
    ]

    dates = sorted(set(diary.values_list('watched_date', flat=True)))
    longest_streak, longest_gap = _streak_and_gap(dates)

    return {
        'busiest_month': busiest_month,
        'weekday_distribution': weekday_distribution,
        'longest_streak_days': longest_streak,
        'longest_gap_days': longest_gap,
    }


def _streak_and_gap(dates: list) -> tuple:
    """Given a sorted list of distinct watch dates, return (longest consecutive-day
    streak, longest gap between watches) in days. Pure Python since this isn't
    expressible as a single SQL aggregate."""
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


def _highlights(watched_movies, rated, most_rewatched_films) -> dict:
    """A handful of single-film superlatives -- longest/shortest runtime, oldest/
    newest release year, highest/lowest rated, most watched. Ties are broken
    arbitrarily (whichever the DB returns first); these are meant as a few fun
    exemplars, not a ranked list, so no MIN_COUNT_FOR_AVERAGE-style guard applies --
    even a single rated film has a legitimate "highest rated" (itself).
    Runtime/year come from watched_movies (Movie rows, TMDB's own title) since
    they're about every film watched, not just rated ones; rating extremes come from
    rated (RatingEntry, Letterboxd's own title) to match every other rating stat."""
    return {
        'longest_film': watched_movies.filter(runtime_minutes__isnull=False).order_by('-runtime_minutes').first(),
        'shortest_film': watched_movies.filter(runtime_minutes__isnull=False).order_by('runtime_minutes').first(),
        'newest_film': watched_movies.filter(release_year__isnull=False).order_by('-release_year').first(),
        'oldest_film': watched_movies.filter(release_year__isnull=False).order_by('release_year').first(),
        'highest_rated': rated.filter(movie__isnull=False).select_related('movie').order_by('-rating').first(),
        'lowest_rated': rated.filter(movie__isnull=False).select_related('movie').order_by('rating').first(),
        'most_watched_film': most_rewatched_films[0] if most_rewatched_films else None,
    }


def _favorite_people(rated, actor_rating_lists, actor_profile_paths) -> dict:
    """Your highest-rated directors/actors -- an average-rating ranking, distinct from
    top_directors/top_actors which rank by how many films you've watched from them,
    not how highly you rated them. Directors need 2+ rated films, actors need 4+ (see
    MIN_COUNT_FOR_FAVORITE_DIRECTOR/_ACTOR). actor_rating_lists/actor_profile_paths
    are built once in build_dashboard_context (already cameo-excluded) and shared
    with top_actors' own avg_rating column, so an actor's numbers agree across both
    views."""
    # A tie in avg rating is broken by count (most watched) -- and vice versa for
    # top_directors/top_actors' own count-then-avg_rating ordering above. The tie
    # check has to use the *displayed* (1dp) average, not the raw one -- two people
    # can both show "4.6" while their true averages are 4.625 vs 4.55, and sorting on
    # the untruncated value would separate them by a difference the user can't even
    # see, silently skipping the count tiebreak they're expecting.
    favorite_directors = list(
        rated.filter(movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(avg=Avg('rating'), count=Count('id'), profile_path=Min('movie__directors__profile_path'))
        .filter(count__gte=MIN_COUNT_FOR_FAVORITE_DIRECTOR)
    )
    for row in favorite_directors:
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')
    favorite_directors.sort(key=lambda r: (round(r['avg'], 1), r['count']), reverse=True)
    favorite_directors = favorite_directors[:TOP_N]

    favorite_actors = [
        {
            'person__name': name,
            'avg': sum(ratings) / len(ratings),
            'count': len(ratings),
            'profile_url': _tmdb_image_url(actor_profile_paths.get(name, ''), 'w185'),
        }
        for name, ratings in actor_rating_lists.items()
        if len(ratings) >= MIN_COUNT_FOR_FAVORITE_ACTOR
    ]
    favorite_actors.sort(key=lambda r: (round(r['avg'], 1), r['count']), reverse=True)
    favorite_actors = favorite_actors[:TOP_N]

    return {'favorite_directors': favorite_directors, 'favorite_actors': favorite_actors}


def _films_watched_total(import_session, diary, rated) -> int:
    """Total distinct films ever marked watched, logged or not. watched.csv is the
    authoritative superset (diary.csv only has films with a logged date); if it wasn't
    included in this export, fall back to the union of diary + ratings as a best effort.
    The fallback is keyed by (title, year), not letterboxd_uri -- see _film_map's
    docstring in stats/services/compare.py."""
    watched = exclude_tv_shows(WatchedEntry.objects.filter(import_session=import_session))
    watched_count = watched.values('title', 'year').distinct().count()
    if watched_count:
        return watched_count
    return len(set(diary.values_list('title', 'year')) | set(rated.values_list('title', 'year')))


def _favorite_films(import_session) -> list:
    """Resolves the (up to 4) favorite films from profile.csv against this session's
    other entry models to get a title/year and, where possible, a TMDB-enriched Movie
    for the poster. Checked in order of "most likely to be enriched" -- a favorite that
    was also rated or logged gets a poster; one that was only ever watched.csv'd falls
    back to title-only."""
    uris = import_session.favorite_letterboxd_uris or []
    if not uris:
        return []

    candidates = defaultdict(list)
    for model in (RatingEntry, DiaryEntry, LikedFilmEntry, WatchedEntry):
        rows = model.objects.filter(import_session=import_session, letterboxd_uri__in=uris).values(
            'letterboxd_uri', 'title', 'year', 'movie_id'
        )
        for row in rows:
            candidates[row['letterboxd_uri']].append(row)

    movie_ids = {row['movie_id'] for rows in candidates.values() for row in rows if row['movie_id']}
    movies = Movie.objects.in_bulk(movie_ids)

    favorites = []
    for uri in uris:
        rows = candidates.get(uri, [])
        best = next((r for r in rows if r['movie_id']), rows[0] if rows else None)
        favorites.append({
            'letterboxd_uri': uri,
            'title': best['title'] if best else None,
            'year': best['year'] if best else None,
            'movie': movies.get(best['movie_id']) if best and best['movie_id'] else None,
        })
    return favorites
