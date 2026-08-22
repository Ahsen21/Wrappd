import zipfile
from decimal import Decimal

from django.test import TestCase

from imports.models import ImportSession
from imports.services.parser import (
    ExportParseError,
    parse_diary_csv,
    parse_export,
    parse_likes_films_csv,
    parse_ratings_csv,
    parse_reviews_csv,
    parse_watched_csv,
    parse_watchlist_csv,
    persist_parsed_export,
)

from .helpers import build_export_zip


class ParseDiaryCsvTests(TestCase):
    def test_parses_rows_with_correct_types(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_diary_csv(zf)

        self.assertEqual(len(rows), 5)
        oppenheimer = rows[0]
        self.assertEqual(oppenheimer['title'], 'Oppenheimer')
        self.assertEqual(oppenheimer['year'], 2023)
        self.assertEqual(oppenheimer['rating'], Decimal('4.5'))
        self.assertFalse(oppenheimer['rewatch'])

    def test_rewatch_flag_and_duplicate_uri_both_kept(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_diary_csv(zf)

        paddington_rows = [r for r in rows if r['letterboxd_uri'] == 'https://boxd.it/cccc']
        self.assertEqual(len(paddington_rows), 2)
        self.assertTrue(paddington_rows[1]['rewatch'])

    def test_missing_year_and_rating_become_none(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_diary_csv(zf)

        no_year_row = next(r for r in rows if r['title'] == 'No Year Film')
        self.assertIsNone(no_year_row['year'])
        self.assertIsNone(no_year_row['rating'])

    def test_missing_column_raises_clear_error(self):
        broken_csv = 'Date,Name,Letterboxd URI\n2024-01-01,Some Film,https://boxd.it/xxxx\n'
        with zipfile.ZipFile(build_export_zip(diary_csv=broken_csv)) as zf:
            with self.assertRaises(ExportParseError) as ctx:
                parse_diary_csv(zf)
        self.assertIn('Year', str(ctx.exception))


class ParseOtherCsvTests(TestCase):
    def test_parse_ratings_csv(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_ratings_csv(zf)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['rating'], Decimal('4.5'))

    def test_parse_watchlist_csv(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_watchlist_csv(zf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Dune Part Two')

    def test_parse_likes_films_csv(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_likes_films_csv(zf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Paddington 2')

    def test_parse_watched_csv(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_watched_csv(zf)
        self.assertEqual(len(rows), 5)
        self.assertIn('Barbie', [r['title'] for r in rows])

    def test_parse_reviews_csv(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            rows = parse_reviews_csv(zf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Oppenheimer')
        self.assertEqual(rows[0]['review'], 'A three-hour fission reaction.')


class ParseExportTests(TestCase):
    def test_parse_export_picks_up_display_name_and_all_files(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            parsed = parse_export(zf)

        self.assertEqual(parsed.display_name, 'moviefan42')
        self.assertEqual(parsed.favorite_uris, ['https://boxd.it/aaaa', 'https://boxd.it/cccc', 'https://boxd.it/zzzz'])
        self.assertEqual(len(parsed.diary), 5)
        self.assertEqual(len(parsed.ratings), 3)
        self.assertEqual(len(parsed.watchlist), 1)
        self.assertEqual(len(parsed.liked_films), 1)
        self.assertEqual(len(parsed.watched), 5)
        self.assertEqual(len(parsed.reviews), 1)

    def test_missing_optional_files_yield_empty_lists_not_errors(self):
        with zipfile.ZipFile(
            build_export_zip(include_watchlist=False, include_likes=False, include_reviews=False)
        ) as zf:
            parsed = parse_export(zf)

        self.assertEqual(parsed.watchlist, [])
        self.assertEqual(parsed.liked_films, [])
        self.assertEqual(parsed.reviews, [])
        self.assertEqual(len(parsed.diary), 5)


class PersistParsedExportTests(TestCase):
    def test_persists_rows_and_sets_display_name(self):
        import_session = ImportSession.objects.create()
        with zipfile.ZipFile(build_export_zip()) as zf:
            parsed = parse_export(zf)

        persist_parsed_export(import_session, parsed)
        import_session.refresh_from_db()

        self.assertEqual(import_session.display_name, 'moviefan42')
        self.assertEqual(
            import_session.favorite_letterboxd_uris,
            ['https://boxd.it/aaaa', 'https://boxd.it/cccc', 'https://boxd.it/zzzz'],
        )
        self.assertEqual(import_session.diary_entries.count(), 5)
        self.assertEqual(import_session.rating_entries.count(), 3)
        self.assertEqual(import_session.watchlist_entries.count(), 1)
        self.assertEqual(import_session.liked_film_entries.count(), 1)
        self.assertEqual(import_session.watched_entries.count(), 5)
        self.assertEqual(import_session.review_entries.count(), 1)

    def test_does_not_overwrite_existing_display_name(self):
        import_session = ImportSession.objects.create(display_name='Already Set')
        with zipfile.ZipFile(build_export_zip()) as zf:
            parsed = parse_export(zf)

        persist_parsed_export(import_session, parsed)
        import_session.refresh_from_db()

        self.assertEqual(import_session.display_name, 'Already Set')

    def test_does_not_overwrite_existing_favorites(self):
        import_session = ImportSession.objects.create(favorite_letterboxd_uris=['https://boxd.it/existing'])
        with zipfile.ZipFile(build_export_zip()) as zf:
            parsed = parse_export(zf)

        persist_parsed_export(import_session, parsed)
        import_session.refresh_from_db()

        self.assertEqual(import_session.favorite_letterboxd_uris, ['https://boxd.it/existing'])
