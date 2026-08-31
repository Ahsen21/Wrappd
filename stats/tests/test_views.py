import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from imports.models import ImportSession, RatingEntry
from tmdb.models import Movie, Person


class PersonFilmographyViewTests(TestCase):
    def setUp(self):
        self.session = ImportSession.objects.create(display_name='Alex')
        self.director = Person.objects.create(tmdb_id=800, name='Director X')
        self.movie = Movie.objects.create(tmdb_id=801, title='Their Film', release_year=2020)
        self.movie.directors.add(self.director)
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/a', title='Their Film', year=2020,
            rating=Decimal('4.5'), movie=self.movie,
        )

    def _url(self, session_id=None, tmdb_id=None):
        return reverse(
            'stats:person_filmography',
            kwargs={'session_id': session_id or self.session.id, 'tmdb_id': tmdb_id or self.director.tmdb_id},
        )

    def test_valid_director_request_returns_films(self):
        response = self.client.get(self._url(), {'role': 'director'})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['person_name'], 'Director X')
        self.assertEqual(data['role'], 'director')
        self.assertEqual(len(data['films']), 1)
        self.assertEqual(data['films'][0]['title'], 'Their Film')
        self.assertEqual(data['films'][0]['rating'], '4.5')

    def test_missing_role_returns_400(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 400)

    def test_invalid_role_returns_400(self):
        response = self.client.get(self._url(), {'role': 'producer'})
        self.assertEqual(response.status_code, 400)

    def test_unknown_session_returns_404(self):
        response = self.client.get(
            reverse('stats:person_filmography', kwargs={
                'session_id': '00000000-0000-0000-0000-000000000000', 'tmdb_id': self.director.tmdb_id,
            }),
            {'role': 'director'},
        )
        self.assertEqual(response.status_code, 404)

    def test_unknown_person_returns_404(self):
        response = self.client.get(self._url(tmdb_id=999999), {'role': 'director'})
        self.assertEqual(response.status_code, 404)
