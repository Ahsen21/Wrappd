"""Read-only aggregation for the single-import dashboard. Everything here is computed
on the fly via the ORM rather than cached -- simple and correct-by-construction, which
matters more than raw speed for a learning project at this scale.

Three data sources are used deliberately:
  - RatingEntry (one row per distinct film, authoritative rating) for anything computing
    an average rating per film -- using DiaryEntry here would double-count rewatches.
  - DiaryEntry (one row per watch event, has watched_date + rewatch) for anything about
    viewing activity over time.
  - WatchedEntry (one row per distinct film, logged or not) for "most watched" counts
    (genre/director/actor/country/language/release year) -- see _watched_movies.
"""

import math
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.db.models.functions import ExtractWeekDay, ExtractYear, TruncMonth

from imports.models import DiaryEntry, LikedFilmEntry, RatingEntry, ReviewEntry, WatchedEntry, WatchlistEntry
from stats.services.filters import exclude_tv_shows
from tmdb.models import Credit, Movie

WEEKDAY_NAMES = {1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday', 5: 'Thursday', 6: 'Friday', 7: 'Saturday'}
# TMDB's production_countries gives full formal names -- shortened to their common
# abbreviations for chart labels, which get cramped in the narrow three-across layout.
COUNTRY_NAME_OVERRIDES = {'United States of America': 'USA', 'United Kingdom': 'UK'}
TOP_N = 10
# Most rewatched films/directors both render as a fixed poster grid (see .favs--eight
# in base.css), not a table -- 2 rows of 8 (16) rather than TOP_N's 10, so the grid
# fills evenly instead of leaving a sparse partial last row.
REWATCH_GRID_DISPLAY_CAP = 16
# Biggest over-rates/under-rates render as a fixed poster grid (see .favs--eight in
# base.css), not a table -- 2 rows of 8 (16) rather than TOP_N's 10.
TASTE_GRID_DISPLAY_CAP = 16
# Favorite Directors/Actors render as a fixed poster grid (see .favs--six in
# base.css -- shared with Double Feature's own Favorite directors/actors grids, not
# .favs--eight's shape like Most rewatched/Taste vs. crowd above), not a table -- 12
# rather than TOP_N's 10. Kept as its own constant per this file's established "a
# grid's cap is about filling its shape evenly, not about 'top N' ranking"
# convention, even though it's not TOP_N-derived.
FAVORITE_PEOPLE_GRID_CAP = 12
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
# Separate from the two thresholds above on purpose -- this is the shrinkage strength
# for the "True score" toggle (see _true_score), not the minimum count to qualify as
# a favorite at all. Reusing MIN_COUNT_FOR_FAVORITE_* here (as an earlier version of
# this did) meant every candidate sat exactly halfway shrunk toward the overall
# average right at the qualifying threshold, which compressed the whole top-N range
# down to a narrow band (e.g. an 0.5-star spread of raw averages became a 0.14-star
# spread of true scores) -- tuned down independently so true_score has room to
# actually differentiate people instead of pulling everyone toward the same point.
TRUE_SCORE_SHRINKAGE_K = 3
# True score's tiebreaker: a small additive bonus for a high rate of 5-star ratings,
# so two people who land on the same true_score (or close to it) don't stay tied just
# because a straight average can't distinguish "consistently great" from "several
# perfect films mixed with weaker ones". Weighted by the same count/(count+k)
# confidence factor as the shrinkage above -- a 3-film director who happens to be
# 3-for-3 on five stars shouldn't get the same bonus as a 10-film director who's
# 9-for-10, even though the raw *rate* is similar, since the smaller sample is
# weaker evidence of a genuine pattern. Max possible bonus (full confidence, 100%
# five-star rate) is this weight itself -- kept small so it nudges close scores
# rather than overriding the primary avg-based ranking.
FIVE_STAR_BONUS_WEIGHT = 0.2
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

# Watchlist recommender: how much each signal counts toward a candidate film's score.
# Summed on top of the user's own overall average rating, not averaged together --
# averaging the signals would cap a film's score at roughly its single best signal,
# so a film matching several things you love could never score higher than one
# matching just one of them. Summing lets multiple favorite signals stack, and lets a
# single standout signal (e.g. a beloved director) carry a film with otherwise-neutral
# genre/cast, which a plain average can't express either. Sums to 1.0.
RECOMMENDATION_WEIGHTS = {
    'genre': 0.30,
    'director': 0.25,
    'actor': 0.15,
    'country': 0.10,
    'language': 0.05,
    'decade': 0.10,
    'runtime': 0.05,
}
# How much a film's TMDB community rating (adjusted by the person's own generosity
# score -- see _watchlist_recommendations) nudges its score, on top of the 7 taste-
# based signals above -- deliberately small and NOT part of RECOMMENDATION_WEIGHTS'
# own adaptive reweighting (_adaptive_weights only ever redistributes across those
# 7, this stays fixed). TMDB rating isn't a personal-taste axis the way genre/
# director are -- it's an external quality prior -- so it shouldn't be able to grow
# via the same "this varies a lot for you" adaptive logic as a real taste signal,
# and a candidate never qualifies on this signal alone (see the empty-components
# guard below) -- it only nudges a film that already matched something personal.
TMDB_WEIGHT = 0.05
# How much weight _adaptive_weights gives to each person's own variance-derived
# weights versus the fixed RECOMMENDATION_WEIGHTS above -- 0.5 means an even blend.
# Kept below 1.0 deliberately: a data-starved axis' variance is noisy, not a
# confident signal on its own, so the fixed weights act as a floor rather than
# being fully replaced. See _adaptive_weights.
ADAPTIVE_WEIGHT_BLEND = 0.5
# How much each category's confidence-shrunk *peak* rating (its single highest
# rating, not its average) contributes to that category's delta, blended alongside
# the existing average-based delta -- see _rating_deltas. A category's average can
# be mediocre or even negative while still containing a genuine outlier favorite
# (e.g. someone who's picky about Action generally but rates their favorite Action
# film a 5) -- averaging alone erases exactly that kind of favorite, since it
# treats "loved a couple, indifferent to the rest" the same as "consistently
# lukewarm" whenever the two happen to average out similarly. Kept below 1.0 so the
# average still dominates -- a peak from a category with only one or two ratings
# shouldn't swing the delta on its own merit; PEAK_BLEND's job is to let a *real*,
# confidence-backed peak (many ratings in this category, one of them clearly
# excellent) surface, not to chase every lucky single high rating.
PEAK_BLEND = 0.35
# Top-N cap for the "Recommended from your watchlist" grid -- .favs--eight's full
# 2-rows-of-8 shape (4x4 on mobile), same convention as Most rewatched films/
# Biggest over-/under-rates.
RECOMMENDATION_DISPLAY_CAP = 16
# A signal's delta has to clear this before it's worth naming as a "why" reason in
# the UI -- otherwise a barely-above-baseline genre would clutter the tooltip
# alongside a film's actually meaningful matches.
RECOMMENDATION_REASON_THRESHOLD = 0.15
# At most this many recommended picks can credit the same director/actor -- without
# it, one dominant favorite (a director whose whole filmography sits on the
# watchlist) could flood the grid, crowding out otherwise-strong picks driven by
# different signals entirely. See _watchlist_recommendations' greedy selection pass.
PERSON_CREDIT_CAP = 2


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
    watchlist = exclude_tv_shows(WatchlistEntry.objects.filter(import_session=import_session))
    watchlist_count = watchlist.values('title', 'year').distinct().count()
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
    # max_rating feeds _watchlist_recommendations' peak-blended director delta (see
    # PEAK_BLEND) -- not used by top_directors/favorite_people below, which only
    # ever read rating_count/avg_rating, so this is a free addition for them.
    director_ratings = {
        row['movie__directors__name']: row
        for row in rated.filter(movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(rating_count=Count('id'), avg_rating=Avg('rating'), max_rating=Max('rating'))
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
        .annotate(
            count=Count('tmdb_id'), profile_path=Min('directors__profile_path'),
            director_tmdb_id=Min('directors__tmdb_id'),
        )
    )
    for row in top_directors:
        stats = director_ratings.get(row['directors__name'])
        row['avg_rating'] = stats['avg_rating'] if stats and stats['rating_count'] >= MIN_COUNT_FOR_AVERAGE else None
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')
    top_directors.sort(key=lambda r: (r['count'], _rounded_or_unrated(r['avg_rating'])), reverse=True)
    top_directors = top_directors[:FAVORITE_PEOPLE_GRID_CAP]

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
    # Alongside profile_path, also track one tmdb_id per name -- same reason as
    # top_directors' director_tmdb_id below, so the template can link to a person
    # filmography view without a second name-based lookup.
    actor_tmdb_ids = {}
    for person_name, movie_id, profile_path, person_tmdb_id in (
        Credit.objects.filter(movie_id__in=rated_ratings_by_movie)
        .exclude(id__in=cameo_credit_ids)
        .values_list('person__name', 'movie_id', 'person__profile_path', 'person__tmdb_id')
    ):
        actor_rating_lists[person_name].append(rated_ratings_by_movie[movie_id])
        actor_profile_paths[person_name] = profile_path
        actor_tmdb_ids[person_name] = person_tmdb_id

    top_actors = list(
        Credit.objects.filter(movie__in=watched_movies)
        .exclude(id__in=cameo_credit_ids)
        .values('person__name')
        .annotate(
            count=Count('movie', distinct=True), profile_path=Min('person__profile_path'),
            actor_tmdb_id=Min('person__tmdb_id'),
        )
    )
    for row in top_actors:
        ratings = actor_rating_lists.get(row['person__name'], [])
        row['avg_rating'] = sum(ratings) / len(ratings) if len(ratings) >= MIN_COUNT_FOR_AVERAGE else None
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')
    top_actors.sort(key=lambda r: (r['count'], _rounded_or_unrated(r['avg_rating'])), reverse=True)
    top_actors = top_actors[:FAVORITE_PEOPLE_GRID_CAP]

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
    release_year_range = _release_year_range(watched_movies, rated)
    release_year_distribution = _release_year_distribution(watched_movies, release_year_range)
    rating_by_release_year = _rating_by_release_year(rated, release_year_range)
    country_distribution = _films_by_country(watched_movies)
    language_distribution = _films_by_language(watched_movies)
    rating_by_country = _rating_by_country(rated)
    rating_by_language = _rating_by_language(rated)
    rewatch = _rewatch_leaderboard(diary)
    calendar = _viewing_calendar(diary)
    favorite_people = _favorite_people(rated, actor_rating_lists, actor_profile_paths, actor_tmdb_ids, avg_rating)
    favorites = _favorite_films(import_session)
    top_tags = _tag_distribution(diary)
    highlights = _highlights(watched_movies, rated, rewatch['most_rewatched_films'])
    recommendations = _watchlist_recommendations(
        rated, watchlist, set(watched_movies.values_list('tmdb_id', flat=True)), avg_rating,
        actor_rating_lists, director_ratings, rated_count,
        taste['generosity_score'], taste['rated_and_enriched_count'],
    )
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
        'recommendations': recommendations,
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
            'heatmap': calendar['heatmap'],
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
            # w342, not w92 -- this renders as a full poster grid card now, not the
            # small inline .film-thumb it was originally sized for. TMDB's smaller
            # size tiers are more aggressively compressed at the source, so w92
            # would look visibly softer than w342 even scaled down to the same size.
            'poster_url': _tmdb_image_url(row['movie__poster_path'], 'w342'),
        })

    raw_avg = _avg_or_none([d['delta'] for d in deltas])
    generosity_score = round(raw_avg, 2) if raw_avg is not None else None
    overrates = sorted(deltas, key=lambda d: d['delta'], reverse=True)[:TASTE_GRID_DISPLAY_CAP]
    underrates = sorted(deltas, key=lambda d: d['delta'])[:TASTE_GRID_DISPLAY_CAP]

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


