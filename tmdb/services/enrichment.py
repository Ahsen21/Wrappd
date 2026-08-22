"""
Orchestrates matching an import's films to TMDB and caching the results globally.

The (title, year) -> Movie cache (TitleYearLookup) is always checked before any
TMDB call, and is shared across every import ever processed by this app -- so the
same film watched by many different users costs exactly one API call, ever.
"""

import logging
import re
import unicodedata

from django.conf import settings
from django.conf.locale import LANG_INFO
from django.db import transaction

from imports.models import DiaryEntry, LikedFilmEntry, RatingEntry, ReviewEntry, WatchedEntry, WatchlistEntry
from tmdb.models import Country, Credit, Genre, Movie, Person, TitleYearLookup
from tmdb.services.client import TMDBClientError, get_movie_details, search_movie, search_tv

logger = logging.getLogger(__name__)

ENTRY_MODELS = (DiaryEntry, RatingEntry, WatchlistEntry, LikedFilmEntry, WatchedEntry, ReviewEntry)


def enrich_import_session_fully(import_session, cap=None):
    """
    Runs enrich_import_session repeatedly until every (title, year) pair this import
    references either resolves or a round makes zero further progress (TMDB is
    unreachable, or a round only turns up titles TMDB genuinely has no record of).
    Used by the upload flow so a user is never sent to their dashboard with films
    sitting unresolved just because one capped round wasn't enough to get through
    them all -- in steady state this converges in a single round, since most films
    a new import references are already in the shared cache from someone else's
    prior import; only a title nobody's ever logged before needs a fresh TMDB call.
    """
    previous_remaining = None
    while True:
        # enrich_import_session runs unconditionally first, every round -- including
        # round 1. It's the only thing that actually copies an already-cached movie
        # onto this session's entries, so checking "is there unattempted work" before
        # ever calling it would skip that copy step entirely whenever the cache is
        # already warm (the exact bug this function exists to avoid: a session left
        # with every entry unenriched despite TMDB having answers for all of them).
        enrich_import_session(import_session, cap=cap)
        remaining = _unattempted_pair_count(import_session)
        if remaining == 0 or remaining == previous_remaining:
            return
        previous_remaining = remaining


def _unattempted_pair_count(import_session) -> int:
    """How many (title, year) pairs still need a TMDB lookup attempt -- NOT the same
    as 'movie is still null' on the entry rows, since a title TMDB genuinely has no
    record of keeps movie=None forever once a negative TitleYearLookup exists for it;
    that pair is done, not still pending, so it must not count as remaining work."""
    pairs = set()
    for model in ENTRY_MODELS:
        pairs.update(
            model.objects.filter(import_session=import_session, movie__isnull=True)
            .exclude(title='')
            .values_list('title', 'year')
        )
    if not pairs:
        return 0
    attempted = set(
        TitleYearLookup.objects.filter(title__in={p[0] for p in pairs}).values_list('title', 'year')
    )
    return len(pairs - attempted)


def enrich_import_session(import_session, cap=None):
    """
    Resolve TMDB matches for every unenriched (title, year) pair referenced by this
    import, up to `cap` *new* TMDB lookups (cache hits don't count against the cap).
    Safe to call again later to pick up where a capped run left off -- see
    enrich_import_session_fully, which does exactly that.
    """
    cap = settings.TMDB_ENRICHMENT_CAP if cap is None else cap

    pairs = set()
    for model in ENTRY_MODELS:
        pairs.update(
            model.objects.filter(import_session=import_session, movie__isnull=True)
            .values_list('title', 'year')
            .distinct()
        )

    lookups_used = 0
    for title, year in pairs:
        if not title:
            continue

        lookup = TitleYearLookup.objects.filter(title=title, year=year).first()
        if lookup is None:
            if lookups_used >= cap:
                continue  # cap reached this round; leave unresolved for a follow-up run
            lookup = _resolve_and_cache(title, year)
            lookups_used += 1

        if lookup is not None and lookup.movie_id is not None:
            _assign_movie_to_entries(import_session, title, year, lookup.movie_id)


