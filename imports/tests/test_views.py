import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from imports.models import ImportSession, canonical_compare_path

from .helpers import build_export_zip

User = get_user_model()


def _give_client_a_session(client):
    """Establishes a real, cookie-backed session for the test client without going
    through an actual upload -- the documented way to pre-seed django.test.Client
    with a session_key it will send back on the next request."""
    session = client.session
    session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    return session.session_key


class UploadViewTests(TestCase):
    """Director's Cut -- stays anonymous, no login required."""

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_upload_creates_import_session_and_redirects_to_landing(self, mock_enrich):
        # core:landing is the entry-flow router (core/views.py) -- with this upload now
        # existing, it renders the home screen instead of bouncing back to the gate.
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')

        response = self.client.post(reverse('imports:upload'), {'export_file': upload})

        import_session = ImportSession.objects.get()
        self.assertRedirects(response, reverse('core:landing'))
        self.assertEqual(import_session.status, ImportSession.Status.READY)
        self.assertEqual(import_session.diary_entries.count(), 5)
        self.assertIsNone(import_session.owner)
        mock_enrich.assert_called_once_with(import_session)

    def test_get_with_no_existing_session_shows_upload_form(self):
        response = self.client.get(reverse('imports:upload'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose File')

    def test_get_with_existing_ready_session_redirects_to_its_dashboard(self):
        # The nav's "Director's Cut" link always points here -- someone who already
        # uploaded should land back on their dashboard, not an empty upload form.
        session = ImportSession.objects.create(status=ImportSession.Status.READY)
        session_key = _give_client_a_session(self.client)
        ImportSession.objects.filter(pk=session.pk).update(session_key=session_key)

        response = self.client.get(reverse('imports:upload'))

        self.assertRedirects(response, reverse('stats:dashboard', kwargs={'session_id': session.id}))

    def test_get_with_existing_owned_session_redirects_to_username_dashboard(self):
        # Same smart redirect as above, but for a logged-in account -- lands on the
        # permanent /dashboard/<username>/ link, not the raw per-upload UUID.
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.client.force_login(user)
        ImportSession.objects.create(owner=user, status=ImportSession.Status.READY)

        response = self.client.get(reverse('imports:upload'))

        self.assertRedirects(response, reverse('stats:dashboard_by_username', kwargs={'username': 'alex'}))

    def test_get_with_new_param_shows_form_even_with_an_existing_session(self):
        # The profile menu's "Upload a new export" needs this exception -- otherwise
        # it would just bounce back to the existing dashboard like the plain nav link.
        session = ImportSession.objects.create(status=ImportSession.Status.READY)
        session_key = _give_client_a_session(self.client)
        ImportSession.objects.filter(pk=session.pk).update(session_key=session_key)

        response = self.client.get(reverse('imports:upload'), {'new': '1'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose File')

    def test_upload_rejects_invalid_file_and_rerenders_form_with_no_session_created(self):
        upload = SimpleUploadedFile('export.txt', b'nope', content_type='text/plain')

        response = self.client.post(reverse('imports:upload'), {'export_file': upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please upload a .zip file')
        self.assertEqual(ImportSession.objects.count(), 0)

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_upload_while_logged_in_attributes_owner_without_requiring_login(self, mock_enrich):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.client.force_login(user)
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')

        self.client.post(reverse('imports:upload'), {'export_file': upload})

        import_session = ImportSession.objects.get()
        self.assertEqual(import_session.owner, user)


class CompareUploadViewTests(TestCase):
    """Double Feature's dual-upload entry point -- login-gated."""

    def setUp(self):
        self.user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')

    def test_anonymous_get_redirects_to_login(self):
        response = self.client.get(reverse('imports:upload_compare'))
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next={reverse('imports:upload_compare')}"
        )

    def test_anonymous_post_redirects_to_login_and_creates_no_session(self):
        upload_a = SimpleUploadedFile('a.zip', build_export_zip().read(), content_type='application/zip')
        upload_b = SimpleUploadedFile('b.zip', build_export_zip().read(), content_type='application/zip')

        response = self.client.post(
            reverse('imports:upload_compare'), {'export_file_a': upload_a, 'export_file_b': upload_b}
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response.url)
        self.assertEqual(ImportSession.objects.count(), 0)

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_compare_upload_creates_two_sessions_and_redirects_to_compare(self, mock_enrich):
        self.client.force_login(self.user)
        upload_a = SimpleUploadedFile('a.zip', build_export_zip().read(), content_type='application/zip')
        upload_b = SimpleUploadedFile('b.zip', build_export_zip().read(), content_type='application/zip')

        response = self.client.post(
            reverse('imports:upload_compare'), {'export_file_a': upload_a, 'export_file_b': upload_b}
        )

        self.assertEqual(ImportSession.objects.count(), 2)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(mock_enrich.call_count, 2)

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_only_first_upload_is_attributed_to_the_logged_in_submitter(self, mock_enrich):
        self.client.force_login(self.user)
        upload_a = SimpleUploadedFile('a.zip', build_export_zip().read(), content_type='application/zip')
        upload_b = SimpleUploadedFile('b.zip', build_export_zip().read(), content_type='application/zip')

        self.client.post(reverse('imports:upload_compare'), {'export_file_a': upload_a, 'export_file_b': upload_b})

        sessions = list(ImportSession.objects.order_by('uploaded_at'))
        self.assertEqual(sessions[0].owner, self.user)
        self.assertIsNone(sessions[1].owner)


class CompareJoinViewTests(TestCase):
    """CompareJoinView is the link-based alternative to CompareUploadView: upload
    once (possibly on an earlier visit), then paste a friend's already-READY
    session's link/id to reach stats:compare -- no second file needed. Login-gated,
    since "my session" is resolved by ownership rather than by an explicit id."""

    def setUp(self):
        self.user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')

    def _make_ready_session(self, **extra):
        return ImportSession.objects.create(status=ImportSession.Status.READY, **extra)

    def test_anonymous_get_redirects_to_login(self):
        response = self.client.get(reverse('imports:compare_join'))
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next={reverse('imports:compare_join')}"
        )

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_get_with_no_owned_session_shows_upload_form(self, mock_enrich):
        self.client.force_login(self.user)

        response = self.client.get(reverse('imports:compare_join'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose File')
        self.assertNotContains(response, "Friend's Wrappd link")

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_get_ignores_another_users_ready_session(self, mock_enrich):
        # A READY session with no owner (or a different owner) must not count as "mine".
        self._make_ready_session(display_name='not me')
        self.client.force_login(self.user)

        response = self.client.get(reverse('imports:compare_join'))

        self.assertContains(response, 'Choose File')

    @mock.patch('imports.views.enrich_import_session_fully')
    def test_post_upload_creates_owned_session_and_shows_join_form_next(self, mock_enrich):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')

        response = self.client.post(reverse('imports:compare_join'), {'export_file': upload})

        self.assertEqual(response.status_code, 200)
        import_session = ImportSession.objects.get()
        self.assertEqual(import_session.owner, self.user)
        self.assertContains(response, 'Friend&#x27;s Wrappd link')

    def test_post_with_valid_friend_link_redirects_to_compare(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')
        with mock.patch('imports.views.enrich_import_session_fully'):
            self.client.post(reverse('imports:compare_join'), {'export_file': upload})
        my_session = ImportSession.objects.get(owner=self.user)
        friend_session = self._make_ready_session(display_name='Friend')
        friend_link = f'https://example.com/stats/dashboard/{friend_session.id}/'

        response = self.client.post(reverse('imports:compare_join'), {'friend_link': friend_link})

        self.assertRedirects(
            response,
            reverse('stats:compare', kwargs={'session_a': my_session.id, 'session_b': friend_session.id}),
        )

    def test_post_picks_most_recent_owned_session_when_several_exist(self):
        self.client.force_login(self.user)
        older = ImportSession.objects.create(status=ImportSession.Status.READY, owner=self.user)
        newer = ImportSession.objects.create(status=ImportSession.Status.READY, owner=self.user)
        # Force a deterministic ordering regardless of clock resolution.
        ImportSession.objects.filter(pk=older.pk).update(uploaded_at=older.uploaded_at - timedelta(days=1))
        friend_session = self._make_ready_session(display_name='Friend')

        response = self.client.post(reverse('imports:compare_join'), {'friend_link': str(friend_session.id)})

        self.assertRedirects(
            response,
            reverse('stats:compare', kwargs={'session_a': newer.id, 'session_b': friend_session.id}),
        )

    def test_post_with_garbage_friend_link_shows_form_error_and_does_not_redirect(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')
        with mock.patch('imports.views.enrich_import_session_fully'):
            self.client.post(reverse('imports:compare_join'), {'export_file': upload})

        response = self.client.post(reverse('imports:compare_join'), {'friend_link': 'not a link at all'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "doesn&#x27;t look like a Wrappd link")

    def test_post_with_unknown_friend_session_id_shows_form_error(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')
        with mock.patch('imports.views.enrich_import_session_fully'):
            self.client.post(reverse('imports:compare_join'), {'export_file': upload})
        unknown_id = uuid.uuid4()

        response = self.client.post(reverse('imports:compare_join'), {'friend_link': str(unknown_id)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn&#x27;t find that Wrappd link")

    def test_post_with_not_ready_friend_session_shows_form_error(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')
        with mock.patch('imports.views.enrich_import_session_fully'):
            self.client.post(reverse('imports:compare_join'), {'export_file': upload})
        pending_friend = ImportSession.objects.create(status=ImportSession.Status.PENDING)

        response = self.client.post(reverse('imports:compare_join'), {'friend_link': str(pending_friend.id)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "isn&#x27;t ready yet")


class ImportSessionUsernameLookupTests(TestCase):
    """ImportSession.latest_for_owner_username (stats:dashboard_by_username) and
    .latest_for_letterboxd_username (Double Feature's search-by-username flow)."""

    def setUp(self):
        self.user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')

    def test_latest_for_owner_username_finds_the_most_recent_ready_session(self):
        older = ImportSession.objects.create(owner=self.user, status=ImportSession.Status.READY)
        newer = ImportSession.objects.create(owner=self.user, status=ImportSession.Status.READY)
        ImportSession.objects.filter(pk=older.pk).update(uploaded_at=older.uploaded_at - timedelta(days=1))

        self.assertEqual(ImportSession.latest_for_owner_username('alex'), newer)

    def test_latest_for_owner_username_ignores_non_ready_and_unknown_usernames(self):
        ImportSession.objects.create(owner=self.user, status=ImportSession.Status.PENDING)

        self.assertIsNone(ImportSession.latest_for_owner_username('alex'))
        self.assertIsNone(ImportSession.latest_for_owner_username('nobody'))

    def test_latest_for_letterboxd_username_finds_a_searchable_owned_session(self):
        session = ImportSession.objects.create(
            owner=self.user, status=ImportSession.Status.READY, display_name='moviefan42',
        )

        self.assertEqual(ImportSession.latest_for_letterboxd_username('moviefan42'), session)
        # Exact match, but case-insensitive.
        self.assertEqual(ImportSession.latest_for_letterboxd_username('MovieFan42'), session)

    def test_latest_for_letterboxd_username_excludes_guest_uploads(self):
        ImportSession.objects.create(status=ImportSession.Status.READY, display_name='moviefan42')

        self.assertIsNone(ImportSession.latest_for_letterboxd_username('moviefan42'))

    def test_latest_for_letterboxd_username_excludes_non_searchable_accounts(self):
        ImportSession.objects.create(
            owner=self.user, status=ImportSession.Status.READY, display_name='moviefan42',
        )
        self.user.profile.is_searchable = False
        self.user.profile.save(update_fields=['is_searchable'])

        self.assertIsNone(ImportSession.latest_for_letterboxd_username('moviefan42'))


class CanonicalComparePathTests(TestCase):
    """canonical_compare_path -- the /compare/<uuid_a>/<uuid_b>/ vs.
    /compare/<username_a>-vs-<username_b>/ decision, used by every place that
    redirects into stats:compare (CompareUploadView, CompareJoinView)."""

    def setUp(self):
        self.user_a = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.user_b = User.objects.create_user(username='sam', password='a-very-unguessable-pw2')

    def test_both_owned_uses_the_username_pair_path(self):
        session_a = ImportSession.objects.create(owner=self.user_a, status=ImportSession.Status.READY)
        session_b = ImportSession.objects.create(owner=self.user_b, status=ImportSession.Status.READY)

        self.assertEqual(
            canonical_compare_path(session_a, session_b),
            reverse('stats:compare_by_usernames', kwargs={'username_a': 'alex', 'username_b': 'sam'}),
        )

    def test_one_side_a_guest_falls_back_to_the_uuid_pair_path(self):
        session_a = ImportSession.objects.create(owner=self.user_a, status=ImportSession.Status.READY)
        session_b = ImportSession.objects.create(status=ImportSession.Status.READY)  # no owner

        self.assertEqual(
            canonical_compare_path(session_a, session_b),
            reverse('stats:compare', kwargs={'session_a': session_a.id, 'session_b': session_b.id}),
        )

    def test_both_guests_uses_the_uuid_pair_path(self):
        session_a = ImportSession.objects.create(status=ImportSession.Status.READY)
        session_b = ImportSession.objects.create(status=ImportSession.Status.READY)

        self.assertEqual(
            canonical_compare_path(session_a, session_b),
            reverse('stats:compare', kwargs={'session_a': session_a.id, 'session_b': session_b.id}),
        )


class CompareJoinSearchTests(TestCase):
    """The search-by-Letterboxd-username alternative to pasting a friend link,
    added alongside JoinCompareForm on the same CompareJoinView."""

    def setUp(self):
        self.user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.client.force_login(self.user)
        self.my_session = ImportSession.objects.create(owner=self.user, status=ImportSession.Status.READY)

    def test_get_shows_search_form_alongside_link_form(self):
        response = self.client.get(reverse('imports:compare_join'))

        self.assertContains(response, "Friend&#x27;s Letterboxd username")
        self.assertContains(response, "Friend&#x27;s Wrappd link")

    def test_search_by_letterboxd_username_redirects_to_compare(self):
        friend = User.objects.create_user(username='friend_account', password='a-very-unguessable-pw2')
        friend_session = ImportSession.objects.create(
            owner=friend, status=ImportSession.Status.READY, display_name='moviefan42',
        )

        response = self.client.post(reverse('imports:compare_join'), {'letterboxd_username': 'moviefan42'})

        # Both sides are account-owned here, so this lands on the pretty
        # username-pair URL, not the raw UUID one.
        self.assertRedirects(
            response,
            reverse('stats:compare_by_usernames', kwargs={'username_a': 'alex', 'username_b': 'friend_account'}),
        )

    def test_search_for_unknown_username_shows_form_error(self):
        response = self.client.post(reverse('imports:compare_join'), {'letterboxd_username': 'nobody'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No searchable Wrappd account found')

    def test_search_for_private_accounts_username_shows_same_not_found_error(self):
        friend = User.objects.create_user(username='friend_account', password='a-very-unguessable-pw2')
        friend.profile.is_searchable = False
        friend.profile.save(update_fields=['is_searchable'])
        ImportSession.objects.create(owner=friend, status=ImportSession.Status.READY, display_name='moviefan42')

        response = self.client.post(reverse('imports:compare_join'), {'letterboxd_username': 'moviefan42'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No searchable Wrappd account found')

    def test_search_ignores_guest_uploads_with_matching_display_name(self):
        ImportSession.objects.create(status=ImportSession.Status.READY, display_name='moviefan42')

        response = self.client.post(reverse('imports:compare_join'), {'letterboxd_username': 'moviefan42'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No searchable Wrappd account found')
