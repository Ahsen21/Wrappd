import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from imports.models import ImportSession, RatingEntry
from tmdb.models import Credit, Movie, Person

User = get_user_model()


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


class InsightFilmsViewTests(TestCase):
    """stats:insight_films -- the JSON endpoint behind the duo / genre-combo /
    decade tile modal."""

    def setUp(self):
        self.session = ImportSession.objects.create(display_name='Alex')
        self.director = Person.objects.create(tmdb_id=880, name='D')
        self.actor = Person.objects.create(tmdb_id=881, name='A')
        movie = Movie.objects.create(tmdb_id=890, title='Collab', release_year=2010)
        movie.directors.add(self.director)
        Credit.objects.create(movie=movie, person=self.actor, order=0)
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/c', title='Collab', year=2010,
            rating=Decimal('4.5'), movie=movie,
        )

    def _url(self, session_id=None):
        return reverse('stats:insight_films', kwargs={'session_id': session_id or self.session.id})

    def test_valid_pairing_request_returns_films(self):
        response = self.client.get(
            self._url(), {'kind': 'pairing', 'p1': self.director.tmdb_id, 'p2': self.actor.tmdb_id}
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual([f['title'] for f in data['films']], ['Collab'])
        self.assertEqual(data['films'][0]['rating'], '4.5')

    def test_decade_request_needs_only_p1(self):
        response = self.client.get(self._url(), {'kind': 'decade', 'p1': '2010s'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['films'][0]['title'], 'Collab')

    def test_runtime_request_returns_films_in_the_bucket(self):
        Movie.objects.filter(tmdb_id=890).update(runtime_minutes=120)
        response = self.client.get(self._url(), {'kind': 'runtime', 'p1': '90-150 min'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['films'][0]['title'], 'Collab')

    def test_unknown_runtime_bucket_returns_400(self):
        response = self.client.get(self._url(), {'kind': 'runtime', 'p1': 'a while'})
        self.assertEqual(response.status_code, 400)

    def test_unknown_kind_returns_400(self):
        response = self.client.get(self._url(), {'kind': 'producer', 'p1': '1'})
        self.assertEqual(response.status_code, 400)

    def test_missing_p1_returns_400(self):
        response = self.client.get(self._url(), {'kind': 'decade'})
        self.assertEqual(response.status_code, 400)

    def test_non_integer_id_for_a_pairing_returns_400(self):
        response = self.client.get(self._url(), {'kind': 'pairing', 'p1': 'abc', 'p2': 'def'})
        self.assertEqual(response.status_code, 400)

    def test_unknown_session_returns_404(self):
        response = self.client.get(
            reverse('stats:insight_films', kwargs={'session_id': '00000000-0000-0000-0000-000000000000'}),
            {'kind': 'decade', 'p1': '2010s'},
        )
        self.assertEqual(response.status_code, 404)


class DashboardViewTests(TestCase):
    """Covers both dashboard routes -- the original UUID one (stats:dashboard,
    always works, guest uploads included) and the newer username one
    (stats:dashboard_by_username, account-owned uploads only) -- and the share_url
    each hands the template, which is meant to always be the "nicest" link for
    that particular session (username-based when there's an account to build one
    from, the UUID link otherwise)."""

    def test_uuid_route_works_for_a_guest_session(self):
        session = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Guest')

        response = self.client.get(reverse('stats:dashboard', kwargs={'session_id': session.id}))

        self.assertEqual(response.status_code, 200)

    def test_uuid_route_share_url_stays_the_uuid_link_for_a_guest_session(self):
        session = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Guest')

        response = self.client.get(reverse('stats:dashboard', kwargs={'session_id': session.id}))

        self.assertIn(str(session.id), response.context['share_url'])

    def test_uuid_route_share_url_becomes_the_username_link_for_an_owned_session(self):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        session = ImportSession.objects.create(owner=user, status=ImportSession.Status.READY)

        response = self.client.get(reverse('stats:dashboard', kwargs={'session_id': session.id}))

        self.assertIn(
            reverse('stats:dashboard_by_username', kwargs={'username': 'alex'}),
            response.context['share_url'],
        )

    def test_username_route_shows_the_accounts_most_recent_ready_session(self):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        older = ImportSession.objects.create(owner=user, status=ImportSession.Status.READY)
        newer = ImportSession.objects.create(owner=user, status=ImportSession.Status.READY)
        ImportSession.objects.filter(pk=older.pk).update(uploaded_at=older.uploaded_at - timedelta(days=1))

        response = self.client.get(reverse('stats:dashboard_by_username', kwargs={'username': 'alex'}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['import_session'], newer)

    def test_username_route_404s_for_an_account_with_no_upload(self):
        User.objects.create_user(username='alex', password='a-very-unguessable-pw1')

        response = self.client.get(reverse('stats:dashboard_by_username', kwargs={'username': 'alex'}))

        self.assertEqual(response.status_code, 404)

    def test_username_route_404s_for_an_unknown_username(self):
        response = self.client.get(reverse('stats:dashboard_by_username', kwargs={'username': 'nobody'}))

        self.assertEqual(response.status_code, 404)

    def test_shorts_param_defaults_to_included(self):
        session = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Guest')

        response = self.client.get(reverse('stats:dashboard', kwargs={'session_id': session.id}))

        self.assertFalse(response.context['exclude_shorts'])
        self.assertNotIn('shorts=exclude', response.context['share_url'])

    def test_shorts_exclude_param_is_read_and_carried_into_the_share_url(self):
        session = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Guest')

        response = self.client.get(
            reverse('stats:dashboard', kwargs={'session_id': session.id}), {'shorts': 'exclude'}
        )

        self.assertTrue(response.context['exclude_shorts'])
        self.assertIn('shorts=exclude', response.context['share_url'])

    def test_an_unrecognized_shorts_value_is_treated_as_included(self):
        session = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Guest')

        response = self.client.get(
            reverse('stats:dashboard', kwargs={'session_id': session.id}), {'shorts': 'nonsense'}
        )

        self.assertFalse(response.context['exclude_shorts'])


class CompareViewTests(TestCase):
    def test_shorts_exclude_param_is_read(self):
        session_a = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Alex')
        session_b = ImportSession.objects.create(status=ImportSession.Status.READY, display_name='Sam')

        response = self.client.get(
            reverse('stats:compare', kwargs={'session_a': session_a.id, 'session_b': session_b.id}),
            {'shorts': 'exclude'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['exclude_shorts'])


class CompareByUsernamesViewTests(TestCase):
    """stats:compare_by_usernames -- the /compare/<username_a>-vs-<username_b>/
    route, alongside the original UUID-pair stats:compare."""

    def setUp(self):
        self.user_a = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.user_b = User.objects.create_user(username='sam', password='a-very-unguessable-pw2')

    def test_shows_each_accounts_most_recent_ready_session(self):
        ImportSession.objects.create(owner=self.user_a, status=ImportSession.Status.PENDING)
        session_a = ImportSession.objects.create(owner=self.user_a, status=ImportSession.Status.READY)
        session_b = ImportSession.objects.create(owner=self.user_b, status=ImportSession.Status.READY)

        response = self.client.get(
            reverse('stats:compare_by_usernames', kwargs={'username_a': 'alex', 'username_b': 'sam'})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['session_a'], session_a)
        self.assertEqual(response.context['session_b'], session_b)

    def test_404s_when_one_account_has_no_upload(self):
        ImportSession.objects.create(owner=self.user_a, status=ImportSession.Status.READY)
        # user_b has no upload at all.

        response = self.client.get(
            reverse('stats:compare_by_usernames', kwargs={'username_a': 'alex', 'username_b': 'sam'})
        )

        self.assertEqual(response.status_code, 404)

    def test_404s_for_an_unknown_username(self):
        ImportSession.objects.create(owner=self.user_a, status=ImportSession.Status.READY)

        response = self.client.get(
            reverse('stats:compare_by_usernames', kwargs={'username_a': 'alex', 'username_b': 'nobody'})
        )

        self.assertEqual(response.status_code, 404)
