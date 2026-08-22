"""Shared filtering helpers used by both dashboard.py and compare.py."""

from django.db.models import Exists, OuterRef

from tmdb.models import TitleYearLookup


def exclude_tv_shows(queryset):
    """Excludes rows whose (title, year) was confirmed as TV via a TMDB TV-search
    follow-up (see tmdb/services/enrichment.py's _resolve_and_cache) -- Letterboxd
    lets people log some TV content (limited series, specials) alongside films, and
    these stats are about films."""
    tv_match = TitleYearLookup.objects.filter(title=OuterRef('title'), year=OuterRef('year'), is_tv_show=True)
    return queryset.exclude(Exists(tv_match))