def _release_year_range(watched_movies, rated):
    """The full (min, max) release year span across both watched_movies and rated
    films, so the two release-year charts (count / avg rating) share one continuous
    x-axis instead of each only showing the years it happens to have data for."""
    years = set(watched_movies.filter(release_year__isnull=False).values_list('release_year', flat=True))
    years |= set(rated.filter(movie__release_year__isnull=False).values_list('movie__release_year', flat=True))
    return (min(years), max(years)) if years else None


def _release_year_distribution(watched_movies, year_range) -> list:
    """Distinct-film count per release year, sourced from watched_movies (see
    _watched_movies) -- the same 'most watched' source as every other distribution
    breakdown (genre/director/actor/country/language), so a rewatch doesn't inflate a
    release year's bar and a watched.csv-only film (never diary-logged) still counts.
    Every year in year_range is included (0 for years with no films) so the x-axis
    has no gaps."""
    if year_range is None:
        return []

    counts = defaultdict(int)
    for release_year in watched_movies.filter(release_year__isnull=False).values_list('release_year', flat=True):
        counts[release_year] += 1

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


def _decade_bucket(year) -> str:
    return f'{(year // 10) * 10}s'


def _runtime_bucket(minutes) -> str:
    if minutes < 90:
        return 'Under 90 min'
    if minutes <= 150:
        return '90-150 min'
    return 'Over 150 min'


