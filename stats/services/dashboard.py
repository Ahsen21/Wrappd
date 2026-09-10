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

import hashlib
import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from itertools import combinations

from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.db.models.functions import ExtractWeekDay, ExtractYear, TruncMonth

from imports.models import DiaryEntry, LikedFilmEntry, RatingEntry, ReviewEntry, WatchedEntry, WatchlistEntry
from stats.services.filters import (
    SHORT_FILM_MAX_RUNTIME_MINUTES, exclude_short_entries, exclude_short_movies, exclude_tv_shows,
)
from tmdb.models import Credit, Movie

WEEKDAY_NAMES = {1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday', 5: 'Thursday', 6: 'Friday', 7: 'Saturday'}
# TMDB's production_countries gives full formal names -- shortened to their common
# abbreviations for chart labels, which get cramped in the narrow three-across layout.
COUNTRY_NAME_OVERRIDES = {'United States of America': 'USA', 'United Kingdom': 'UK'}
# How many flags render in the Countries/Languages explored insight tiles' flag
# cluster (see _countries_explored_insight/_languages_explored_insight) -- the
# top N most-watched countries/languages by film count, not every one they've
# ever seen a single film from. A cluster of 30+ tiny flags would be
# illegible, not more informative.
FLAG_CLUSTER_SIZE = 4
# Language name (as stored on Movie.original_language, see tmdb/services/
# enrichment.py's _resolve_language_name) -> a representative ISO 3166-1
# alpha-2 country code, for the Languages explored insight tile's flag
# cluster (see _languages_explored_insight). A language isn't a country, so
# every mapping here is an approximation (picking the single most
# internationally recognizable flag for that language, not "the" country it
# belongs to) -- good enough for a decorative cluster of flags, not a claim
# about where a film was actually made (that's what the Countries tile is
# for). Deliberately not exhaustive: a language missing here just doesn't
# contribute a flag to the cluster (see _flag_emoji's own "don't fabricate"
# fallback), rather than guessing at one.
LANGUAGE_FLAG_CODES = {
    'English': 'GB', 'French': 'FR', 'Spanish': 'ES', 'German': 'DE', 'Italian': 'IT',
    'Japanese': 'JP', 'Korean': 'KR', 'Mandarin': 'CN', 'Cantonese': 'HK', 'Hindi': 'IN',
    'Russian': 'RU', 'Portuguese': 'PT', 'Swedish': 'SE', 'Danish': 'DK', 'Norwegian': 'NO',
    'Dutch': 'NL', 'Arabic': 'SA', 'Turkish': 'TR', 'Polish': 'PL', 'Thai': 'TH',
    'Finnish': 'FI', 'Greek': 'GR', 'Hebrew': 'IL', 'Hungarian': 'HU', 'Czech': 'CZ',
    'Romanian': 'RO', 'Ukrainian': 'UA', 'Vietnamese': 'VN', 'Indonesian': 'ID',
    'Tagalog': 'PH', 'Icelandic': 'IS', 'Persian': 'IR', 'Punjabi': 'IN', 'Tamil': 'IN',
    'Bengali': 'BD', 'Serbian': 'RS', 'Croatian': 'HR', 'Bulgarian': 'BG', 'Slovak': 'SK',
}
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
# The insight grid's runtime/decade tiles (_raw_axis_deltas, feeding
# _rating_insights) need their own, stronger-than-MIN_COUNT_FOR_AVERAGE bars --
# every rated film falls into exactly one runtime bucket and one decade, unlike
# a director/genre/etc. that only some films share, so these two buckets fill up
# fast and a low bar would make them the least meaningful, not the most
# meaningful, tiles in the grid. Runtime's bar is the higher of the two: only 3
# buckets total (see _runtime_bucket) means each one soaks up roughly a third of
# a person's whole rated history, so it takes more evidence before a runtime
# preference is distinguishable from "that's just most of what I watch" --
# decade splits far more finely (one bucket per 10 years), so 10 is already a
# real pattern there.
MIN_COUNT_FOR_RUNTIME_INSIGHT = 20
MIN_COUNT_FOR_DECADE_INSIGHT = 10
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
# A two-person "collaboration" (director+actor or actor+actor) needs at least
# this many shared rated films to be a pattern, not a coincidence -- most pairs
# that appear together at all only do so in exactly one or two films, so this is
# still well below MIN_COUNT_FOR_FAVORITE_DIRECTOR/_ACTOR's higher single-person
# bar. Shared by both _favorite_pairing_insight (director+actor) and
# _favorite_actor_duo_insight (actor+actor) -- one pairing bar, not two, since
# the underlying question ("is this a real recurring pattern, not a fluke") is
# identical either way.
MIN_COUNT_FOR_PAIRING = 3
# A genre *pair* (see _favorite_genre_combo_insight) needs its own, higher bar
# than MIN_COUNT_FOR_PAIRING -- two specific people appearing together is
# genuinely rare, but two genres appearing together is not (most films carry
# 2-3 genres at once, so a common combo like Action+Adventure can rack up
# shared films fast without that meaning anything about taste). This needs to
# be well above what two co-occurring genres would rack up by pure genre-tagging
# frequency alone, so the pair that wins is really the person's favorite blend,
# not just TMDB's most-assigned genre pairing.
MIN_COUNT_FOR_GENRE_COMBO = 8
# A film's TMDB vote_count has to sit at or below this before it's genuinely
# obscure enough to call a "hidden gem" -- vote counts scale enormously by a
# film's prominence (a real blockbuster sits in the tens of thousands even at the
# "less popular" end), so without an absolute floor, someone whose most obscure
# favorite still has, say, 15,000 votes would get it mislabeled as hidden. See
# _hidden_gem_insight.
HIDDEN_GEM_MAX_VOTE_COUNT = 1000


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


def _flag_emoji(country_code: str) -> str:
    """Two-letter ISO 3166-1 alpha-2 code -> its Unicode flag emoji (e.g. 'US' ->
    the US flag), for the Countries/Languages explored insight tiles' flag cluster
    (see _countries_explored_insight/_languages_explored_insight). No image
    asset involved -- a flag emoji is just two "Regional Indicator Symbol"
    codepoints, each one a fixed offset from the plain ASCII letter, so pairing
    two of them is all Unicode itself requires to render a flag; every
    reasonably current platform already has the glyphs.

    Returns '' for anything that isn't exactly two ASCII letters, rather than
    emitting mojibake -- Country.code is normally a clean ISO code, but this
    stays defensive since not every codebase-wide "country" is guaranteed to
    resolve to one (and a caller might pass an unmapped LANGUAGE_FLAG_CODES
    lookup's default '' straight through)."""
    code = country_code.upper()
    if len(code) != 2 or not code.isalpha():
        return ''
    return ''.join(chr(0x1F1E6 + ord(letter) - ord('A')) for letter in code)


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


def _watched_movies(import_session, diary, rated, exclude_shorts=False):
    """Movie objects for every distinct film actually watched, logged or not -- the
    same 'watched.csv is authoritative, else union of diary+ratings' pattern as
    _films_watched_total (see its docstring). This is the source for every 'most
    watched' breakdown (genre/director/actor/country/language) so a rewatch doesn't
    inflate a count and a watched.csv-only film (never diary-logged) still counts.

    exclude_shorts filters the FINAL Movie queryset (exclude_short_movies), not
    `watched` itself, so it applies uniformly regardless of which of the two paths
    above actually produced movie_ids -- diary/rated are already shorts-filtered
    by the caller when this flag is set, but watched.csv's own WatchedEntry rows,
    fetched fresh here, aren't filtered until this final step."""
    watched = exclude_tv_shows(WatchedEntry.objects.filter(import_session=import_session))
    movie_ids = set(watched.exclude(movie__isnull=True).values_list('movie_id', flat=True))
    if not movie_ids:
        movie_ids = set(diary.exclude(movie__isnull=True).values_list('movie_id', flat=True))
        movie_ids |= set(rated.exclude(movie__isnull=True).values_list('movie_id', flat=True))
    movies = Movie.objects.filter(tmdb_id__in=movie_ids)
    return exclude_short_movies(movies) if exclude_shorts else movies


def build_dashboard_context(import_session, exclude_shorts=False) -> dict:
    # _maybe_exclude_shorts is a no-op when the toggle is off -- every call site
    # below stays identical either way, rather than an `if exclude_shorts: ...`
    # branch at each one.
    _maybe_exclude_shorts = exclude_short_entries if exclude_shorts else (lambda queryset: queryset)

    diary_all = DiaryEntry.objects.filter(import_session=import_session)
    rated_all = RatingEntry.objects.filter(import_session=import_session)
    # TV-exclusion is tracked (excluded_tv_count/_titles below) against THIS
    # intermediate stage specifically, not the final shorts-filtered diary/rated --
    # otherwise a short film pulled out by the toggle would get miscounted as
    # "couldn't be matched to TMDB" in that banner, which is about TV content, not
    # runtime at all.
    diary_no_tv = exclude_tv_shows(diary_all)
    rated_no_tv = exclude_tv_shows(rated_all)
    diary = _maybe_exclude_shorts(diary_no_tv)
    rated = _maybe_exclude_shorts(rated_no_tv)
    watched_movies = _watched_movies(import_session, diary, rated, exclude_shorts)
    # Deduped by (title, year) like every other distinct-film count on this page -- a
    # review or like logged against a rewatch shouldn't count twice.
    reviews_count = (
        _maybe_exclude_shorts(exclude_tv_shows(ReviewEntry.objects.filter(import_session=import_session)))
        .exclude(review='').values('title', 'year').distinct().count()
    )
    likes_count = (
        _maybe_exclude_shorts(exclude_tv_shows(LikedFilmEntry.objects.filter(import_session=import_session)))
        .values('title', 'year').distinct().count()
    )
    watchlist = _maybe_exclude_shorts(exclude_tv_shows(WatchlistEntry.objects.filter(import_session=import_session)))
    watchlist_count = watchlist.values('title', 'year').distinct().count()
    # Distinct (title, year) pairs excluded from either source -- a title can be TV-
    # flagged and present in only one of diary/ratings (e.g. rated but never diary-
    # logged), so counting diary rows alone would undercount.
    excluded_pairs = set(diary_all.values_list('title', 'year')) - set(diary_no_tv.values_list('title', 'year'))
    excluded_pairs |= set(rated_all.values_list('title', 'year')) - set(rated_no_tv.values_list('title', 'year'))
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
    films_watched_total = _films_watched_total(import_session, diary, rated, exclude_shorts)

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
    axis_deltas = _all_axis_deltas(rated, avg_rating, actor_rating_lists, director_ratings, rated_count)
    recommendations = _watchlist_recommendations(
        watchlist, set(watched_movies.values_list('tmdb_id', flat=True)), avg_rating, axis_deltas,
        taste['generosity_score'], taste['rated_and_enriched_count'],
    )
    insights = _dashboard_insights(diary, rated, avg_rating, watched_movies, likes_count, films_watched_total)
    # First favorite with a resolved poster, used as the header banner's backdrop --
    # not necessarily favorites[0] itself, since an earlier favorite might not have
    # resolved to a Movie (and therefore have no poster) while a later one did.
    hero_poster_url = next((f['movie'].poster_url for f in favorites if f['movie'] and f['movie'].poster_url), '')

    return {
        'import_session': import_session,
        'exclude_shorts': exclude_shorts,
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
        'insights': insights,
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


def _all_axis_deltas(rated, avg_rating, actor_rating_lists, director_ratings, rated_count) -> dict:
    """{'genre': {...}, 'director': {...}, 'actor': {...}, 'country': {...},
    'language': {...}, 'decade': {...}, 'runtime': {...}} -- the confidence-shrunk,
    peak-blended, rarity-scaled per-key delta maps (see _rating_deltas) for all 7
    taste axes at once. Factored out from _watchlist_recommendations so
    _rating_insights can reuse the exact same numbers rather than recomputing a
    second, potentially-drifting copy -- an axis/key's delta can never disagree
    between the recommendation grid and the insights card.

    Returns {} if avg_rating is None (fewer than MIN_COUNT_FOR_AVERAGE rated films)
    -- there's no baseline to compute a delta against. avg_rating arrives here as a
    Decimal (RatingEntry.rating is a DecimalField) when not None; converted to float
    up front since float * Decimal raises TypeError in _shrunk_delta below (same fix
    _favorite_people applies)."""
    if avg_rating is None:
        return {}
    avg_rating = float(avg_rating)

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
    return {
        'genre': genre_deltas, 'director': director_deltas, 'actor': actor_deltas,
        'country': country_deltas, 'language': language_deltas, 'decade': decade_deltas,
        'runtime': runtime_deltas,
    }


def _watchlist_recommendations(
    watchlist, watched_movie_ids, avg_rating, axis_deltas, generosity_score, rated_and_enriched_count,
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
    a fabricated "recommendation" (axis_deltas is already {} in that case -- see
    _all_axis_deltas -- which the guard below catches). A candidate with no matching
    signal on any of the 7 taste axes (new genre, unknown director, unrecognized
    cast/country/language/decade/runtime) is skipped entirely too -- TMDB rating
    alone never qualifies a candidate on its own, it only nudges one that already
    matched something personal (see TMDB_WEIGHT's own comment for why)."""
    if not axis_deltas:
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

    genre_deltas = axis_deltas['genre']
    director_deltas = axis_deltas['director']
    actor_deltas = axis_deltas['actor']
    country_deltas = axis_deltas['country']
    language_deltas = axis_deltas['language']
    decade_deltas = axis_deltas['decade']
    runtime_deltas = axis_deltas['runtime']

    weights = _adaptive_weights(axis_deltas)

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
        .filter(Q(movie__runtime_minutes__isnull=True) | Q(movie__runtime_minutes__gte=SHORT_FILM_MAX_RUNTIME_MINUTES))
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

        # Named distinctly from the axis_deltas parameter above (this is per-candidate
        # matched deltas grouped by axis, not the person's whole taste profile).
        matched_deltas_by_axis = defaultdict(list)
        for axis, _, delta in components:
            matched_deltas_by_axis[axis].append(delta)
        taste_score = sum(
            weights[axis] * (sum(deltas) / len(deltas)) for axis, deltas in matched_deltas_by_axis.items()
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


def _raw_axis_deltas(rated, avg_rating) -> dict:
    """{'decade': {...}, 'runtime': {...}} -- plain, unadjusted average-rating
    deltas from avg_rating, for the 2 axes _rating_insights actually shows (see
    that function's own comment on why only those 2 -- director/actor used to be
    here too, before Favorite Directors/Actors' own cards made a third copy of
    that signal redundant). Deliberately NOT _all_axis_deltas' confidence-shrunk,
    peak-blended, rarity-scaled numbers -- those exist to make a *scoring*
    decision (_watchlist_recommendations) robust to thin evidence, at the cost of
    producing a number you can't sanity-check by eye. This card is meant to be
    checkable, so it reports the real average, not an adjusted one -- backed by
    MIN_COUNT_FOR_RUNTIME_INSIGHT/_DECADE_INSIGHT (see either constant's own
    comment for why they're stricter than the general MIN_COUNT_FOR_AVERAGE) so a
    thin sample still can't dominate a slot, it just isn't reshaped by shrinkage/
    peak-blend/rarity on top of that.

    Returns {} if avg_rating is None (fewer than MIN_COUNT_FOR_AVERAGE rated films
    -- no baseline to compute a delta against)."""
    if avg_rating is None:
        return {}
    avg_rating = float(avg_rating)

    decade_ratings = defaultdict(list)
    for rating, year in rated.filter(movie__release_year__isnull=False).values_list('rating', 'movie__release_year'):
        decade_ratings[_decade_bucket(year)].append(float(rating))
    decade_deltas = {
        decade: sum(ratings) / len(ratings) - avg_rating
        for decade, ratings in decade_ratings.items()
        if len(ratings) >= MIN_COUNT_FOR_DECADE_INSIGHT
    }

    runtime_ratings = defaultdict(list)
    for rating, minutes in rated.filter(movie__runtime_minutes__isnull=False).values_list(
        'rating', 'movie__runtime_minutes'
    ):
        runtime_ratings[_runtime_bucket(minutes)].append(float(rating))
    runtime_deltas = {
        bucket: sum(ratings) / len(ratings) - avg_rating
        for bucket, ratings in runtime_ratings.items()
        if len(ratings) >= MIN_COUNT_FOR_RUNTIME_INSIGHT
    }

    return {'decade': decade_deltas, 'runtime': runtime_deltas}


# Shared by every _rating_insights slot -- deliberately ONE plain format, not a
# per-axis sentence, since the combined insight grid's own tile label (see
# _INSIGHT_LABELS) already says what the tile is ("Favorite runtime", "Favorite
# decade"), so the body text just needs the value and the number, tight enough
# to read in a grid tile rather than a full-width row.
_DELTA_INSIGHT_TEXT = '{value} — {delta:+.1f}★ vs. avg'

# Only 2 of the 7 axes _all_axis_deltas/_raw_axis_deltas compute get a slot in the
# combined insight grid at all (see _AXIS_INSIGHT_SLOTS below) -- this grid exists
# to say something the rest of the dashboard doesn't, and every other axis already
# has a home of its own: genre/country/language have their own "Highest rated" tab
# (_rating_by_genre_and_decade/_country/_language), showing the exact same raw
# average this grid uses (see _raw_axis_deltas); director/actor have their own
# Favorite Directors/Actors cards. A tile repeating any of those would be a pure
# duplicate, not just a rephrase. Decade and runtime have no dashboard tab of their
# own at all, so both are unconditionally new information here.

# Icon shown next to a decade/runtime insight when no representative film's
# poster resolves (see _rating_insights/_best_film_per_bucket). Not meant to be
# precise iconography, just a quick visual anchor distinguishing one tile from
# the next.
_AXIS_ICONS = {'decade': '📅', 'runtime': '⏱️'}

# Small header title shown above each grid tile in _rating_insights. Only the
# favorable direction is ever shown for these axes (see _AXIS_INSIGHT_SLOTS), so
# there's a single label each -- no "Least favorite ..." variant.
_INSIGHT_LABELS = {'decade': 'Favorite decade', 'runtime': 'Favorite runtime'}

# The combined insight grid's second row (the other two -- duo, hidden gem --
# come from _favorite_pairing_insight/_hidden_gem_insight, computed separately
# -- see _dashboard_insights). 'positive' only: this grid celebrates what a
# person likes, so a runtime/decade they rate BELOW their own average never
# gets a tile even when that negative delta is the stronger one.
_AXIS_INSIGHT_SLOTS = [('runtime', 'positive'), ('decade', 'positive')]


def _strongest_axis_delta(deltas: dict, direction: str):
    """The single (value, delta) pair from a _rating_deltas-shaped dict that best
    matches `direction` ('positive', 'negative', or 'either' for whichever of the
    two clears RECOMMENDATION_REASON_THRESHOLD with the larger |delta|), or None if
    nothing in that direction clears the threshold. Shared by _rating_insights'
    fixed grid slots so 'either' doesn't duplicate the positive/negative logic."""
    if not deltas:
        return None
    candidates = []
    if direction in ('positive', 'either'):
        value, delta = max(deltas.items(), key=lambda item: item[1])
        if delta >= RECOMMENDATION_REASON_THRESHOLD:
            candidates.append((value, delta))
    if direction in ('negative', 'either'):
        value, delta = min(deltas.items(), key=lambda item: item[1])
        if delta <= -RECOMMENDATION_REASON_THRESHOLD:
            candidates.append((value, delta))
    if not candidates:
        return None
    return max(candidates, key=lambda c: abs(c[1]))


def _stable_hash(value) -> int:
    """Deterministic hash of `value`, unlike Python's builtin hash() of a str/int
    -- that one is salted with a random seed (PYTHONHASHSEED) picked fresh per
    interpreter process, so the "same" input would hash differently after every
    server restart/redeploy. This hashes a fixed string encoding via md5
    instead, so the same input always produces the same output -- used for
    _best_film_per_bucket's 'stable_random' tiebreak, where the whole point is a
    pick that looks arbitrary but never changes."""
    return int(hashlib.md5(str(value).encode('utf-8')).hexdigest(), 16)


def _best_qualifying_pair(pair_ratings: dict, min_count: int, avg_rating: float):
    """The single best-rated pair from a {pair_key: [ratings]} dict, or None if
    nothing qualifies -- shared by every "two things that co-occur" insight in
    this file (_favorite_pairing_insight, _favorite_actor_duo_insight,
    _favorite_genre_combo_insight), which otherwise each reimplemented the
    exact same "enough evidence, then clearly above average" selection.

    A pair only qualifies with at least `min_count` shared rated films (a
    caller-supplied bar -- MIN_COUNT_FOR_PAIRING for person pairs,
    MIN_COUNT_FOR_GENRE_COMBO for genre pairs, since genre pairs occur far
    more often and need a stronger bar to mean anything -- see that
    constant's own comment). Among qualifying pairs, the highest average
    wins, ties broken toward whichever pair shares more films (same "more
    evidence wins a tie" convention _favorite_people already uses) -- and
    even the winner is only returned if its average clears avg_rating +
    RECOMMENDATION_REASON_THRESHOLD, same "don't fabricate a neutral
    insight" bar as every other insight in this file.

    Returns (best_pair, ratings, avg) or None."""
    qualifying = {pair: ratings for pair, ratings in pair_ratings.items() if len(ratings) >= min_count}
    if not qualifying:
        return None
    best_pair = max(
        qualifying, key=lambda pair: (sum(qualifying[pair]) / len(qualifying[pair]), len(qualifying[pair])),
    )
    ratings = qualifying[best_pair]
    avg = sum(ratings) / len(ratings)
    if avg - avg_rating < RECOMMENDATION_REASON_THRESHOLD:
        return None
    return best_pair, ratings, avg


def _duo_portrait(name_a: str, photo_a: str, name_b: str, photo_b: str) -> dict:
    """Builds the 'duo' key for a two-person insight tile's two-circle
    portrait -- shared by _favorite_pairing_insight and
    _favorite_actor_duo_insight, the file's two person-pair duo insights."""
    return {
        'a': {'name': name_a, 'image': _tmdb_image_url(photo_a, 'w185') or None},
        'b': {'name': name_b, 'image': _tmdb_image_url(photo_b, 'w185') or None},
    }


def _best_film_per_bucket(rated, field: str, bucket_fn, tie_break: str) -> dict:
    """{bucket: {'title', 'year', 'movie_id'}} for the single highest-rated film
    in each bucket a movie's `field` sorts into under `bucket_fn` (e.g.
    field='release_year', bucket_fn=_decade_bucket) -- feeds the poster shown
    for the runtime/decade insight tiles in _rating_insights, since those two
    axes describe a pattern across many films rather than one single subject
    the way a person would.

    Ties (equal rating) break according to `tie_break`:
      - 'runtime': toward the longer runtime -- a real, meaningful tiebreak for
        the runtime axis itself (its whole tile is about runtime, so a tie
        should favor more of it, not an arbitrary pick). A film with no runtime
        data can't win a tie on that basis, so it falls back to alphabetical
        title -- same fallback used when runtime also ties.
      - 'stable_random': toward whichever film hashes lower under _stable_hash
        -- looks arbitrary rather than systematically favoring, say, the
        earliest release or the alphabetically first title, but is 100%
        reproducible from one request to the next, so the same decade always
        shows the same poster instead of it flipping on every reload or
        deploy. Used for the decade axis, where neither runtime nor
        alphabetical order has any natural connection to "which film best
        represents this decade".

    Only considers rated films with a resolved movie and a non-null `field` --
    an unresolved film can't be bucketed or given a poster either way."""
    best = {}
    rows = rated.filter(movie__isnull=False, **{f'movie__{field}__isnull': False}).values_list(
        'rating', 'title', 'year', 'movie_id', f'movie__{field}', 'movie__runtime_minutes',
    )
    for rating, title, year, movie_id, field_value, runtime_minutes in rows:
        bucket = bucket_fn(field_value)
        current = best.get(bucket)
        candidate_hash = _stable_hash(movie_id)
        if tie_break == 'runtime':
            candidate_key = (rating, runtime_minutes if runtime_minutes is not None else -1)
            current_key = (
                (current['rating'], current['runtime_minutes'] if current['runtime_minutes'] is not None else -1)
                if current else None
            )
            better = (
                current is None or candidate_key > current_key
                or (candidate_key == current_key and title < current['title'])
            )
        else:
            better = (
                current is None or rating > current['rating']
                or (rating == current['rating'] and candidate_hash < current['hash'])
            )
        if better:
            best[bucket] = {
                'rating': rating, 'title': title, 'year': year, 'movie_id': movie_id,
                'runtime_minutes': runtime_minutes, 'hash': candidate_hash,
            }
    return best


def _rating_insights(axis_deltas: dict, slots: list, decade_best_films: dict = None, runtime_best_films: dict = None) -> list:
    """Plain-English, grid-tile-sized facts about what actually moves this
    person's ratings -- e.g. "1990s — +0.6★ vs. avg" -- built from
    _raw_axis_deltas' plain, unadjusted average-rating deltas (deliberately NOT
    _all_axis_deltas' confidence-shrunk/peak-blended/rarity-scaled numbers, which
    exist for _watchlist_recommendations' own scoring purposes -- see
    _raw_axis_deltas' own comment for why this grid wants the checkable raw
    number instead). Returns [] if axis_deltas is {} (fewer than
    MIN_COUNT_FOR_AVERAGE rated films -- no baseline to compute a delta against).

    `slots` is a list of (axis, direction) pairs -- currently always
    _AXIS_INSIGHT_SLOTS (runtime then decade, 'positive' only; see that
    constant's own comment for why the unfavorable direction is never shown).
    Each slot's axis has to clear RECOMMENDATION_REASON_THRESHOLD in the
    requested direction to produce a tile at all (same "don't fabricate a
    neutral insight" principle as the rest of this file) -- a shorter grid
    rather than a fabricated filler. Unlike an earlier version of this function,
    the result is NOT sorted by |delta| -- slot order is fixed by category
    (runtime, then decade), not by which axis happens to have the single
    strongest number this time, so the same kind of insight always lands in the
    same grid position from one visit to the next.

    decade_best_films/runtime_best_films (from _best_film_per_bucket) supply the
    representative poster for a decade/runtime tile -- neither axis has one
    single subject the way director/actor slots used to, so the poster shown is
    "your highest-rated film from this bucket", not "the" film for it. Posters
    resolved via one bulk Movie lookup rather than a query per tile."""
    best_films_by_axis = {'decade': decade_best_films or {}, 'runtime': runtime_best_films or {}}
    resolved = []
    for axis, direction in slots:
        best = _strongest_axis_delta(axis_deltas.get(axis, {}), direction)
        if best is None:
            continue
        value, delta = best
        film = best_films_by_axis.get(axis, {}).get(value)
        resolved.append((axis, value, delta, film))

    movie_ids = {r[3]['movie_id'] for r in resolved if r[3] and r[3].get('movie_id')}
    movies_by_id = Movie.objects.in_bulk(movie_ids)

    insights = []
    for axis, value, delta, film in resolved:
        insight = {
            'text': _DELTA_INSIGHT_TEXT.format(value=value, delta=delta),
            'label': _INSIGHT_LABELS[axis],
            'axis': axis,
            'delta': delta,
            'icon': _AXIS_ICONS[axis],
            'image': None,
            'image_title': None,
        }
        # Both tiles click through to every rated film in the bucket (see
        # _featured_card's 'drill' and build_insight_films). `value` is the
        # decade label ("1990s") or the runtime bucket label ("90-150 min");
        # build_insight_films turns either back into the matching filter.
        if axis in ('decade', 'runtime'):
            insight['drill'] = {'kind': axis, 'p1': value, 'p2': ''}
        if film:
            movie = movies_by_id.get(film['movie_id'])
            if movie and movie.poster_url:
                insight['image'] = movie.poster_url
                insight['image_title'] = f"{film['title']} ({film['year']})"
        insights.append(insight)
    return insights


def _favorite_pairing_insight(rated, avg_rating) -> list:
    """0 or 1 insight about this person's best-rated recurring director-actor
    collaboration -- e.g. "Denis Villeneuve + Timothée Chalamet — 4 films, 4.8★".
    One tile in the combined insight grid (see _dashboard_insights) -- a pairing
    isn't a per-axis delta the way director/actor/decade/runtime are (it's a
    two-person co-occurrence, a different shape of fact entirely), so it's
    computed separately from _rating_insights' slot machinery rather than folded
    into it.

    Raw average, not confidence-shrunk/peak-blended -- same "checkable against
    the real numbers" reasoning as _raw_axis_deltas. Only considers pairs sharing
    at least MIN_COUNT_FOR_PAIRING rated films -- fewer than that is a
    coincidence, not a collaboration pattern -- and only surfaces the single best
    one if its average clears avg_rating + RECOMMENDATION_REASON_THRESHOLD, same
    "don't fabricate a neutral insight" bar as every other insight in this file.
    Ties (equal average) break toward whichever pair shares more films, same
    "more evidence wins a tie" convention _favorite_people already uses.

    The returned dict's 'duo' key carries each person's own headshot (resolved
    from whichever film first paired them -- profile photos don't vary film to
    film, so any occurrence works) for the two-circle portrait the insight grid
    renders instead of a single poster/icon -- see _favorite_actor_duo_insight
    for the same shape, just director+actor here instead of actor+actor.

    Returns [] if avg_rating is None (fewer than MIN_COUNT_FOR_AVERAGE rated films
    -- no baseline to compare against)."""
    if avg_rating is None:
        return []
    avg_rating = float(avg_rating)

    rated = rated.exclude(movie__isnull=True)
    movie_ids = set(rated.values_list('movie_id', flat=True))
    # Cast, not just directors, needs its own pass -- computed once up front (same
    # _cameo_credit_ids reused elsewhere) rather than a query per film below.
    cameo_ids = _cameo_credit_ids(movie_ids)

    directors_by_movie = defaultdict(list)
    for entry in rated.select_related('movie').prefetch_related('movie__directors'):
        for director in entry.movie.directors.all():
            directors_by_movie[entry.movie_id].append((director.name, director.profile_path, director.tmdb_id))

    actors_by_movie = defaultdict(list)
    for movie_id, name, profile_path, person_id in (
        Credit.objects.filter(movie_id__in=movie_ids).exclude(id__in=cameo_ids)
        .values_list('movie_id', 'person__name', 'person__profile_path', 'person_id')
    ):
        actors_by_movie[movie_id].append((name, profile_path, person_id))

    pair_ratings = defaultdict(list)
    # First-seen (photos, tmdb ids) for each (director, actor) key -- neither
    # varies film to film, so which occurrence supplies them doesn't matter.
    # The ids feed the tile's click-through modal (see _featured_card's 'drill').
    pair_meta = {}
    for movie_id, rating in rated.values_list('movie_id', 'rating'):
        for director_name, director_photo, director_id in directors_by_movie.get(movie_id, []):
            for actor_name, actor_photo, actor_id in actors_by_movie.get(movie_id, []):
                key = (director_name, actor_name)
                pair_ratings[key].append(float(rating))
                pair_meta.setdefault(key, (director_photo, actor_photo, director_id, actor_id))

    result = _best_qualifying_pair(pair_ratings, MIN_COUNT_FOR_PAIRING, avg_rating)
    if result is None:
        return []
    best_pair, ratings, avg = result

    director_name, actor_name = best_pair
    director_photo, actor_photo, director_id, actor_id = pair_meta[best_pair]
    return [{
        'text': f'{director_name} + {actor_name} — {len(ratings)} films, {avg:.1f}★',
        'label': 'Favorite actor/director duo',
        'axis': 'pairing',
        'icon': '🤝',
        'image': None,
        'image_title': None,
        'duo': _duo_portrait(director_name, director_photo, actor_name, actor_photo),
        'drill': {'kind': 'pairing', 'p1': director_id, 'p2': actor_id},
    }]


def _favorite_actor_duo_insight(rated, avg_rating) -> list:
    """0 or 1 insight about this person's best-rated recurring on-screen actor
    pairing -- e.g. "Timothée Chalamet + Zendaya — 3 films, 4.8★". Same shape and
    reasoning as _favorite_pairing_insight (director+actor), just actor x actor
    instead -- an "on-screen chemistry" fact distinct from that one, which pairs a
    director with an actor rather than two actors with each other. One tile in
    the combined insight grid (see _dashboard_insights).

    Raw average, not confidence-shrunk/peak-blended -- same "checkable against
    the real numbers" reasoning as _raw_axis_deltas. Only considers pairs sharing
    at least MIN_COUNT_FOR_PAIRING rated films -- fewer than that is a
    coincidence, not a recurring pairing -- and only surfaces the single best one
    if its average clears avg_rating + RECOMMENDATION_REASON_THRESHOLD, same
    "don't fabricate a neutral insight" bar as every other insight in this file.
    Ties (equal average) break toward whichever pair shares more films, same
    "more evidence wins a tie" convention _favorite_people already uses.

    Pair key is a sorted tuple, not insertion order -- the same two actors can be
    credited in either order film to film (billing order varies), and an
    unsorted key would silently split one real pairing into two separate,
    under-counted entries.

    The returned dict's 'duo' key carries each actor's own headshot -- see
    _favorite_pairing_insight's own comment on this same shape.

    Returns [] if avg_rating is None (fewer than MIN_COUNT_FOR_AVERAGE rated
    films -- no baseline to compare against)."""
    if avg_rating is None:
        return []
    avg_rating = float(avg_rating)

    rated = rated.exclude(movie__isnull=True)
    movie_ids = set(rated.values_list('movie_id', flat=True))
    cameo_ids = _cameo_credit_ids(movie_ids)

    actors_by_movie = defaultdict(list)
    for movie_id, name, profile_path, person_id in (
        Credit.objects.filter(movie_id__in=movie_ids).exclude(id__in=cameo_ids)
        .values_list('movie_id', 'person__name', 'person__profile_path', 'person_id')
    ):
        actors_by_movie[movie_id].append((name, profile_path, person_id))

    pair_ratings = defaultdict(list)
    # First-seen (photo, tmdb id) per actor name -- see _favorite_pairing_insight's
    # own pair_meta comment. The ids feed the tile's click-through modal.
    pair_meta = {}
    for movie_id, rating in rated.values_list('movie_id', 'rating'):
        # dict.setdefault, not set() -- dedupes a movie's cast by name (a person
        # can't appear twice in the same combinations() pass) while keeping each
        # name's (photo, id) alongside it, then sorted() on the items still
        # orders by name first, same as the plain-name sort this replaced.
        unique_cast = {}
        for name, photo, person_id in actors_by_movie.get(movie_id, []):
            unique_cast.setdefault(name, (photo, person_id))
        for (actor_a, (photo_a, id_a)), (actor_b, (photo_b, id_b)) in combinations(sorted(unique_cast.items()), 2):
            key = (actor_a, actor_b)
            pair_ratings[key].append(float(rating))
            pair_meta.setdefault(key, (photo_a, photo_b, id_a, id_b))

    result = _best_qualifying_pair(pair_ratings, MIN_COUNT_FOR_PAIRING, avg_rating)
    if result is None:
        return []
    best_pair, ratings, avg = result

    actor_a, actor_b = best_pair
    photo_a, photo_b, id_a, id_b = pair_meta[best_pair]
    return [{
        'text': f'{actor_a} + {actor_b} — {len(ratings)} films, {avg:.1f}★',
        'label': 'Favorite actor duo',
        'axis': 'actor_pairing',
        'icon': '🎬',
        'image': None,
        'image_title': None,
        'duo': _duo_portrait(actor_a, photo_a, actor_b, photo_b),
        'drill': {'kind': 'actor_pairing', 'p1': id_a, 'p2': id_b},
    }]


def _favorite_genre_combo_insight(rated, avg_rating) -> list:
    """0 or 1 insight about this person's best-rated recurring genre pairing --
    e.g. "Sci-Fi + Comedy — 9 films, 4.7★". Same duo mechanic as
    _favorite_pairing_insight/_favorite_actor_duo_insight, just genre x genre
    instead of two people -- a "your favorite blend" fact distinct from the
    Genres chart (_rating_by_genre_and_decade), which only ever reports a
    single genre's own average, never how two genres perform together. One
    tile in the combined insight grid (see _dashboard_insights).

    Raw average, not confidence-shrunk/peak-blended -- same "checkable against
    the real numbers" reasoning as _raw_axis_deltas. Only considers pairs
    sharing at least MIN_COUNT_FOR_GENRE_COMBO rated films -- see that
    constant's own comment for why genre pairs need a stronger bar than
    MIN_COUNT_FOR_PAIRING's person-pair threshold -- and only surfaces the
    single best one if its average clears avg_rating + RECOMMENDATION_REASON_
    THRESHOLD, same "don't fabricate a neutral insight" bar as every other
    insight in this file. Ties (equal average) break toward whichever pair
    shares more films, same "more evidence wins a tie" convention
    _favorite_people already uses.

    Pair key is a sorted tuple, not genre-list order -- TMDB's own genre
    ordering for a movie isn't stable across titles, and an unsorted key would
    silently split one real pairing into two separate, under-counted entries.

    Unlike the person-pair duos (a fact about two specific people, shown as
    their two headshots), a genre combo isn't about two visualizable things --
    it's shown instead via a single representative film: the highest-rated
    film among the winning pair's own qualifying films, ties broken toward the
    lower _stable_hash of its movie id (same "looks arbitrary, never changes
    between requests" reasoning _best_film_per_bucket's own 'stable_random'
    tiebreak uses, and for the same reason -- there's no runtime-style natural
    secondary axis to break a genre-combo tie on).

    Returns [] if avg_rating is None (fewer than MIN_COUNT_FOR_AVERAGE rated
    films -- no baseline to compare against)."""
    if avg_rating is None:
        return []
    avg_rating = float(avg_rating)

    rated = rated.exclude(movie__isnull=True)
    genres_by_movie = defaultdict(list)
    movie_info = {}
    rows = rated.filter(movie__genres__isnull=False).values_list(
        'movie_id', 'movie__genres__name', 'title', 'year', 'movie__poster_path',
    )
    for movie_id, genre_name, title, year, poster_path in rows:
        genres_by_movie[movie_id].append(genre_name)
        movie_info[movie_id] = (title, year, poster_path)

    pair_ratings = defaultdict(list)
    # Every film that qualifies a pair, kept alongside its rating so the
    # winning pair's representative poster can be picked after the fact --
    # see the docstring above for why "highest-rated, stable-hash tiebreak"
    # rather than reusing _best_film_per_bucket directly (that function buckets
    # by a single field; here the "bucket" is a fixed pair decided only after
    # every candidate has already been seen).
    pair_films = defaultdict(list)
    for movie_id, rating in rated.values_list('movie_id', 'rating'):
        genres = genres_by_movie.get(movie_id, [])
        for genre_a, genre_b in combinations(sorted(set(genres)), 2):
            key = (genre_a, genre_b)
            pair_ratings[key].append(float(rating))
            pair_films[key].append((float(rating), movie_id))

    result = _best_qualifying_pair(pair_ratings, MIN_COUNT_FOR_GENRE_COMBO, avg_rating)
    if result is None:
        return []
    best_pair, ratings, avg = result

    best_film = None
    for film_rating, movie_id in pair_films[best_pair]:
        film_hash = _stable_hash(movie_id)
        if (
            best_film is None or film_rating > best_film['rating']
            or (film_rating == best_film['rating'] and film_hash < best_film['hash'])
        ):
            title, year, poster_path = movie_info[movie_id]
            best_film = {'rating': film_rating, 'title': title, 'year': year, 'poster_path': poster_path, 'hash': film_hash}
    image = _tmdb_image_url(best_film['poster_path'], 'w342')

    genre_a, genre_b = best_pair
    return [{
        'text': f'{genre_a} + {genre_b} — {len(ratings)} films, {avg:.1f}★',
        'label': 'Favorite genre combo',
        'axis': 'genre_combo',
        'icon': '🎨',
        'image': image or None,
        'image_title': f"{best_film['title']} ({best_film['year']})" if image else None,
        'drill': {'kind': 'genre_combo', 'p1': genre_a, 'p2': genre_b},
    }]


def _hidden_gem_insight(rated, avg_rating) -> list:
    """0 or 1 insight about this person's most obscure real favorite -- a film
    they rated well above their own average that almost nobody on TMDB has rated
    at all. e.g. "Perfect Blue — 5.0★, 1,900 TMDB votes". One tile in the combined
    insight grid (see _dashboard_insights) -- same reasoning as
    _favorite_pairing_insight: this isn't a per-axis delta, it's a single-film
    fact, a different shape entirely.

    "Obscure" is self-relative for WHICH film qualifies (the lowest vote_count
    among the person's own favorites -- what counts as obscure varies far too
    much by genre/era for one universal cutoff to make sense there), but still
    has to clear HIDDEN_GEM_MAX_VOTE_COUNT in absolute terms before it's reported
    at all -- otherwise even a mainstream hit that merely happens to be this
    person's *least* mainstream favorite would get mislabeled "hidden". A
    "favorite" here means a rating that clears avg_rating + RECOMMENDATION_
    REASON_THRESHOLD, same bar as every other insight in this file.

    Only considers films with a resolved vote_count -- Movie.vote_count is null
    for anything enriched before that field existed and not yet backfilled (see
    the backfill_vote_counts management command), so those are skipped rather
    than wrongly treated as "0 votes, maximally obscure."

    Returns [] if avg_rating is None (fewer than MIN_COUNT_FOR_AVERAGE rated films
    -- no baseline to compare against)."""
    if avg_rating is None:
        return []
    avg_rating = float(avg_rating)

    favorites = [
        entry for entry in rated.filter(movie__isnull=False, movie__vote_count__isnull=False)
        .select_related('movie')
        if float(entry.rating) - avg_rating >= RECOMMENDATION_REASON_THRESHOLD
    ]
    if not favorites:
        return []

    gem = min(favorites, key=lambda entry: entry.movie.vote_count)
    if gem.movie.vote_count > HIDDEN_GEM_MAX_VOTE_COUNT:
        return []

    return [{
        'text': f'{gem.movie.title} — {float(gem.rating):.1f}★, {gem.movie.vote_count:,} TMDB votes',
        'label': 'Hidden gem',
        'axis': 'hidden_gem',
        'icon': '💎',
        'image': gem.movie.poster_url or None,
        'image_title': f'{gem.movie.title} ({gem.year})' if gem.movie.poster_url else None,
    }]


def _countries_explored_insight(watched_movies) -> list:
    """0 or 1 insight: how many distinct production countries this person's
    watched films span -- e.g. "34 countries". A pure breadth/exploration
    number, distinct from _films_by_country's per-country breakdown (that's
    about which countries show up most, this is just "how many different ones
    have you set foot in at all"). One tile in the combined insight grid (see
    _dashboard_insights).

    Renders in the insight grid's compact "by the numbers" strip (see
    _dashboard_insights): a big count with a small row of real flag emoji
    under it for the top FLAG_CLUSTER_SIZE most-watched countries (by distinct
    film count) -- genuine data already on hand (Country.code, via
    _flag_emoji), not a new asset. A country whose code doesn't resolve to a
    flag just contributes none (see _flag_emoji's own defensiveness).

    Uses watched_movies (every distinct film watched, logged or not -- see
    _watched_movies), same base as _films_by_country, so a rewatch can't
    inflate the count and an unlogged-but-watched film still counts.

    No minimum-evidence gate, unlike most insights in this file -- a distinct
    count IS the evidence; there's no "thin sample" version of "how many
    countries have you seen films from" the way there is for an average.
    Returns [] only if there's no country data at all yet (nothing enriched, or
    nothing with a resolved country)."""
    with_country = watched_movies.filter(countries__isnull=False)
    count = with_country.values('countries').distinct().count()
    if not count:
        return []
    top_countries = (
        with_country.values('countries__code')
        .annotate(film_count=Count('tmdb_id', distinct=True))
        .order_by('-film_count')[:FLAG_CLUSTER_SIZE]
    )
    flags = [flag for row in top_countries if (flag := _flag_emoji(row['countries__code']))]
    return [{
        'label': 'Countries explored',
        'value': str(count),
        'flags': flags,
    }]


def _languages_explored_insight(watched_movies) -> list:
    """0 or 1 insight: how many distinct original languages this person's
    watched films span -- e.g. "12 languages". Same reasoning as
    _countries_explored_insight, just the language axis instead -- see that
    function's own comment for why there's no minimum-evidence gate here
    either. One tile in the combined insight grid (see _dashboard_insights).

    Same compact flag-cluster treatment as the Countries tile, via
    LANGUAGE_FLAG_CODES' approximate language -> country mapping -- see that
    constant's own comment on why it's necessarily approximate, and why an
    unmapped language just contributes no flag rather than a guess."""
    with_language = watched_movies.exclude(original_language='')
    count = with_language.values('original_language').distinct().count()
    if not count:
        return []
    top_languages = (
        with_language.values('original_language')
        .annotate(film_count=Count('tmdb_id', distinct=True))
        .order_by('-film_count')[:FLAG_CLUSTER_SIZE]
    )
    flags = [
        flag for row in top_languages
        if (flag := _flag_emoji(LANGUAGE_FLAG_CODES.get(row['original_language'], '')))
    ]
    return [{
        'label': 'Languages explored',
        'value': str(count),
        'flags': flags,
    }]


def _rewatch_drift_insights(diary) -> list:
    """Up to 2 insights about how a rewatched film's rating changed between the
    first time it was logged and the most recent -- the single biggest upgrade and
    the single biggest downgrade, each only included if it clears
    RECOMMENDATION_REASON_THRESHOLD. Grouped by (title, year), not movie_id --
    same reasoning as _rewatch_leaderboard: a rewatch's diary row can get a
    different boxd.it short link than the original watch, but title/year is a
    safe "same film" key either way, resolved or not. Only diary entries with a
    logged rating count -- not every one has one."""
    ratings_by_film = defaultdict(list)
    for title, year, watched_date, rating, movie_id in diary.filter(rating__isnull=False).values_list(
        'title', 'year', 'watched_date', 'rating', 'movie_id'
    ):
        ratings_by_film[(title, year)].append((watched_date, float(rating), movie_id))

    drifts = []
    for (title, year), entries in ratings_by_film.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda entry: entry[0])
        first_rating = entries[0][1]
        last_rating = entries[-1][1]
        drift = last_rating - first_rating
        if drift != 0:
            # Any entry in the group with a resolved movie works for the poster --
            # it's the same film either way, just whichever watch happened to match
            # a TMDB id (a rewatch's diary row isn't guaranteed to have one -- see
            # this function's own docstring on why title/year, not movie_id, is
            # the grouping key).
            movie_id = next((entry[2] for entry in entries if entry[2] is not None), None)
            drifts.append({
                'title': title, 'year': year, 'first': first_rating, 'last': last_rating,
                'drift': drift, 'movie_id': movie_id,
            })

    if not drifts:
        return []

    selected = []
    biggest_upgrade = max(drifts, key=lambda d: d['drift'])
    if biggest_upgrade['drift'] >= RECOMMENDATION_REASON_THRESHOLD:
        selected.append((biggest_upgrade, 'climbed'))
    # Can never be the same film as biggest_upgrade -- a single drift can't clear
    # both a positive and a negative threshold at once.
    biggest_downgrade = min(drifts, key=lambda d: d['drift'])
    if biggest_downgrade['drift'] <= -RECOMMENDATION_REASON_THRESHOLD:
        selected.append((biggest_downgrade, 'dropped'))
    if not selected:
        return []

    posters = Movie.objects.in_bulk(d['movie_id'] for d, _ in selected if d['movie_id'] is not None)
    # {verb: grid label} -- "climbed" is the increase tile, "dropped" the decrease
    # tile, matching _rewatch_drift_insights' own selected-tuple verbs above.
    labels = {'climbed': 'Biggest rewatch increase', 'dropped': 'Biggest rewatch decrease'}
    insights = []
    for d, verb in selected:
        movie = posters.get(d['movie_id'])
        has_poster = bool(movie and movie.poster_url)
        insights.append({
            'text': f"{d['title']}: {d['first']:.1f}★ → {d['last']:.1f}★",
            'label': labels[verb],
            'icon': '🔁',
            'image': movie.poster_url if has_poster else None,
            'image_title': f"{d['title']} ({d['year']})" if has_poster else None,
        })
    return insights


def _rewatch_shift_insight(diary) -> list:
    """0 or 1 insight: whether this person rates films higher or lower once
    they're rewatching them -- their average rating on rewatch log entries
    minus their average on first-watch entries, e.g. "0.3★ vs. first watch"
    with an up arrow.

    A pool comparison (every rated rewatch entry against every rated first-watch
    entry), not a per-film paired difference -- the plain-English claim is "your
    rewatch ratings run higher/lower than your first-watch ratings", which
    doesn't need a film to have been logged on both sides. Needs
    MIN_COUNT_FOR_AVERAGE rated entries in each pool.

    A gap under RECOMMENDATION_REASON_THRESHOLD either way isn't a real lean, so
    it collapses to a flat "0.0★ / barely changes" with no arrow rather than a
    misleading tiny number. Renders in the number bar (see _stat_card /
    dashboard.html): 'direction' drives the trending arrow and the accent
    colour, and is absent for the flat state."""
    first = diary.filter(rewatch=False, rating__isnull=False).aggregate(avg=Avg('rating'), n=Count('id'))
    rewatched = diary.filter(rewatch=True, rating__isnull=False).aggregate(avg=Avg('rating'), n=Count('id'))
    if (first['n'] or 0) < MIN_COUNT_FOR_AVERAGE or (rewatched['n'] or 0) < MIN_COUNT_FOR_AVERAGE:
        return []
    delta = float(rewatched['avg']) - float(first['avg'])
    if abs(delta) < RECOMMENDATION_REASON_THRESHOLD:
        return [{'label': 'Rewatch shift', 'value': '0.0★', 'note': 'barely changes'}]
    return [{
        'label': 'Rewatch shift',
        'value': f'{abs(delta):.1f}★',
        'note': 'vs. first watch',
        'direction': 'up' if delta > 0 else 'down',
    }]


def _like_percentage_insight(likes_count: int, films_watched_total: int) -> list:
    """0 or 1 insight: what share of every film watched got a Letterboxd heart
    -- e.g. "38% liked". Just likes_count / films_watched_total, the same two
    numbers already shown as their own hero stats up top, so the tile agrees
    with them exactly rather than deriving its own slightly different count.

    No minimum-evidence gate beyond needing at least one watched film -- it's a
    plain ratio of two counts, not an average, so there's no thin-sample
    version of it to guard against (see _countries_explored_insight for the
    same reasoning). Returns [] only if nothing's been watched yet.

    Renders in the number bar as a circular gauge filled to `pct` (see
    _dashboard_insights / dashboard.html's .ib-gauge)."""
    if not films_watched_total:
        return []
    pct = round(likes_count / films_watched_total * 100)
    return [{
        'label': 'Like percentage',
        'value': f'{pct}%',
        'pct': pct,
        'note': 'of films watched',
    }]


_STAT_SHORT_LABELS = {
    'Countries explored': 'Countries', 'Languages explored': 'Languages',
    'Like percentage': 'Like %',
}


def _featured_card(insight: dict) -> dict:
    """One entry in the insight section's zone 1 (see _dashboard_insights) --
    the insight's one-line text split into its value (names / genre pair /
    decade / film title) and the trailing stat line, plus its poster or
    two-headshot portrait.

    rpartition, not partition -- split on the LAST separator, since a film
    title can itself contain one ("Avatar: The Way of Water: 4.5★ → 5.0★" must
    split to "Avatar: The Way of Water" + "4.5★ → 5.0★", not "Avatar" + the
    rest)."""
    value, sep, rest = insight['text'].rpartition(' — ')
    if not sep:
        value, _, rest = insight['text'].rpartition(': ')
    return {
        'label': insight['label'],
        'value': value,
        'stat': rest.replace(', ', ' · '),
        'image': insight.get('image'),
        'duo': insight.get('duo'),
        # Present only for the duo / genre-combo / decade entries -- see
        # build_insight_films. dict of {kind, ...ids/names} the template drops
        # onto the entry as data-* attributes for the click-through modal.
        'drill': insight.get('drill'),
    }


def _stat_card(stat: dict) -> dict:
    """One cell in the insight section's zone 2 number bar -- a short label
    (see _STAT_SHORT_LABELS) and the figure itself, rendered one of four ways:
    the two "explored" counts as a plain number over a flag cluster, the like
    percentage as a circular gauge (`pct`), the rewatch shift as an accent
    figure with a trending arrow (`direction`), and its flat "barely changes"
    state as a muted figure -- all over a short note (`sub`)."""
    card = {
        'label': _STAT_SHORT_LABELS.get(stat['label'], stat['label']),
        'value': stat['value'],
        'pct': stat.get('pct'),
        'direction': stat.get('direction'),
    }
    if 'flags' in stat:
        more = int(stat['value']) - len(stat['flags'])
        card['flags'] = stat['flags']
        card['flags_more'] = f'+{more} more' if more > 0 else ''
    else:
        card['sub'] = stat.get('note', '')
    return card


def _dashboard_insights(diary, rated, avg_rating, watched_movies, likes_count, films_watched_total) -> dict:
    """The "Your rating insights" section, split into two visually distinct
    zones (Favorite Directors/Actors already cover the director/actor signal on
    their own cards, so this section doesn't repeat it):

    'featured' -- up to 8 borderless entries, for the facts that have a real
        image: favorite director-actor duo (_favorite_pairing_insight) and
        favorite actor duo (_favorite_actor_duo_insight), both two headshots;
        favorite genre combo (_favorite_genre_combo_insight), favorite runtime,
        favorite decade (_rating_insights with _AXIS_INSIGHT_SLOTS), and hidden
        gem (_hidden_gem_insight), each a representative film poster; biggest
        rewatch increase and biggest rewatch decrease (_rewatch_drift_insights),
        each the film whose rating moved most.
    'stats' -- up to 4 cells in a "by the numbers" bar, for the facts that are
        just a figure: countries explored (_countries_explored_insight),
        languages explored (_languages_explored_insight), rewatch shift
        (_rewatch_shift_insight), and like percentage (_like_percentage_insight).

    Each zone is a fixed order -- not sorted by magnitude -- so the same
    insight always lands in the same spot from one visit to the next; see each
    individual function's own docstring for how its piece is computed and
    gated. A slot with nothing that clears its own "worth reporting" bar is
    skipped entirely, not padded. The whole section is hidden only when BOTH
    zones come back empty."""
    raw_deltas = _raw_axis_deltas(rated, avg_rating)
    decade_best_films = _best_film_per_bucket(rated, 'release_year', _decade_bucket, tie_break='stable_random')
    runtime_best_films = _best_film_per_bucket(rated, 'runtime_minutes', _runtime_bucket, tie_break='runtime')
    featured = (
        _favorite_pairing_insight(rated, avg_rating)
        + _favorite_actor_duo_insight(rated, avg_rating)
        + _favorite_genre_combo_insight(rated, avg_rating)
        + _rating_insights(raw_deltas, _AXIS_INSIGHT_SLOTS, decade_best_films, runtime_best_films)
        + _hidden_gem_insight(rated, avg_rating)
        + _rewatch_drift_insights(diary)
    )
    stats = (
        _countries_explored_insight(watched_movies)
        + _languages_explored_insight(watched_movies)
        + _rewatch_shift_insight(diary)
        + _like_percentage_insight(likes_count, films_watched_total)
    )
    return {
        'featured': [_featured_card(insight) for insight in featured],
        'stats': [_stat_card(stat) for stat in stats],
    }


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


def _films_watched_total(import_session, diary, rated, exclude_shorts=False) -> int:
    """Total distinct films ever marked watched, logged or not. watched.csv is the
    authoritative superset (diary.csv only has films with a logged date); if it wasn't
    included in this export, fall back to the union of diary + ratings as a best effort.
    The fallback is keyed by (title, year), not letterboxd_uri -- see _film_map's
    docstring in stats/services/compare.py.

    exclude_shorts is applied to `watched` explicitly here (fetched fresh from
    WatchedEntry, same as _watched_movies' own primary path) -- diary/rated are
    already shorts-filtered by the caller before reaching this function, so the
    fallback branch needs no separate handling."""
    watched = exclude_tv_shows(WatchedEntry.objects.filter(import_session=import_session))
    if exclude_shorts:
        watched = exclude_short_entries(watched)
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
