from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from imports.models import ImportSession

User = get_user_model()


def _give_client_a_session(client):
    """Establishes a real, cookie-backed session for the test client without going
    through an actual upload -- the documented way to pre-seed django.test.Client
    with a session_key it will send back on the next request."""
    session = client.session
    session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    return session.session_key


class LandingRouterTests(TestCase):
    """"/" is a state router (core.views.landing): gate -> upload -> home, per the
    onboarding flow -- see the view's own docstring for the full state table."""

    def test_unrecognized_visitor_is_sent_to_the_login_page_as_the_entry_gate(self):
        # accounts:login doubles as the entry gate -- its own template leads with the
        # login form, then "New user? Sign up", then continue as guest.
        response = self.client.get(reverse('core:landing'))

        self.assertRedirects(response, reverse('accounts:login'))
        self.assertContains(self.client.get(reverse('accounts:login')), 'continue as a guest')

    def test_authenticated_with_no_upload_redirects_to_upload_not_the_gate(self):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.client.force_login(user)

        response = self.client.get(reverse('core:landing'))

        self.assertRedirects(response, reverse('imports:upload'))

    def test_authenticated_with_only_a_non_ready_session_still_redirects_to_upload(self):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        ImportSession.objects.create(owner=user, status=ImportSession.Status.PENDING)
        self.client.force_login(user)

        response = self.client.get(reverse('core:landing'))

        self.assertRedirects(response, reverse('imports:upload'))

    def test_authenticated_with_ready_session_sees_home_screen_linking_to_dashboard(self):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        session = ImportSession.objects.create(owner=user, status=ImportSession.Status.READY)
        self.client.force_login(user)

        response = self.client.get(reverse('core:landing'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'core/landing.html')
        self.assertContains(response, reverse('stats:dashboard', kwargs={'session_id': session.id}))

    def test_guest_with_ready_session_tied_to_browser_sees_home_screen(self):
        session_key = _give_client_a_session(self.client)
        ImportSession.objects.create(session_key=session_key, status=ImportSession.Status.READY)

        response = self.client.get(reverse('core:landing'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'core/landing.html')

    def test_guest_with_no_ready_session_sees_gate_not_a_redirect_loop(self):
        response = self.client.get(reverse('core:landing'))

        self.assertRedirects(response, reverse('accounts:login'))


class NavProfileMenuTests(TestCase):
    """The nav's logged-in profile menu (core/templates/core/base.html), rendered on
    any page -- checked here via accounts:login since that's reachable both logged
    out and logged in."""

    def test_logged_out_shows_login_and_signup_not_a_profile_button(self):
        response = self.client.get(reverse('accounts:login'))

        self.assertContains(response, reverse('accounts:login'))
        self.assertContains(response, reverse('accounts:signup'))
        # The nav's toggle <script> always renders and references this id by name --
        # check for the actual markup, not just the substring, so the JS doesn't
        # make this assertion a false negative.
        self.assertNotContains(response, '<div class="nav-profile">')

    def test_logged_in_shows_profile_button_with_username_and_menu_items(self):
        user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')
        self.client.force_login(user)

        # core:landing redirects a fresh account (no upload yet) to imports:upload --
        # follow it to reach an actual rendered page (base.html's nav) to check.
        response = self.client.get(reverse('core:landing'), follow=True)

        self.assertContains(response, 'alex')
        self.assertContains(response, 'nav-profile-btn')
        self.assertContains(response, reverse('imports:upload') + '?new=1')
        self.assertContains(response, reverse('imports:my_uploads'))
        self.assertContains(response, reverse('accounts:logout'))
        self.assertNotContains(response, reverse('accounts:login'))