def _rarity_factor(count, total) -> float:
    """How much a signal value's rarity within the person's own rated history should
    scale its contribution to a recommendation score -- close to 1.0 for a value only
    a handful of rated films share (maximally distinguishing), fading toward 0 for a
    value shared by nearly every rated film (present everywhere, so it says little
    about *this* person's specific taste rather than being true of almost anything
    they'd rate). The same idea as TF-IDF's inverse-document-frequency: a common
    genre/decade carries less signal than a rare one, even backed by equal evidence
    -- confidence shrinkage (_true_score) already answers "how much should I trust
    this average", this answers the different question "how much does this average
    actually tell me about this person specifically, versus being true of almost
    everything they've rated". Smoothed (+1 both sides) so a value that's literally
    every rated film still returns exactly 0 (log(1) = 0), not a divide-by-zero, and
    that's exactly the right answer -- not merely discounted, but genuinely
    uninformative on its own since it can't distinguish this candidate from any other.

    (A cardinality-normalized version of this -- measuring each key's count against
    its own axis's average instead of the person's total rated-film count -- was
    tried and reverted: it was more theoretically correct for the genre-vs-director
    fairness gap it targeted, but produced worse real recommendations in practice,
    and a holdout validation showed it didn't move the actual ranking metric that
    mattered. Reverted rather than kept as a "more correct but worse" change.)"""
    return math.log((total + 1) / (count + 1)) / math.log(total + 1)