def _assign_movie_to_entries(import_session, title, year, movie_id):
    for model in ENTRY_MODELS:
        model.objects.filter(
            import_session=import_session, title=title, year=year, movie__isnull=True
        ).update(movie_id=movie_id)


def _normalize_title(title: str) -> str:
    """Lowercases, strips accents, and drops everything but letters/digits, so 'Amélie'
    and 'Amelie' normalize the same way but two genuinely different titles don't."""
    decomposed = unicodedata.normalize('NFKD', title or '')
    ascii_only = decomposed.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]', '', ascii_only.lower())


def _is_real_match(query_title: str, result: dict, *title_fields: str) -> bool:
    """TMDB's search is fuzzy text search, not a lookup -- it will happily return a
    top result that only loosely relates to the query (confirmed for real: searching
    'Neon Genesis Evangelion' returned a promotional VHS clip reel titled 'Neon Genesis
    Evangelion: Genesis 0:0 - In the Beginning', and 'Twin Peaks: The Return' returned
    an unrelated documentary called 'Bad Binoculars'). Only trust a result whose title
    normalizes to *exactly* the query -- not a prefix/substring match, since the VHS
    case shows a bad result can still start with the real title."""
    normalized_query = _normalize_title(query_title)
    return any(_normalize_title(result.get(field, '')) == normalized_query for field in title_fields)


_SUBTITLE_SEPARATORS = (':', '-')
# Below this, a lone result is more likely an obscure promotional/alternate-cut
# item than the actual film -- confirmed for real: TMDB's only result for "Neon
# Genesis Evangelion" is the same VHS clip reel from the _is_real_match bug above,
# with vote_count=4, versus thousands for a real theatrical release like "Glass
# Onion". This is what keeps _is_subtitle_variant from reintroducing that bug.
MIN_VOTE_COUNT_FOR_SUBTITLE_MATCH = 50


_PART_SUFFIX_RE = re.compile(r'^(part|chapter|vol\.?|volume)\s+\S+$', re.IGNORECASE)


def _is_subtitle_variant(query_title: str, result_title: str) -> bool:
    """True if result_title looks like query_title with extra text tacked on that
    TMDB includes but Letterboxd's short title omits -- e.g. Letterboxd's 'Glass
    Onion' vs. TMDB's actual 'Glass Onion: A Knives Out Mystery', or 'Mission:
    Impossible - Dead Reckoning' vs. TMDB's '... Dead Reckoning Part One'.
    Deliberately narrower than a plain prefix check (see _is_real_match's docstring
    for why a bare prefix/substring match is dangerous): the query must be followed
    *immediately* by either a genuine subtitle separator character, or a narrow
    'Part/Chapter/Volume N' franchise suffix -- not by more of the same word or an
    unrelated continuation (e.g. this correctly rejects 'Batman' as a match for
    'Batman Begins', since 'Begins' is neither). Dash variants (-, en dash, em dash)
    are treated as equivalent, since TMDB and Letterboxd don't always agree on which
    one to use in the same title."""
    query_norm = ' '.join((query_title or '').replace('–', '-').replace('—', '-').split()).casefold()
    result_norm = ' '.join((result_title or '').replace('–', '-').replace('—', '-').split())
    if not query_norm or not result_norm.casefold().startswith(query_norm):
        return False
    rest = result_norm[len(query_norm):].lstrip()
    if not rest:
        return False
    return rest[0] in _SUBTITLE_SEPARATORS or bool(_PART_SUFFIX_RE.match(rest))


