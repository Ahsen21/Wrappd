"""Fetch-on-click film list behind a clickable insight entry -- the shared
rated films for a director-actor or actor-actor pairing, every rated film with a
given genre pair, every rated film from a decade, or every rated film in a
runtime bucket. Same {films: [{title, year, poster_url, rating}]} shape as
build_person_filmography, so dashboard.html's one modal renders both.

Scoped to what one import session has RATED, not merely watched: the tiles these
back up -- Favorite actor/director duo, Favorite actor duo, Favorite genre
combo, Favorite decade and Favorite runtime -- are all computed from ratings.csv
(see _favorite_pairing_insight etc. in dashboard.py), so their "N films, X.X★"
numbers only count rated films and the modal has to match. Cameo appearances are
excluded the same way every other actor stat on the dashboard excludes them
(_cameo_credit_ids)."""

from django.db.models import Max

from imports.models import RatingEntry
from stats.services.dashboard import _cameo_credit_ids
from stats.services.filters import exclude_tv_shows
from tmdb.models import Credit, Movie

VALID_KINDS = ('pairing', 'actor_pairing', 'genre_combo', 'decade', 'runtime')

# Inverse of dashboard._runtime_bucket -- each label maps back to the
# runtime_minutes filter kwargs that define it. Kept in lockstep with that
# function's boundaries (< 90, 90-150 inclusive, > 150).
_RUNTIME_BUCKET_FILTERS = {
    'Under 90 min': {'movie__runtime_minutes__lt': 90},
    '90-150 min': {'movie__runtime_minutes__gte': 90, 'movie__runtime_minutes__lte': 150},
    'Over 150 min': {'movie__runtime_minutes__gt': 150},
}


def _non_cameo_movie_ids(person_id, among_movie_ids) -> set:
    """The subset of `among_movie_ids` in which `person_id` has a non-cameo
    credit -- same _cameo_credit_ids rule the dashboard's actor stats use."""
    cameo_ids = _cameo_credit_ids(among_movie_ids)
    return set(
        Credit.objects.filter(person_id=person_id, movie_id__in=among_movie_ids)
        .exclude(id__in=cameo_ids)
        .values_list('movie_id', flat=True)
    )


def build_insight_films(import_session, kind, p1, p2) -> dict:
    """Films behind a clickable insight tile. `kind` is one of VALID_KINDS;
    p1/p2 are the two tmdb ids (pairing / actor_pairing), the two genre names
    (genre_combo), or -- for decade / runtime -- a single bucket label ("1990s",
    "90-150 min") with an unused '' second slot."""
    rated = exclude_tv_shows(
        RatingEntry.objects.filter(import_session=import_session, movie__isnull=False)
    )
    rated_movie_ids = set(rated.values_list('movie_id', flat=True))

    if kind == 'pairing':
        director_movie_ids = set(
            rated.filter(movie__directors=int(p1)).values_list('movie_id', flat=True)
        )
        movie_ids = director_movie_ids & _non_cameo_movie_ids(int(p2), rated_movie_ids)
    elif kind == 'actor_pairing':
        movie_ids = _non_cameo_movie_ids(int(p1), rated_movie_ids) & _non_cameo_movie_ids(
            int(p2), rated_movie_ids
        )
    elif kind == 'genre_combo':
        # Two chained .filter()s, not one with both names -- each adds its own
        # join so the film must carry BOTH genres, not either.
        movie_ids = set(
            rated.filter(movie__genres__name=p1)
            .filter(movie__genres__name=p2)
            .values_list('movie_id', flat=True)
        )
    elif kind == 'decade':
        start = int(str(p1)[:4])
        movie_ids = set(
            rated.filter(movie__release_year__gte=start, movie__release_year__lt=start + 10)
            .values_list('movie_id', flat=True)
        )
    elif kind == 'runtime':
        try:
            bucket_filter = _RUNTIME_BUCKET_FILTERS[p1]
        except KeyError:
            raise ValueError(f'unknown runtime bucket: {p1!r}')
        movie_ids = set(
            rated.filter(movie__runtime_minutes__isnull=False, **bucket_filter)
            .values_list('movie_id', flat=True)
        )
    else:
        raise ValueError(f'unknown insight-films kind: {kind!r}')

    if not movie_ids:
        return {'films': []}

    # Max(), not one-row-per-movie -- a theatrical cut and a director's cut can
    # log as two rows resolving to the same tmdb movie; take the higher rating.
    rating_by_movie = dict(
        rated.filter(movie_id__in=movie_ids)
        .values('movie_id')
        .annotate(r=Max('rating'))
        .values_list('movie_id', 'r')
    )
    films = [
        {
            'title': movie.title,
            'year': movie.release_year,
            'poster_url': movie.poster_url,
            'rating': str(rating_by_movie[movie.tmdb_id]),
        }
        for movie in Movie.objects.filter(tmdb_id__in=movie_ids)
    ]
    # Highest rated first, title as the tiebreak -- same order as
    # build_person_filmography.
    films.sort(key=lambda f: (-float(f['rating']), f['title']))
    return {'films': films}