def _shrunk_delta(value, count, overall_avg_rating) -> float:
    """Confidence-shrunk delta of `value` from overall_avg_rating -- reuses
    _true_score's own evidence-weighted shrinkage (count/(count+k) confidence
    toward the overall average) rather than a hard MIN_COUNT_FOR_AVERAGE cutoff.
    `value` can be any single statistic about a group of `count` ratings (their
    average, their max, ...) -- the shrinkage math is the same either way, it's
    just asking "how much should I trust this number given how much evidence backs
    it," not "is this number itself an average."""
    return _true_score(value, count, TRUE_SCORE_SHRINKAGE_K, overall_avg_rating) - overall_avg_rating


def _rating_deltas(pairs, overall_avg_rating, total_count) -> dict:
    """Given an iterable of (key, rating) pairs, returns {key: confidence-shrunk
    delta from overall_avg_rating, scaled by that key's rarity} for every key seen.
    A key backed by just one or two ratings still contributes, just heavily
    discounted, rather than being excluded outright the way it would be on the
    dashboard's own display stats. total_count is the person's total rated-film
    count, the denominator _rarity_factor measures each key's share against.

    Each key's delta blends two shrunk statistics, not just the average (see
    PEAK_BLEND): the average rating in that key, and the single highest rating in
    it. A key's average can be mediocre while it still contains a genuine outlier
    favorite (picky about a genre generally, but rates their favorite entry a 5) --
    averaging alone erases exactly that favorite, since "loved a couple, indifferent
    to the rest" and "consistently lukewarm" can land on the same average. Both
    statistics get the same confidence shrinkage, based on the key's real count --
    a peak backed by only one or two ratings is barely trusted either, same as a
    thin average would be."""
    ratings_by_key = defaultdict(list)
    for key, rating in pairs:
        ratings_by_key[key].append(rating)

    def _blended_delta(ratings):
        count = len(ratings)
        avg_delta = _shrunk_delta(sum(ratings) / count, count, overall_avg_rating)
        peak_delta = _shrunk_delta(max(ratings), count, overall_avg_rating)
        return (1 - PEAK_BLEND) * avg_delta + PEAK_BLEND * peak_delta

    return {
        key: _blended_delta(ratings) * _rarity_factor(len(ratings), total_count)
        for key, ratings in ratings_by_key.items()
    }


def _variance(values) -> float:
    """Population variance of an iterable of numbers, or 0 for fewer than 2 values
    -- variance needs at least 2 points to mean anything, and 0 is the right
    fallback for _adaptive_weights specifically, where 0 already means "this axis
    isn't informative"."""
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _adaptive_weights(axis_deltas: dict) -> dict:
    """Per-person axis weights, blended with the fixed RECOMMENDATION_WEIGHTS (see
    ADAPTIVE_WEIGHT_BLEND) rather than replacing them outright. Derived from how
    much each axis's own deltas vary for this person, as a proxy for how much that
    axis actually discriminates their taste -- an axis whose deltas all cluster near
    0 isn't telling us anything about what this person likes, while one that swings
    between strongly positive and negative clearly is. The fixed weights stay in the
    blend as a floor because variance from a handful of data points is noisy, not a
    confident signal on its own -- the same "don't let noise pass as signal"
    principle as _rarity_factor and _true_score's own shrinkage elsewhere in this
    file, just applied to entire axes instead of individual keys within one.

    axis_deltas is {'genre': {...}, 'director': {...}, ...} -- the same 7 per-axis
    delta maps _watchlist_recommendations already builds. Both RECOMMENDATION_WEIGHTS
    and the variance-derived weights sum to 1.0 on their own, so blending them at a
    fixed ratio does too, with no separate renormalization step needed."""
    variances = {axis: _variance(deltas.values()) for axis, deltas in axis_deltas.items()}
    total_variance = sum(variances.values())
    if total_variance == 0:
        # No axis shows any spread at all (e.g. someone who's rated everything the
        # same) -- nothing to adapt to, so the fixed weights stand untouched.
        return dict(RECOMMENDATION_WEIGHTS)
    return {
        axis: (
            (1 - ADAPTIVE_WEIGHT_BLEND) * RECOMMENDATION_WEIGHTS[axis]
            + ADAPTIVE_WEIGHT_BLEND * (variances[axis] / total_variance)
        )
        for axis in RECOMMENDATION_WEIGHTS
    }


