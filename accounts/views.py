from django.contrib.auth import login
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import CreateView

from .forms import SignUpForm


class SignUpView(CreateView):
    """Creates the account and logs the user straight in -- no separate "now go log
    in" step. Honors ?next= the same way LoginView does, so a signup reached via a
    login-required redirect (e.g. from Double Feature) lands back where they were
    headed instead of the generic landing page."""

    form_class = SignUpForm
    template_name = 'accounts/signup.html'
    success_url = reverse_lazy('core:landing')

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

    def get_success_url(self):
        next_url = self.request.POST.get('next') or self.request.GET.get('next')
        # Same open-redirect guard LoginView applies to its own "next" -- a signup
        # reached via a login-required bounce carries this from the query string, so
        # it can't be trusted without validating it points back at this site.
        if next_url and url_has_allowed_host_and_scheme(
            url=next_url, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()
        ):
            return next_url
        return str(self.success_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['next'] = self.request.GET.get('next', '')
        return context