# Manual overrides for (title, year) pairs confirmed to have no findable match under
# any search phrasing, because TMDB only has the film filed under an unrelated AKA
# title -- not a subtitle/part variant (_is_subtitle_variant), a genuinely different
# title entirely. Nausicaa of the Valley of the Wind (1984) only turns up on TMDB as
# "Warriors of the Wind" (id 81), the heavily re-edited ~95min US release of the same
# film (the original is ~117min) -- so runtime/rating on this Movie reflect that cut,
# not the original, but genre/director are correct and it's far better than nothing.
KNOWN_TITLE_ALIASES = {
    ('Nausicaä of the Valley of the Wind', 1984): 81,
}


def _resolve_and_cache(title, year):
    """Returns the resolved TitleYearLookup, or None if TMDB couldn't be reached this
    round (rate limit, network error, etc.) -- a None here must NOT be cached as a
    negative match. Only a real TMDB response with zero results (or a result that
    doesn't actually match the title) means 'not found'."""
    override_tmdb_id = KNOWN_TITLE_ALIASES.get((title, year))
    if override_tmdb_id is not None:
        movie, created = Movie.objects.get_or_create(tmdb_id=override_tmdb_id, defaults={'title': title})
        if created:
            _populate_details(movie)
        lookup, _ = TitleYearLookup.objects.get_or_create(title=title, year=year, defaults={'movie': movie})
        return lookup

    try:
        results = search_movie(title, year)
    except TMDBClientError as exc:
        logger.info('Skipping %r (%r) this round -- TMDB unavailable: %s', title, year, exc)
        return None

    # TMDB's relevance ranking doesn't always put the exact title+year match first
    # (a numbered sequel can rank below an earlier installment), so scan every
    # candidate for an exact normalized-title match instead of trusting just the top
    # result.
    result = next((r for r in results if _is_real_match(title, r, 'title', 'original_title')), None)

    # No exact match, but if TMDB has exactly one candidate and it's clearly the
    # same film with a subtitle Letterboxd's short title omits (not just some
    # loosely-related result), trust it -- gated on vote_count so this can't
    # latch onto an obscure/promotional item the way a bare prefix check would.
    if result is None and len(results) == 1:
        candidate = results[0]
        if (
            candidate.get('vote_count', 0) >= MIN_VOTE_COUNT_FOR_SUBTITLE_MATCH
            and (
                _is_subtitle_variant(title, candidate.get('title', ''))
                or _is_subtitle_variant(title, candidate.get('original_title', ''))
            )
        ):
            logger.info(
                'Accepting subtitle-variant match for %r (%r): %r', title, year, candidate.get('title')
            )
            result = candidate

    if results and result is None:
        logger.info(
            'Rejecting weak movie match for %r (%r): got %r', title, year, results[0].get('title')
        )

    if not result:
        try:
            tv_results = search_tv(title, year)
        except TMDBClientError as exc:
            logger.info('Skipping %r (%r) this round -- TMDB TV search unavailable: %s', title, year, exc)
            return None

        tv_result = next((r for r in tv_results if _is_real_match(title, r, 'name', 'original_name')), None)
        return TitleYearLookup.objects.create(title=title, year=year, movie=None, is_tv_show=bool(tv_result))

    tmdb_id = result['id']
    movie, created = Movie.objects.get_or_create(
        tmdb_id=tmdb_id,
        defaults={
            'title': result.get('title', '') or title,
            'original_title': result.get('original_title', '') or '',
            'release_year': _year_from_release_date(result.get('release_date')),
            'poster_path': result.get('poster_path') or '',
            'overview': result.get('overview', '') or '',
            'tmdb_rating': result.get('vote_average'),
        },
    )
    if created:
        _populate_details(movie)

    lookup, _ = TitleYearLookup.objects.get_or_create(title=title, year=year, defaults={'movie': movie})
    return lookup


