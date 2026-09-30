from io import StringIO

import requests_mock
from django.core.management import call_command
from django.test import TestCase, override_settings

from tmdb.models import Movie


@override_settings(TMDB_API_KEY='test-key')
class BackfillKeywordsTests(TestCase):
    def test_backfills_only_movies_with_no_keywords(self):
        needs_backfill = Movie.objects.create(tmdb_id=1, title='Needs Backfill')
        already_has = Movie.objects.create(tmdb_id=2, title='Already Has One')
        already_has.keywords.create(tmdb_id=999, name='existing keyword')

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/1', json={
                'keywords': {'keywords': [{'id': 1, 'name': 'time travel'}, {'id': 2, 'name': 'heist'}]},
            })
            call_command('backfill_keywords', stdout=StringIO())

        self.assertEqual(
            set(Movie.objects.get(tmdb_id=1).keywords.values_list('name', flat=True)), {'time travel', 'heist'},
        )
        self.assertEqual(
            set(Movie.objects.get(tmdb_id=2).keywords.values_list('name', flat=True)), {'existing keyword'},
        )
        # Only the movie with zero keywords should have been requested at all --
        # id 2 already qualifies and must not cost an API call.
        self.assertEqual(m.call_count, 1)

    def test_a_failed_movie_does_not_abort_the_rest(self):
        Movie.objects.create(tmdb_id=1, title='Will Fail')
        Movie.objects.create(tmdb_id=2, title='Will Succeed')

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/1', status_code=500)
            m.get('https://api.themoviedb.org/3/movie/2', json={'keywords': {'keywords': [{'id': 1, 'name': 'noir'}]}})
            call_command('backfill_keywords', stdout=StringIO(), stderr=StringIO())

        self.assertEqual(Movie.objects.get(tmdb_id=1).keywords.count(), 0)
        self.assertEqual(list(Movie.objects.get(tmdb_id=2).keywords.values_list('name', flat=True)), ['noir'])

    def test_limit_option_caps_how_many_are_processed(self):
        for tmdb_id in range(1, 4):
            Movie.objects.create(tmdb_id=tmdb_id, title=f'Movie {tmdb_id}')

        with requests_mock.Mocker() as m:
            m.get(requests_mock.ANY, json={'keywords': {'keywords': [{'id': 1, 'name': 'tag'}]}})
            call_command('backfill_keywords', '--limit=1', stdout=StringIO())

        self.assertEqual(m.call_count, 1)
        self.assertEqual(Movie.objects.filter(keywords__isnull=False).distinct().count(), 1)

    def test_reports_nothing_to_do_when_every_movie_already_has_keywords(self):
        movie = Movie.objects.create(tmdb_id=1, title='Already Backfilled')
        movie.keywords.create(tmdb_id=1, name='already tagged')
        out = StringIO()

        with requests_mock.Mocker() as m:
            call_command('backfill_keywords', stdout=out)
            self.assertEqual(m.call_count, 0)

        self.assertIn('Nothing to backfill', out.getvalue())
