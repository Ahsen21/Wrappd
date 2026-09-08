from io import StringIO

import requests_mock
from django.core.management import call_command
from django.test import TestCase, override_settings

from tmdb.models import Movie


@override_settings(TMDB_API_KEY='test-key')
class BackfillVoteCountsTests(TestCase):
    def test_backfills_only_movies_missing_vote_count(self):
        Movie.objects.create(tmdb_id=1, title='Needs Backfill')
        Movie.objects.create(tmdb_id=2, title='Already Has It', vote_count=500)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/1', json={'vote_count': 12345})
            call_command('backfill_vote_counts', stdout=StringIO())

        self.assertEqual(Movie.objects.get(tmdb_id=1).vote_count, 12345)
        self.assertEqual(Movie.objects.get(tmdb_id=2).vote_count, 500)
        # Only the one movie actually missing a vote_count should have been
        # requested at all -- id 2 already qualifies and must not cost an API call.
        self.assertEqual(m.call_count, 1)

    def test_a_failed_movie_does_not_abort_the_rest(self):
        Movie.objects.create(tmdb_id=1, title='Will Fail')
        Movie.objects.create(tmdb_id=2, title='Will Succeed')

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/1', status_code=500)
            m.get('https://api.themoviedb.org/3/movie/2', json={'vote_count': 42})
            call_command('backfill_vote_counts', stdout=StringIO(), stderr=StringIO())

        self.assertIsNone(Movie.objects.get(tmdb_id=1).vote_count)
        self.assertEqual(Movie.objects.get(tmdb_id=2).vote_count, 42)

    def test_limit_option_caps_how_many_are_processed(self):
        for tmdb_id in range(1, 4):
            Movie.objects.create(tmdb_id=tmdb_id, title=f'Movie {tmdb_id}')

        with requests_mock.Mocker() as m:
            m.get(requests_mock.ANY, json={'vote_count': 1})
            call_command('backfill_vote_counts', '--limit=1', stdout=StringIO())

        self.assertEqual(m.call_count, 1)
        self.assertEqual(Movie.objects.filter(vote_count__isnull=False).count(), 1)

    def test_reports_nothing_to_do_when_every_movie_already_has_a_count(self):
        Movie.objects.create(tmdb_id=1, title='Already Backfilled', vote_count=10)
        out = StringIO()

        with requests_mock.Mocker() as m:
            call_command('backfill_vote_counts', stdout=out)
            self.assertEqual(m.call_count, 0)

        self.assertIn('Nothing to backfill', out.getvalue())