def _populate_details(movie):
    try:
        details = get_movie_details(movie.tmdb_id)
    except TMDBClientError:
        return  # keep the basic search-result fields; details can be backfilled later

    movie.runtime_minutes = details.get('runtime')
    movie.overview = details.get('overview', '') or movie.overview
    movie.tmdb_rating = details.get('vote_average')
    movie.original_title = details.get('original_title', '') or movie.original_title
    movie.release_year = _year_from_release_date(details.get('release_date')) or movie.release_year
    movie.original_language = _resolve_language_name(details)
    movie.save()

    genres = []
    for genre_data in details.get('genres', []):
        genre, _ = Genre.objects.get_or_create(tmdb_id=genre_data['id'], defaults={'name': genre_data['name']})
        genres.append(genre)
    movie.genres.set(genres)

    countries = []
    for country_data in details.get('production_countries', []):
        country, _ = Country.objects.get_or_create(
            code=country_data['iso_3166_1'], defaults={'name': country_data.get('name', '')}
        )
        countries.append(country)
    movie.countries.set(countries)

    credits = details.get('credits', {})
    _populate_directors(movie, credits.get('crew', []))
    _populate_cast(movie, credits.get('cast', []))


# TMDB codes with no entry (or no 'name') in Django's LANG_INFO -- 'cn'/'zh' are
# TMDB-specific quirks (Cantonese / Chinese) rather than gaps in LANG_INFO's ISO-639-1
# coverage, but either way they'd otherwise fall through to the bare code below.
_LANGUAGE_NAME_OVERRIDES = {
    'cn': 'Cantonese',
    'zh': 'Chinese',
}


def _language_name_from_code(code: str) -> str:
    """Canonical ISO 639-1 code -> English name, independent of any single movie's
    TMDB record. Used as the fallback for _resolve_language_name."""
    if code in _LANGUAGE_NAME_OVERRIDES:
        return _LANGUAGE_NAME_OVERRIDES[code]
    info = LANG_INFO.get(code)
    if info and info.get('name'):
        return info['name']
    return code


def _resolve_language_name(details) -> str:
    """original_language on its own is just an ISO 639-1 code (e.g. 'en') -- prefer
    the matching entry in spoken_languages for its human-readable name (it can carry
    a more specific/localized name than a generic code table), but spoken_languages
    doesn't always include the original language (seen for e.g. 'lv', 'fr', 'en' --
    TMDB simply omits it from that list on some records), so fall back to a canonical
    code table rather than ever surfacing the bare code."""
    code = details.get('original_language') or ''
    for lang in details.get('spoken_languages', []):
        if lang.get('iso_639_1') == code:
            return lang.get('english_name') or lang.get('name') or _language_name_from_code(code)
    return _language_name_from_code(code)


def _populate_directors(movie, crew):
    """Every crew member credited as 'Director' -- a film can have co-directors
    (e.g. TMDB lists both Joel and Ethan Coen on most Coen Brothers films), so this
    must capture all of them, not just the first one found in the crew list."""
    directors = []
    for director_data in crew:
        if director_data.get('job') != 'Director':
            continue
        director, _ = Person.objects.get_or_create(
            tmdb_id=director_data['id'],
            defaults={'name': director_data.get('name', ''), 'profile_path': director_data.get('profile_path') or ''},
        )
        directors.append(director)
    movie.directors.set(directors)


@transaction.atomic
def _populate_cast(movie, cast):
    """Every credited cast member, not just the top-billed few -- a role well outside
    the top 10 can still be a real, memorable one (confirmed for real: Willem Dafoe as
    Green Goblin is cast position 12 in 'Spider-Man 2' and 16 in 'Spider-Man 3', both
    past the old top-10 cutoff, which silently dropped those films from his 'most
    watched' count -- the same class of bug as the single-director FK before it became
    a many-to-many field)."""
    for entry in cast:
        person, _ = Person.objects.get_or_create(
            tmdb_id=entry['id'],
            defaults={'name': entry.get('name', ''), 'profile_path': entry.get('profile_path') or ''},
        )
        Credit.objects.get_or_create(
            movie=movie,
            person=person,
            defaults={'character_name': entry.get('character', '') or '', 'order': entry.get('order', 0)},
        )


def _year_from_release_date(release_date):
    if not release_date:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None
