"""Shared filtering helpers used by both dashboard.py and compare.py."""

from django.db.models import Exists, OuterRef, Q

from tmdb.models import TitleYearLookup


def exclude_tv_shows(queryset):
    """Excludes rows whose (title, year) was confirmed as TV via a TMDB TV-search
    follow-up (see tmdb/services/enrichment.py's _resolve_and_cache) -- Letterboxd
    lets people log some TV content (limited series, specials) alongside films, and
    these stats are about films."""
    tv_match = TitleYearLookup.objects.filter(title=OuterRef('title'), year=OuterRef('year'), is_tv_show=True)
    return queryset.exclude(Exists(tv_match))


# A film with a confirmed runtime under this counts as a "short" for the
# include/exclude shorts toggle on both Director's Cut and Double Feature. Same
# threshold _watchlist_recommendations' own eligibility filter already uses --
# defined once here so both stay in sync rather than drifting independently.
SHORT_FILM_MAX_RUNTIME_MINUTES = 60


def exclude_short_entries(queryset):
    """For querysets of entries (RatingEntry/DiaryEntry/WatchlistEntry/etc.) that
    carry a `movie` FK -- excludes rows whose resolved movie has a *confirmed*
    runtime under SHORT_FILM_MAX_RUNTIME_MINUTES. An unresolved movie (movie is
    NULL) or an unknown runtime (movie__runtime_minutes is NULL) is kept rather
    than excluded -- unconfirmed isn't the same as confirmed-short, same
    reasoning the TV/short eligibility filters elsewhere in this codebase
    already use."""
    return queryset.filter(
        Q(movie__runtime_minutes__isnull=True) | Q(movie__runtime_minutes__gte=SHORT_FILM_MAX_RUNTIME_MINUTES)
    )


def exclude_short_movies(queryset):
    """Same idea as exclude_short_entries, but for a queryset of Movie rows
    directly (runtime_minutes, not movie__runtime_minutes) -- used wherever a
    Movie queryset is built without going through an entry's own movie FK (see
    _watched_movies)."""
    return queryset.filter(
        Q(runtime_minutes__isnull=True) | Q(runtime_minutes__gte=SHORT_FILM_MAX_RUNTIME_MINUTES)
    )