def _watchlist_recommendations(
    rated, watchlist, watched_movie_ids, avg_rating, actor_rating_lists, director_ratings, rated_count,
    generosity_score, rated_and_enriched_count,
) -> list:
    """Scores every watchlist film the user hasn't already watched, hasn't released
    yet, or is under an hour long (see the candidates queryset below) against their
    own rating history across genre, director, actor, country, language, decade, and
    runtime, and returns the top RECOMMENDATION_DISPLAY_CAP as
    {'movie', 'score', 'reasons'} dicts, highest score first (subject to
    PERSON_CREDIT_CAP -- see the greedy selection pass at the end). See
    RECOMMENDATION_WEIGHTS' comment for why this sums confidence-weighted deltas
    across signals rather than averaging them (within one signal -- e.g. a film's
    several genres -- the deltas ARE averaged, since those describe the same kind
    of thing about one film rather than independent kinds of evidence). Each delta
    is also scaled by _rarity_factor -- a decade or runtime bucket shared by most of
    someone's rated films says little about their specific taste even if their
    average rating within it is reliable, so it shouldn't compete on equal footing
    with a rare, specific match like a favorite director. The per-axis weights
    themselves aren't the fixed RECOMMENDATION_WEIGHTS either -- see
    _adaptive_weights for how they're nudged per-person toward whichever axes
    actually vary for that person's own taste.

    On top of those 7 taste-based signals, a film's TMDB community rating nudges the
    score by a small, fixed TMDB_WEIGHT (see that constant) -- adjusted by the
    person's own generosity_score (their average delta from TMDB's crowd rating on
    films they HAVE rated, from _taste_vs_crowd) so a systematically harsher or more
    generous rater's own scale is accounted for, not the raw crowd number. Both
    generosity_score and this whole TMDB nudge are confidence-shrunk by
    rated_and_enriched_count -- a generosity_score from only a couple of TMDB-
    enriched rated films is noisy, so it's pulled toward 0 (assume crowd-aligned)
    the same way every other thin-evidence signal in this file is.

    Requires avg_rating (i.e. at least MIN_COUNT_FOR_AVERAGE rated films) -- there's
    no baseline to compute a delta against otherwise, so this returns [] rather than
    a fabricated "recommendation". A candidate with no matching signal on any of the
    7 taste axes (new genre, unknown director, unrecognized cast/country/language/
    decade/runtime) is skipped entirely too -- TMDB rating alone never qualifies a
    candidate on its own, it only nudges one that already matched something
    personal (see TMDB_WEIGHT's own comment for why)."""
    if avg_rating is None:
        return []
    # _true_score does float(avg) internally but not float(overall_avg_rating) --
    # avg_rating arrives here as a Decimal (RatingEntry.rating is a DecimalField),
    # and float * Decimal raises TypeError, so this has to happen before any of the
    # _true_score calls below (same fix _favorite_people already applies).
    avg_rating = float(avg_rating)

    # Confidence-shrunk toward 0 (crowd-aligned) the same way every other thin-
    # evidence signal here is -- generosity_score itself can be None (fewer than
    # MIN_COUNT_FOR_AVERAGE TMDB-enriched rated films), in which case there's
    # nothing to shrink and this just stays 0.
    if generosity_score is not None and rated_and_enriched_count:
        generosity_confidence = rated_and_enriched_count / (rated_and_enriched_count + TRUE_SCORE_SHRINKAGE_K)
        shrunk_generosity = generosity_confidence * generosity_score
    else:
        shrunk_generosity = 0.0

    genre_deltas = _rating_deltas(
        rated.filter(movie__genres__isnull=False).values_list('movie__genres__name', 'rating'), avg_rating,
        rated_count,
    )
    country_deltas = _rating_deltas(
        rated.filter(movie__countries__isnull=False).values_list('movie__countries__name', 'rating'), avg_rating,
        rated_count,
    )
    language_deltas = _rating_deltas(
        rated.exclude(movie__isnull=True).exclude(movie__original_language='')
        .values_list('movie__original_language', 'rating'),
        avg_rating, rated_count,
    )
    decade_deltas = _rating_deltas(
        (
            (_decade_bucket(year), rating)
            for rating, year in rated.filter(movie__release_year__isnull=False)
            .values_list('rating', 'movie__release_year')
        ),
        avg_rating, rated_count,
    )
    runtime_deltas = _rating_deltas(
        (
            (_runtime_bucket(minutes), rating)
            for rating, minutes in rated.filter(movie__runtime_minutes__isnull=False)
            .values_list('rating', 'movie__runtime_minutes')
        ),
        avg_rating, rated_count,
    )
    # Directors/actors reuse the same rating groupings already built in
    # build_dashboard_context for top_directors/top_actors/favorite_people
    # (director_ratings, actor_rating_lists) rather than rebuilding them -- that
    # avoids duplicating actor_rating_lists' cameo filtering here, and keeps an
    # actor/director's numbers from ever disagreeing between the two views.
    # Peak-blended (see PEAK_BLEND/_shrunk_delta) same as every other axis -- an
    # inconsistent director (one standout among otherwise-average films) shouldn't
    # be invisible here just because director_ratings' own avg_rating doesn't show
    # it; max_rating (added to that query above) is what makes the peak side of the
    # blend possible without a second query.
    director_deltas = {
        name: (
            (
                (1 - PEAK_BLEND) * _shrunk_delta(stats['avg_rating'], stats['rating_count'], avg_rating)
                + PEAK_BLEND * _shrunk_delta(stats['max_rating'], stats['rating_count'], avg_rating)
            )
            * _rarity_factor(stats['rating_count'], rated_count)
        )
        for name, stats in director_ratings.items()
    }
    actor_deltas = {
        name: (
            (
                (1 - PEAK_BLEND) * _shrunk_delta(sum(ratings) / len(ratings), len(ratings), avg_rating)
                + PEAK_BLEND * _shrunk_delta(max(ratings), len(ratings), avg_rating)
            )
            * _rarity_factor(len(ratings), rated_count)
        )
        for name, ratings in actor_rating_lists.items()
    }

    weights = _adaptive_weights({
        'genre': genre_deltas, 'director': director_deltas, 'actor': actor_deltas,
        'country': country_deltas, 'language': language_deltas, 'decade': decade_deltas,
        'runtime': runtime_deltas,
    })

    # Excludes unreleased films (a confirmed future release_year -- there's no exact
    # release_date stored, just the year TMDB gave it, so a same-year film that
    # hasn't actually come out yet can still slip through; this is the closest check
    # the data on hand allows) and shorts (a confirmed runtime under 60 minutes).
    # Both filters keep a NULL value rather than exclude it -- an unknown release
    # year/runtime hasn't been confirmed bad, so it shouldn't be penalized for
    # missing data the way a genuinely-future or genuinely-short film should be.
    candidates = list(
        watchlist.filter(movie__isnull=False)
        .exclude(movie_id__in=watched_movie_ids)
        .filter(Q(movie__release_year__isnull=True) | Q(movie__release_year__lte=date.today().year))
        .filter(Q(movie__runtime_minutes__isnull=True) | Q(movie__runtime_minutes__gte=60))
        .select_related('movie')
        .prefetch_related('movie__genres', 'movie__countries', 'movie__directors')
    )
    candidate_movie_ids = {entry.movie_id for entry in candidates}
    # Cast, not just genre/director, needs its own pass -- computed once for every
    # candidate up front (same _cameo_credit_ids reused elsewhere) rather than one
    # query per film in the loop below.
    candidate_cameo_ids = _cameo_credit_ids(candidate_movie_ids)
    actors_by_movie = defaultdict(list)
    for movie_id, person_name in (
        Credit.objects.filter(movie_id__in=candidate_movie_ids)
        .exclude(id__in=candidate_cameo_ids)
        .values_list('movie_id', 'person__name')
    ):
        actors_by_movie[movie_id].append(person_name)

    scored = []
    seen_movie_ids = set()
    for entry in candidates:
        movie = entry.movie
        if movie.tmdb_id in seen_movie_ids:
            continue
        seen_movie_ids.add(movie.tmdb_id)

        # (axis, label, delta) for every signal this film actually has -- axes
        # missing entirely (e.g. an unrecognized genre) just never appear here,
        # rather than contributing a fabricated neutral 0.
        components = []
        for genre in movie.genres.all():
            if genre.name in genre_deltas:
                components.append(('genre', genre.name, genre_deltas[genre.name]))
        for director in movie.directors.all():
            if director.name in director_deltas:
                components.append(('director', director.name, director_deltas[director.name]))
        for actor_name in actors_by_movie.get(movie.tmdb_id, []):
            if actor_name in actor_deltas:
                components.append(('actor', actor_name, actor_deltas[actor_name]))
        for country in movie.countries.all():
            if country.name in country_deltas:
                components.append(('country', country.name, country_deltas[country.name]))
        if movie.original_language in language_deltas:
            components.append(('language', movie.original_language, language_deltas[movie.original_language]))
        if movie.release_year:
            decade = _decade_bucket(movie.release_year)
            if decade in decade_deltas:
                components.append(('decade', decade, decade_deltas[decade]))
        if movie.runtime_minutes:
            bucket = _runtime_bucket(movie.runtime_minutes)
            if bucket in runtime_deltas:
                components.append(('runtime', bucket, runtime_deltas[bucket]))

        if not components:
            continue

        axis_deltas = defaultdict(list)
        for axis, _, delta in components:
            axis_deltas[axis].append(delta)
        taste_score = sum(
            weights[axis] * (sum(deltas) / len(deltas)) for axis, deltas in axis_deltas.items()
        )

        # TMDB_WEIGHT's own comment explains why this is fixed rather than part of
        # the adaptive weights above -- components is already non-empty by this
        # point (the guard above), so this only ever nudges a candidate that
        # already qualified on taste, never qualifies one on its own. A film with
        # no TMDB rating at all just uses the plain taste_score, unscaled -- it
        # shouldn't lose 5% of its score to a signal that simply isn't there.
        if movie.tmdb_rating is not None:
            crowd_rating = float(movie.tmdb_rating) / 2
            tmdb_delta = (crowd_rating + shrunk_generosity) - avg_rating
            score = avg_rating + (1 - TMDB_WEIGHT) * taste_score + TMDB_WEIGHT * tmdb_delta
        else:
            score = avg_rating + taste_score

        reasons = [
            label for _, label, delta in sorted(components, key=lambda c: c[2], reverse=True)
            if delta >= RECOMMENDATION_REASON_THRESHOLD
        ][:3]
        # Directors/actors this candidate is meaningfully credited to, for
        # PERSON_CREDIT_CAP below -- same RECOMMENDATION_REASON_THRESHOLD as
        # `reasons` (just not capped to the top 3), not every director/actor axis
        # component. actor_deltas has an entry for anyone who's ever appeared in even
        # one rated film (see _rating_deltas' docstring on why there's no hard count
        # cutoff) -- most of those are negligible, shrunk-near-zero deltas, and
        # counting every one of them toward the cap would let a handful of trivial
        # one-film overlaps exhaust a genuinely favorite actor's 2 real slots.
        people = {
            label for axis, label, delta in components
            if axis in ('director', 'actor') and delta >= RECOMMENDATION_REASON_THRESHOLD
        }

        scored.append({'movie': movie, 'score': score, 'reasons': reasons, 'people': people})

    scored.sort(key=lambda item: item['score'], reverse=True)

    # Greedy selection, highest score first, skipping any candidate that would push
    # a director/actor already credited PERSON_CREDIT_CAP times over that limit --
    # without this, one favorite director whose whole filmography sits on the
    # watchlist could dominate the grid, crowding out otherwise-strong picks driven
    # by entirely different signals. Draws from the full scored list, not just the
    # first RECOMMENDATION_DISPLAY_CAP, so a skipped slot gets backfilled by the
    # next-best candidate rather than just shrinking the grid.
    selected = []
    person_credit_counts = defaultdict(int)
    for item in scored:
        if any(person_credit_counts[name] >= PERSON_CREDIT_CAP for name in item['people']):
            continue
        for name in item['people']:
            person_credit_counts[name] += 1
        selected.append(item)
        if len(selected) == RECOMMENDATION_DISPLAY_CAP:
            break

    return selected


