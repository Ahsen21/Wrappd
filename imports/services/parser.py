"""
Pure parsing functions for Letterboxd export CSVs. Nothing here touches Django's
request/response cycle or the database (except persist_parsed_export, which does
the actual writes) -- that's what makes the parsing itself unit-testable against
plain file-like objects.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zipfile import ZipFile

from django.db import transaction

from imports.models import (
    DiaryEntry, LikedFilmEntry, ListEntry, RatingEntry, ReviewEntry, UserList, WatchedEntry, WatchlistEntry,
)


class ExportParseError(Exception):
    """Raised when a CSV inside the export is missing an expected column or is malformed."""


@dataclass
class ParsedExport:
    display_name: str = ''
    favorite_uris: list = field(default_factory=list)
    diary: list = field(default_factory=list)
    ratings: list = field(default_factory=list)
    watchlist: list = field(default_factory=list)
    liked_films: list = field(default_factory=list)
    watched: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    lists: list = field(default_factory=list)


def _require_columns(fieldnames, required, csv_name):
    missing = [col for col in required if col not in (fieldnames or [])]
    if missing:
        raise ExportParseError(f"{csv_name} is missing expected column(s): {', '.join(missing)}")


def _parse_year(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_rating(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_diary_csv(zf: ZipFile, name: str = 'diary.csv') -> list:
    required = ('Name', 'Year', 'Letterboxd URI', 'Watched Date')
    rows = []
    reader = csv.DictReader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline=''))
    _require_columns(reader.fieldnames, required, name)
    for raw in reader:
        watched_date = _parse_date(raw.get('Watched Date')) or _parse_date(raw.get('Date'))
        if watched_date is None:
            continue  # a diary row with no usable date can't be stored; skip rather than fail the whole import
        rows.append({
            'title': (raw.get('Name') or '').strip(),
            'year': _parse_year(raw.get('Year')),
            'letterboxd_uri': (raw.get('Letterboxd URI') or '').strip(),
            'watched_date': watched_date,
            'rating': _parse_rating(raw.get('Rating')),
            'rewatch': (raw.get('Rewatch') or '').strip().lower() == 'yes',
            'tags': (raw.get('Tags') or '').strip(),
        })
    return rows


def parse_ratings_csv(zf: ZipFile, name: str = 'ratings.csv') -> list:
    required = ('Name', 'Year', 'Letterboxd URI', 'Rating')
    rows = []
    reader = csv.DictReader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline=''))
    _require_columns(reader.fieldnames, required, name)
    for raw in reader:
        rating = _parse_rating(raw.get('Rating'))
        if rating is None:
            continue
        rows.append({
            'title': (raw.get('Name') or '').strip(),
            'year': _parse_year(raw.get('Year')),
            'letterboxd_uri': (raw.get('Letterboxd URI') or '').strip(),
            'rating': rating,
        })
    return rows


def parse_watchlist_csv(zf: ZipFile, name: str = 'watchlist.csv') -> list:
    required = ('Name', 'Year', 'Letterboxd URI')
    rows = []
    reader = csv.DictReader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline=''))
    _require_columns(reader.fieldnames, required, name)
    for raw in reader:
        rows.append({
            'title': (raw.get('Name') or '').strip(),
            'year': _parse_year(raw.get('Year')),
            'letterboxd_uri': (raw.get('Letterboxd URI') or '').strip(),
            'added_date': _parse_date(raw.get('Date')),
        })
    return rows


def parse_likes_films_csv(zf: ZipFile, name: str = 'likes/films.csv') -> list:
    required = ('Name', 'Year', 'Letterboxd URI')
    rows = []
    reader = csv.DictReader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline=''))
    _require_columns(reader.fieldnames, required, name)
    for raw in reader:
        rows.append({
            'title': (raw.get('Name') or '').strip(),
            'year': _parse_year(raw.get('Year')),
            'letterboxd_uri': (raw.get('Letterboxd URI') or '').strip(),
            'date_liked': _parse_date(raw.get('Date')),
        })
    return rows


def parse_reviews_csv(zf: ZipFile, name: str = 'reviews.csv') -> list:
    required = ('Name', 'Year', 'Letterboxd URI')
    rows = []
    reader = csv.DictReader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline=''))
    _require_columns(reader.fieldnames, required, name)
    for raw in reader:
        rows.append({
            'title': (raw.get('Name') or '').strip(),
            'year': _parse_year(raw.get('Year')),
            'letterboxd_uri': (raw.get('Letterboxd URI') or '').strip(),
            'watched_date': _parse_date(raw.get('Watched Date')) or _parse_date(raw.get('Date')),
            'rating': _parse_rating(raw.get('Rating')),
            'review': (raw.get('Review') or '').strip(),
        })
    return rows


def parse_watched_csv(zf: ZipFile, name: str = 'watched.csv') -> list:
    required = ('Name', 'Year', 'Letterboxd URI')
    rows = []
    reader = csv.DictReader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline=''))
    _require_columns(reader.fieldnames, required, name)
    for raw in reader:
        rows.append({
            'title': (raw.get('Name') or '').strip(),
            'year': _parse_year(raw.get('Year')),
            'letterboxd_uri': (raw.get('Letterboxd URI') or '').strip(),
        })
    return rows


def _parse_profile(zf: ZipFile) -> tuple:
    """Returns (display_name, favorite_letterboxd_uris) from profile.csv, or ('', [])
    if the file isn't present. "Favorite Films" is a single column holding a
    comma-separated list of up to 4 boxd.it URIs, e.g.
    "https://boxd.it/297o, https://boxd.it/eDGs"."""
    if 'profile.csv' not in zf.namelist():
        return '', []
    reader = csv.DictReader(io.TextIOWrapper(zf.open('profile.csv'), encoding='utf-8-sig', newline=''))
    for raw in reader:
        display_name = (raw.get('Username') or raw.get('Given Name') or '').strip()
        favorites_raw = raw.get('Favorite Films') or ''
        favorite_uris = [uri.strip() for uri in favorites_raw.split(',') if uri.strip()]
        return display_name, favorite_uris
    return '', []


def _parse_list_rows(rows):
    """One lists/*.csv file as {'name', 'tags', 'url', 'entries': [...]}, or None if it
    doesn't look like a list export. The layout is a metadata block (a 'Date,Name,
    Tags,URL,Description' header and one row of values), a blank row, then a
    'Position,Name,Year,URL,Description' header followed by one row per film. Found by
    those header rows rather than fixed line numbers, so an extra preamble line doesn't
    break it."""
    meta_header = film_header = None
    for index, row in enumerate(rows):
        first = (row[0] if row else '').strip().lower()
        if first == 'date' and meta_header is None:
            meta_header = index
        elif first == 'position' and meta_header is not None:
            film_header = index
            break
    if meta_header is None or film_header is None or meta_header + 1 >= len(rows):
        return None
    meta = dict(zip((h.strip().lower() for h in rows[meta_header]), rows[meta_header + 1]))
    columns = [h.strip().lower() for h in rows[film_header]]

    entries = []
    for row in rows[film_header + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        record = dict(zip(columns, row))
        title = (record.get('name') or '').strip()
        if not title:
            continue
        position_raw = (record.get('position') or '').strip()
        entries.append({
            'position': int(position_raw) if position_raw.isdigit() else len(entries) + 1,
            'title': title,
            'year': _parse_year(record.get('year')),
            'letterboxd_uri': (record.get('url') or '').strip(),
        })
    entries.sort(key=lambda entry: entry['position'])
    tags = [tag.strip().lower() for tag in (meta.get('tags') or '').split(',') if tag.strip()]
    return {
        'name': (meta.get('name') or '').strip(), 'tags': tags, 'url': (meta.get('url') or '').strip(),
        'entries': entries,
    }


def parse_lists(zf: ZipFile) -> list:
    """Every list in the export's lists/ folder. A file that can't be read or isn't in
    the expected layout is skipped rather than failing the whole import -- lists are
    only used for optional touches like the dashboard's hero image."""
    lists = []
    for name in sorted(zf.namelist()):
        if not (name.startswith('lists/') and name.lower().endswith('.csv')):
            continue
        try:
            rows = list(csv.reader(io.TextIOWrapper(zf.open(name), encoding='utf-8-sig', newline='')))
        except (UnicodeDecodeError, csv.Error):
            continue
        parsed = _parse_list_rows(rows)
        if parsed is not None:
            lists.append(parsed)
    return lists


def parse_export(zf: ZipFile) -> ParsedExport:
    """
    Parse the subset of a Letterboxd export zip that v1 supports: diary, ratings,
    watchlist, liked films, watched, reviews, lists, and profile (display name + favorites).
    Missing optional files (e.g. no watchlist) are treated as "no rows", not an error -- only
    a completely missing diary AND ratings file is a hard failure (checked earlier by
    validators.py).
    """
    names = set(zf.namelist())
    display_name, favorite_uris = _parse_profile(zf)
    parsed = ParsedExport(display_name=display_name, favorite_uris=favorite_uris)

    if 'diary.csv' in names:
        parsed.diary = parse_diary_csv(zf)
    if 'ratings.csv' in names:
        parsed.ratings = parse_ratings_csv(zf)
    if 'watchlist.csv' in names:
        parsed.watchlist = parse_watchlist_csv(zf)
    if 'likes/films.csv' in names:
        parsed.liked_films = parse_likes_films_csv(zf)
    if 'watched.csv' in names:
        parsed.watched = parse_watched_csv(zf)
    if 'reviews.csv' in names:
        parsed.reviews = parse_reviews_csv(zf)
    parsed.lists = parse_lists(zf)

    return parsed


@transaction.atomic
def persist_parsed_export(import_session, parsed: ParsedExport) -> None:
    """Bulk-create all parsed rows for an import session. Deduping (unique_together) is
    handled by the CSV format itself -- Letterboxd doesn't export duplicate rows -- so
    plain bulk_create is safe and fast here."""

    update_fields = []
    if parsed.display_name and not import_session.display_name:
        import_session.display_name = parsed.display_name
        update_fields.append('display_name')
    if parsed.favorite_uris and not import_session.favorite_letterboxd_uris:
        import_session.favorite_letterboxd_uris = parsed.favorite_uris
        update_fields.append('favorite_letterboxd_uris')
    if update_fields:
        import_session.save(update_fields=update_fields)

    DiaryEntry.objects.bulk_create([
        DiaryEntry(import_session=import_session, **row) for row in parsed.diary
    ])
    RatingEntry.objects.bulk_create([
        RatingEntry(import_session=import_session, **row) for row in parsed.ratings
    ])
    WatchlistEntry.objects.bulk_create([
        WatchlistEntry(import_session=import_session, **row) for row in parsed.watchlist
    ])
    LikedFilmEntry.objects.bulk_create([
        LikedFilmEntry(import_session=import_session, **row) for row in parsed.liked_films
    ])
    WatchedEntry.objects.bulk_create([
        WatchedEntry(import_session=import_session, **row) for row in parsed.watched
    ])
    ReviewEntry.objects.bulk_create([
        ReviewEntry(import_session=import_session, **row) for row in parsed.reviews
    ])
    for parsed_list in parsed.lists:
        user_list = UserList.objects.create(
            import_session=import_session, name=parsed_list['name'], tags=parsed_list['tags'],
            letterboxd_url=parsed_list['url'],
        )
        ListEntry.objects.bulk_create([
            ListEntry(user_list=user_list, **entry) for entry in parsed_list['entries']
        ])
