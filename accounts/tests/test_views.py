from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class SignUpViewTests(TestCase):
    def test_signup_creates_user_and_logs_them_in(self):
        response = self.client.post(reverse('accounts:signup'), {
            'username': 'newuser',
            'password1': 'a-very-unguessable-pw1',
            'password2': 'a-very-unguessable-pw1',
        })

        self.assertEqual(User.objects.filter(username='newuser').count(), 1)
        # core:landing is the entry-flow router -- a freshly signed-up user has no
        # upload yet, so it redirects them onward to imports:upload (hence status 302
        # on the target itself, not core:landing's own render).
        self.assertRedirects(response, reverse('core:landing'), target_status_code=302)
        # Logged in immediately -- no separate "now go log in" step.
        whoami = self.client.get(reverse('core:landing'))
        self.assertTrue(whoami.wsgi_request.user.is_authenticated)

    def test_signup_honors_next_param_and_validates_it(self):
        safe_next = reverse('imports:compare_join')
        response = self.client.post(
            reverse('accounts:signup') + f'?next={safe_next}',
            {'username': 'someone', 'password1': 'a-very-unguessable-pw1', 'password2': 'a-very-unguessable-pw1'},
        )
        self.assertRedirects(response, safe_next)

    def test_signup_rejects_unsafe_next_param(self):
        response = self.client.post(
            reverse('accounts:signup') + '?next=https://evil.example.com/',
            {'username': 'someoneelse', 'password1': 'a-very-unguessable-pw1', 'password2': 'a-very-unguessable-pw1'},
        )
        self.assertRedirects(response, reverse('core:landing'), target_status_code=302)

    def test_duplicate_username_shows_form_error_and_creates_no_second_user(self):
        User.objects.create_user(username='taken', password='whatever-pw-1')

        response = self.client.post(reverse('accounts:signup'), {
            'username': 'taken',
            'password1': 'a-very-unguessable-pw1',
            'password2': 'a-very-unguessable-pw1',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username='taken').count(), 1)


class LoginLogoutViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alex', password='a-very-unguessable-pw1')

    def test_login_with_correct_credentials_authenticates(self):
        response = self.client.post(reverse('accounts:login'), {'username': 'alex', 'password': 'a-very-unguessable-pw1'})
        # core:landing redirects an authenticated-but-no-upload-yet user onward to
        # imports:upload -- see the signup test above for the same reasoning.
        self.assertRedirects(response, reverse('core:landing'), target_status_code=302)
        whoami = self.client.get(reverse('core:landing'))
        self.assertTrue(whoami.wsgi_request.user.is_authenticated)

    def test_login_with_wrong_password_shows_error_and_stays_anonymous(self):
        response = self.client.post(reverse('accounts:login'), {'username': 'alex', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        whoami = self.client.get(reverse('core:landing'))
        self.assertFalse(whoami.wsgi_request.user.is_authenticated)

    def test_logout_requires_post_and_deauthenticates(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('accounts:logout'))
        # core:landing redirects a logged-out, upload-less visitor onward to
        # accounts:login (now doubling as the entry gate) -- same reasoning as the
        # signup/login tests above.
        self.assertRedirects(response, reverse('core:landing'), target_status_code=302)
        whoami = self.client.get(reverse('core:landing'))
        self.assertFalse(whoami.wsgi_request.user.is_authenticated)