def _rewatch_leaderboard(diary) -> dict:
    # Grouped by (title, year), not letterboxd_uri -- a rewatch's diary row can get a
    # different boxd.it short link than the original watch, so uri isn't a safe
    # "same film" key here.
    most_rewatched_films = list(
        diary.values('title', 'year')
        .annotate(watch_count=Count('id'), poster_path=Min('movie__poster_path'))
        .filter(watch_count__gt=1)
        .order_by('-watch_count')[:REWATCH_GRID_DISPLAY_CAP]
    )
    for row in most_rewatched_films:
        # w342, not w185 -- this renders as a full poster card now (.favs--eight), not
        # the small inline .film-thumb it was originally sized for. TMDB's smaller
        # size tiers are more aggressively compressed at the source, so w185 still
        # looks visibly softer than w342 even scaled down to the same final size.
        row['poster_url'] = _tmdb_image_url(row.pop('poster_path'), 'w342')

    most_rewatched_directors = list(
        diary.filter(rewatch=True, movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(
            count=Count('id'), profile_path=Min('movie__directors__profile_path'),
            director_tmdb_id=Min('movie__directors__tmdb_id'),
        )
        .order_by('-count')[:REWATCH_GRID_DISPLAY_CAP]
    )
    for row in most_rewatched_directors:
        # w342, not w185 -- same reasoning as most_rewatched_films' poster_url above:
        # this now renders as a full .fav-card photo, not the small .person-thumb it
        # was originally sized for.
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w342')

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
        'heatmap': _viewing_heatmap(diary),
    }


