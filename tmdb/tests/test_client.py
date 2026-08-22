from unittest import mock

import requests_mock
from django.test import TestCase, override_settings

from tmdb.services.client import TMDBClientError, get_movie_details, search_movie, search_tv


class SearchMovieTests(TestCase):
    @override_settings(TMDB_API_KEY='test-key')
    def test_returns_all_results(self):
        with requests_mock.Mocker() as m:
            m.get(
                'https://api.themoviedb.org/3/search/movie',
                json={'results': [{'id': 1, 'title': 'Foo'}, {'id': 2, 'title': 'Foo 2'}]},
            )
            results = search_movie('Foo', 2020)
        self.assertEqual([r['id'] for r in results], [1, 2])

    @override_settings(TMDB_API_KEY='test-key')
    def test_returns_empty_list_when_no_results(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': []})
            results = search_movie('Foo', 2020)
        self.assertEqual(results, [])

    @override_settings(TMDB_API_KEY='')
    def test_raises_when_api_key_missing(self):
        with self.assertRaises(TMDBClientError):
            search_movie('Foo', 2020)

    @override_settings(TMDB_API_KEY='test-key')
    def test_raises_on_http_error(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', status_code=500)
            with self.assertRaises(TMDBClientError):
                search_movie('Foo', 2020)

    @override_settings(TMDB_API_KEY='test-key')
    def test_retries_on_429_and_succeeds(self):
        with requests_mock.Mocker() as m, mock.patch('tmdb.services.client.time.sleep') as mock_sleep:
            m.get('https://api.themoviedb.org/3/search/movie', [
                {'status_code': 429},
                {'json': {'results': [{'id': 1, 'title': 'Foo'}]}, 'status_code': 200},
            ])
            results = search_movie('Foo', 2020)
        self.assertEqual(results[0]['id'], 1)
        mock_sleep.assert_called_once()

    @override_settings(TMDB_API_KEY='test-key')
    def test_raises_after_exhausting_retries_on_persistent_429(self):
        with requests_mock.Mocker() as m, mock.patch('tmdb.services.client.time.sleep'):
            m.get('https://api.themoviedb.org/3/search/movie', status_code=429)
            with self.assertRaises(TMDBClientError):
                search_movie('Foo', 2020)


class SearchTvTests(TestCase):
    @override_settings(TMDB_API_KEY='test-key')
    def test_returns_all_results(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': [{'id': 7, 'name': 'Foo Show'}]})
            results = search_tv('Foo Show', 2020)
        self.assertEqual(results[0]['id'], 7)

    @override_settings(TMDB_API_KEY='test-key')
    def test_returns_empty_list_when_no_results(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            results = search_tv('Foo Show', 2020)
        self.assertEqual(results, [])

    @override_settings(TMDB_API_KEY='test-key')
    def test_uses_first_air_date_year_param_not_year(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            search_tv('Foo Show', 2020)
        query_params = m.request_history[0].qs
        self.assertEqual(query_params.get('first_air_date_year'), ['2020'])
        self.assertNotIn('year', query_params)

    @override_settings(TMDB_API_KEY='')
    def test_raises_when_api_key_missing(self):
        with self.assertRaises(TMDBClientError):
            search_tv('Foo Show', 2020)


class GetMovieDetailsTests(TestCase):
    @override_settings(TMDB_API_KEY='test-key')
    def test_returns_json_body(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/42', json={'id': 42, 'runtime': 100})
            details = get_movie_details(42)
        self.assertEqual(details['runtime'], 100)

    @override_settings(TMDB_API_KEY='test-key')
    def test_raises_on_http_error(self):
        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/42', status_code=404)
            with self.assertRaises(TMDBClientError):
                get_movie_details(42)
