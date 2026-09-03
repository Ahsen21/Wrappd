from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView

from imports.models import ImportSession

from .forms import ProfileSettingsForm, SignUpForm


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
        # self.object (the new User) triggers the post_save signal during
        # super().form_valid()'s form.save() call, which auto-creates its Profile
        # with the default is_searchable=True -- overwrite it with what they
        # actually chose on the signup form.
        self.object.profile.is_searchable = form.cleaned_data['is_searchable']
        self.object.profile.save(update_fields=['is_searchable'])
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


class AccountView(LoginRequiredMixin, View):
    """The consolidated "Account" page: the searchable/private setting (editable
    copy of the same choice made once at signup), and the uploads list that used to
    be its own separate "Previous uploads" page (imports.MyUploadsView) -- folded in
    here since both are account-level concerns, so the nav's profile dropdown only
    needs to link here (plus Log out) instead of one entry per action."""

    template_name = 'accounts/account.html'

    def _context(self, request, form=None):
        sessions = list(ImportSession.objects.filter(
            owner=request.user, status=ImportSession.Status.READY
        ).order_by('-uploaded_at'))
        # The most recent is always what ImportSession.ready_for would return for
        # this account -- reuse it instead of a second query (same reasoning
        # MyUploadsView used to apply).
        current = sessions[0] if sessions else None
        return {
            'form': form or ProfileSettingsForm(instance=request.user.profile),
            'sessions': sessions,
            'current': current,
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        form = ProfileSettingsForm(request.POST, instance=request.user.profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings saved.')
            return redirect('accounts:account')
        return render(request, self.template_name, self._context(request, form=form))
