"""Fetch-on-click filmography for a single director/actor, scoped to what one
import session has actually watched -- via watched.csv, diary.csv, or ratings.csv,
same "watched" definition _watched_movies already establishes for every "most
watched" breakdown on the dashboard (genre/director/actor/country/language). This
module is a same-page drill-down of dashboard.py's own tables, not an independently
evolving page like compare.py, so it imports dashboard.py's helpers directly rather
than re-implementing them a third time."""

from django.db.models import Max

from imports.models import DiaryEntry, RatingEntry
from stats.services.dashboard import _cameo_credit_ids, _watched_movies
from stats.services.filters import exclude_tv_shows
from tmdb.models import Credit


def build_person_filmography(import_session, person, role) -> dict:
    """Every film this session has watched that `person` directed (role='director')
    or acted in, non-cameo (role='actor') -- with whatever rating info this session
    has for it. A film only watched via watched.csv (no rating, no diary log) still
    appears, with rating=None, matching the same _watched_movies-based count already
    shown next to this person's name in Most Watched Directors/Actors -- excluding
    those here would silently show fewer films than that count implied."""
    diary = exclude_tv_shows(DiaryEntry.objects.filter(import_session=import_session))
    rated = exclude_tv_shows(RatingEntry.objects.filter(import_session=import_session))
    watched_movies = _watched_movies(import_session, diary, rated)

    if role == 'director':
        person_movies = watched_movies.filter(directors=person)
    else:
        # Same cameo exclusion as every other actor stat on this page (top_actors/
        # favorite_actors) -- a big-cast bit part shouldn't count as "you watched a
        # film with this actor" here either.
        movie_ids = set(Credit.objects.filter(person=person).values_list('movie_id', flat=True))
        cameo_ids = _cameo_credit_ids(movie_ids)
        non_cameo_ids = set(
            Credit.objects.filter(person=person, movie_id__in=movie_ids)
            .exclude(id__in=cameo_ids).values_list('movie_id', flat=True)
        )
        person_movies = watched_movies.filter(tmdb_id__in=non_cameo_ids)

    person_movie_ids = list(person_movies.values_list('tmdb_id', flat=True))
    # Max(), not an assumed one-row-per-movie -- two different logged titles/years
    # can resolve to the same TMDB movie (e.g. theatrical vs. director's cut logged
    # separately); Max picks the higher of the two rather than crashing on multiple
    # rows per movie_id.
    rating_by_movie = dict(
        rated.filter(movie_id__in=person_movie_ids).values('movie_id')
        .annotate(r=Max('rating')).values_list('movie_id', 'r')
    )
    diary_rating_by_movie = dict(
        diary.filter(movie_id__in=person_movie_ids).exclude(rating__isnull=True).values('movie_id')
        .annotate(r=Max('rating')).values_list('movie_id', 'r')
    )

    films = []
    for movie in person_movies:
        rating = rating_by_movie.get(movie.tmdb_id) or diary_rating_by_movie.get(movie.tmdb_id)
        films.append({
            'title': movie.title,
            'year': movie.release_year,
            'poster_url': movie.poster_url,
            'rating': str(rating) if rating is not None else None,
        })

    # Highest rated first, unrated last, title as the tiebreak.
    films.sort(key=lambda f: (f['rating'] is None, -float(f['rating'] or 0), f['title']))

    return {'person_name': person.name, 'role': role, 'films': films}