def _viewing_heatmap(diary) -> dict:
    """Per-day watch counts bucketed by year, for the calendar-heatmap grid in the
    Viewing calendar card. The grid itself is laid out client-side (see
    dashboard.html's heatmap script) rather than computed here -- this just hands
    over {year: {'YYYY-MM-DD': count}} plus which years actually have data, so a
    year with zero entries never shows up as an empty toggle option."""
    rows = diary.values('watched_date').annotate(count=Count('id'))
    by_year = defaultdict(dict)
    for row in rows:
        watched_date = row['watched_date']
        by_year[watched_date.year][watched_date.isoformat()] = row['count']

    # Oldest -> newest, so the year toggle reads left-to-right chronologically --
    # default_year (the most recent) is taken from the end of this list rather than
    # the start, and is what the toggle opens on, not necessarily years[0].
    years = sorted(by_year.keys())
    return {
        'years': years,
        'default_year': years[-1] if years else None,
        'data': {str(year): counts for year, counts in by_year.items()},
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


def _true_score(avg, count, k, overall_avg_rating, five_star_count=0) -> float:
    """Bayesian-shrunk rating (the classic IMDB 'weighted rating' formula) -- blends
    a person's own average with the user's overall average rating, weighted by how
    much evidence (count) backs it up. A low count pulls the score toward the overall
    average (a neutral assumption) rather than crashing it toward zero the way a
    naive count/rating multiplication would; a high count leaves it close to the raw
    average, since the weighting shifts toward `count` as it grows relative to `k`.
    `k` is TRUE_SCORE_SHRINKAGE_K, not MIN_COUNT_FOR_FAVORITE_* -- see that constant's
    comment for why they're deliberately separate.

    Adds a small confidence-weighted bonus for a high rate of 5-star ratings on top
    -- see FIVE_STAR_BONUS_WEIGHT's comment for why it's weighted rather than a flat
    proportion."""
    confidence = count / (count + k)
    score = confidence * float(avg) + (k / (count + k)) * overall_avg_rating
    if count:
        score += confidence * (five_star_count / count) * FIVE_STAR_BONUS_WEIGHT
    return score


def _favorite_people(rated, actor_rating_lists, actor_profile_paths, actor_tmdb_ids, overall_avg_rating) -> dict:
    """Your highest-rated directors/actors -- an average-rating ranking, distinct from
    top_directors/top_actors which rank by how many films you've watched from them,
    not how highly you rated them. Directors need 2+ rated films, actors need 4+ (see
    MIN_COUNT_FOR_FAVORITE_DIRECTOR/_ACTOR). actor_rating_lists/actor_profile_paths
    are built once in build_dashboard_context (already cameo-excluded) and shared
    with top_actors' own avg_rating column, so an actor's numbers agree across both
    views.

    overall_avg_rating can in principle be None (fewer than MIN_COUNT_FOR_AVERAGE
    rated films total) -- practically unreachable here, since qualifying for
    favorite_directors/_actors at all requires at least MIN_COUNT_FOR_FAVORITE_*
    rated films from one person alone, which already exceeds MIN_COUNT_FOR_AVERAGE.
    Defensive 0.0 fallback documents that rather than risking a crash on it."""
    overall_avg_rating = float(overall_avg_rating) if overall_avg_rating is not None else 0.0

    # A tie in avg rating is broken by count (most watched) -- and vice versa for
    # top_directors/top_actors' own count-then-avg_rating ordering above. The tie
    # check has to use the *displayed* (1dp) average, not the raw one -- two people
    # can both show "4.6" while their true averages are 4.625 vs 4.55, and sorting on
    # the untruncated value would separate them by a difference the user can't even
    # see, silently skipping the count tiebreak they're expecting.
    favorite_directors_all = list(
        rated.filter(movie__directors__isnull=False)
        .values('movie__directors__name')
        .annotate(
            avg=Avg('rating'), count=Count('id'), profile_path=Min('movie__directors__profile_path'),
            five_star_count=Count('id', filter=Q(rating=Decimal('5.0'))),
            director_tmdb_id=Min('movie__directors__tmdb_id'),
        )
        .filter(count__gte=MIN_COUNT_FOR_FAVORITE_DIRECTOR)
    )
    for row in favorite_directors_all:
        row['profile_url'] = _tmdb_image_url(row.pop('profile_path'), 'w185')
        row['true_score'] = _true_score(
            row['avg'], row['count'], TRUE_SCORE_SHRINKAGE_K, overall_avg_rating, row['five_star_count']
        )
    favorite_directors = sorted(favorite_directors_all, key=lambda r: (round(r['avg'], 1), r['count']), reverse=True)
    favorite_directors = favorite_directors[:FAVORITE_PEOPLE_GRID_CAP]
    favorite_directors_by_true_score = sorted(favorite_directors_all, key=lambda r: r['true_score'], reverse=True)
    favorite_directors_by_true_score = favorite_directors_by_true_score[:FAVORITE_PEOPLE_GRID_CAP]

    favorite_actors_all = [
        {
            'person__name': name,
            'avg': sum(ratings) / len(ratings),
            'count': len(ratings),
            'five_star_count': sum(1 for r in ratings if r == Decimal('5.0')),
            'profile_url': _tmdb_image_url(actor_profile_paths.get(name, ''), 'w185'),
            'actor_tmdb_id': actor_tmdb_ids.get(name),
        }
        for name, ratings in actor_rating_lists.items()
        if len(ratings) >= MIN_COUNT_FOR_FAVORITE_ACTOR
    ]
    for row in favorite_actors_all:
        row['true_score'] = _true_score(
            row['avg'], row['count'], TRUE_SCORE_SHRINKAGE_K, overall_avg_rating, row['five_star_count']
        )
    favorite_actors = sorted(favorite_actors_all, key=lambda r: (round(r['avg'], 1), r['count']), reverse=True)
    favorite_actors = favorite_actors[:FAVORITE_PEOPLE_GRID_CAP]
    favorite_actors_by_true_score = sorted(favorite_actors_all, key=lambda r: r['true_score'], reverse=True)
    favorite_actors_by_true_score = favorite_actors_by_true_score[:FAVORITE_PEOPLE_GRID_CAP]

    return {
        'favorite_directors': favorite_directors,
        'favorite_actors': favorite_actors,
        'favorite_directors_by_true_score': favorite_directors_by_true_score,
        'favorite_actors_by_true_score': favorite_actors_by_true_score,
    }


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
