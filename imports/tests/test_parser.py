import zipfile
from decimal import Decimal

from django.test import TestCase

from imports.models import ImportSession, ListEntry, UserList
from imports.services.parser import (
    ExportParseError,
    parse_diary_csv,
    parse_export,
    parse_lists,
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


LIST_CSV = """Letterboxd list export v7
Date,Name,Tags,URL,Description
2026-12-01,My 2026 Favorites,"top2026, Favorites",https://boxd.it/abcd,Best of the year

Position,Name,Year,URL,Description
2,Past Lives,2023,https://boxd.it/bbbb,
1,Oppenheimer,2023,https://boxd.it/aaaa,
3,No Year Film,,https://boxd.it/dddd,
"""


def _zip_with_lists(**files):
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        for name, content in files.items():
            zf.writestr(name.replace('__', '/').replace('_csv', '.csv'), content)
    buffer.seek(0)
    return buffer


class ParseListsTests(TestCase):
    def test_parses_name_lowercased_tags_and_films_in_position_order(self):
        with zipfile.ZipFile(_zip_with_lists(lists__favs_csv=LIST_CSV)) as zf:
            lists = parse_lists(zf)

        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]['name'], 'My 2026 Favorites')
        self.assertEqual(lists[0]['tags'], ['top2026', 'favorites'])
        self.assertEqual([e['title'] for e in lists[0]['entries']], ['Oppenheimer', 'Past Lives', 'No Year Film'])
        self.assertEqual(lists[0]['entries'][0]['year'], 2023)
        self.assertIsNone(lists[0]['entries'][2]['year'])

    def test_a_list_with_no_tags_has_an_empty_tag_list(self):
        untagged = LIST_CSV.replace('"top2026, Favorites"', '')
        with zipfile.ZipFile(_zip_with_lists(lists__plain_csv=untagged)) as zf:
            self.assertEqual(parse_lists(zf)[0]['tags'], [])

    def test_a_file_not_in_the_list_layout_is_skipped_not_fatal(self):
        with zipfile.ZipFile(_zip_with_lists(lists__broken_csv='just,some,junk\n1,2,3\n', lists__favs_csv=LIST_CSV)) as zf:
            lists = parse_lists(zf)

        self.assertEqual([l['name'] for l in lists], ['My 2026 Favorites'])

    def test_an_export_without_a_lists_folder_has_no_lists(self):
        with zipfile.ZipFile(build_export_zip()) as zf:
            self.assertEqual(parse_lists(zf), [])


class PersistListsTests(TestCase):
    def test_lists_and_their_films_are_saved_in_order(self):
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(build_export_zip()) as source, zipfile.ZipFile(buffer, 'w') as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
            target.writestr('lists/favs.csv', LIST_CSV)
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as zf:
            parsed = parse_export(zf)
        session = ImportSession.objects.create()

        persist_parsed_export(session, parsed)

        saved = UserList.objects.get(import_session=session)
        self.assertEqual(saved.tags, ['top2026', 'favorites'])
        self.assertEqual(
            list(ListEntry.objects.filter(user_list=saved).values_list('title', flat=True)),
            ['Oppenheimer', 'Past Lives', 'No Year Film'],
        )
