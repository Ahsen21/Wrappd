import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from tmdb.services.enrichment import enrich_import_session_fully

from .forms import CompareUploadForm, JoinCompareForm, SearchFriendForm, UploadForm
from .models import ImportSession, canonical_compare_path
from .services.parser import ExportParseError, parse_export, persist_parsed_export

logger = logging.getLogger(__name__)


def _session_key(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _process_upload(request, uploaded_file, zf, *, owner=None):
    """Create an ImportSession and run it through parse -> persist -> enrich.

    owner defaults to None (anonymous) rather than being inferred from request.user
    here, since one caller (CompareUploadView) needs to attribute only one of its two
    uploads to the logged-in submitter -- the second file is physically a friend's
    export handed over for convenience, not this account's own data."""
    import_session = ImportSession.objects.create(
        session_key=_session_key(request),
        owner=owner,
        source_filename=uploaded_file.name,
        status=ImportSession.Status.PARSING,
    )
    try:
        parsed = parse_export(zf)
        persist_parsed_export(import_session, parsed)
        import_session.status = ImportSession.Status.ENRICHING
        import_session.save(update_fields=['status'])
        enrich_import_session_fully(import_session)
        import_session.status = ImportSession.Status.READY
        import_session.save(update_fields=['status'])
    except ExportParseError as exc:
        import_session.status = ImportSession.Status.FAILED
        import_session.error_message = str(exc)
        import_session.save(update_fields=['status', 'error_message'])
        logger.warning('Import %s failed to parse: %s', import_session.id, exc)
    return import_session


class UploadView(View):
    """Director's Cut. Stays fully anonymous (no login required) -- but if this
    visitor already has a completed upload (e.g. the nav's "Director's Cut" link,
    clicked from anywhere else in the app), skip straight to their existing
    dashboard instead of showing an empty upload form again.

    ?new=1 bypasses that -- it's how the profile menu's "Upload a new export" reaches
    the actual form despite already having a session; every other link to this page
    wants the smart redirect, so this is the one deliberate exception rather than a
    behavior change to the view as a whole."""

    template_name = 'imports/upload.html'

    def get(self, request):
        my_session = ImportSession.ready_for(request)
        if my_session and not request.GET.get('new'):
            return redirect(my_session.canonical_dashboard_path())
        return render(request, self.template_name, {'form': UploadForm()})

    def post(self, request):
        form = UploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        # Login isn't required here -- Director's Cut stays anonymous -- but if the
        # uploader happens to be logged in anyway, attribute it to them for free.
        owner = request.user if request.user.is_authenticated else None
        import_session = _process_upload(request, request.FILES['export_file'], form.cleaned_zip, owner=owner)
        if import_session.status == ImportSession.Status.FAILED:
            form.add_error(None, import_session.error_message)
            return render(request, self.template_name, {'form': form})

        # core:landing is the entry-flow router (see core/views.py) -- now that this
        # upload exists, it'll render the home screen instead of bouncing back here.
        return redirect(reverse('core:landing'))


class CompareUploadView(LoginRequiredMixin, View):
    """The dual-upload alternative to CompareJoinView below -- both exports in one
    request, for two people uploading together at one computer. Login-gated like
    every Double Feature entry point, but only the first file is attributed to the
    logged-in submitter's account (session_a's owner); the second (session_b) stays
    anonymous, since it's physically a friend's export, not this account's own data."""

    template_name = 'imports/upload_compare.html'

    def get(self, request):
        return render(request, self.template_name, {'form': CompareUploadForm()})

    def post(self, request):
        form = CompareUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        session_a = _process_upload(request, request.FILES['export_file_a'], form.cleaned_zip_a, owner=request.user)
        session_b = _process_upload(request, request.FILES['export_file_b'], form.cleaned_zip_b)

        failed = [s for s in (session_a, session_b) if s.status == ImportSession.Status.FAILED]
        if failed:
            form.add_error(None, '; '.join(s.error_message for s in failed))
            return render(request, self.template_name, {'form': form})

        return redirect(canonical_compare_path(session_a, session_b))


class CompareJoinView(LoginRequiredMixin, View):
    """Two alternatives to CompareUploadView above, both letting each person upload
    independently (possibly on a separate visit) instead of both files landing in
    one request: search for a friend's Letterboxd username (SearchFriendForm,
    primary), or paste their dashboard link directly (JoinCompareForm, still useful
    for a private account -- search won't find it, but a direct link still works,
    same bearer-link model dashboards have always had). stats:compare already
    accepts any two session ids with no ownership check (compare links stay openly
    shareable), so this view's only job is resolving "my" session and the other one
    (by either path), then handing both to it.

    Login-gated (unlike CompareUploadView, this is the only place "my session" needs
    to be looked up implicitly rather than being handed both ids explicitly) because
    "my" has to mean something unambiguous: filtering by session_key alone can't
    tell apart several different people's exports uploaded from the same browser --
    ImportSession.ready_for resolves it by owner instead, since that's only ever set
    on uploads made while logged in as this specific account."""

    template_name = 'imports/compare_join.html'

    def get(self, request):
        my_session = ImportSession.ready_for(request)
        context = {
            'my_session': my_session,
            'upload_form': None if my_session else UploadForm(),
            'join_form': JoinCompareForm() if my_session else None,
            'search_form': SearchFriendForm() if my_session else None,
        }
        return render(request, self.template_name, context)

    def post(self, request):
        my_session = ImportSession.ready_for(request)

        if not my_session:
            upload_form = UploadForm(request.POST, request.FILES)
            if not upload_form.is_valid():
                return render(request, self.template_name, {
                    'my_session': None, 'upload_form': upload_form, 'join_form': None, 'search_form': None,
                })

            my_session = _process_upload(
                request, request.FILES['export_file'], upload_form.cleaned_zip, owner=request.user
            )
            if my_session.status == ImportSession.Status.FAILED:
                upload_form.add_error(None, my_session.error_message)
                return render(request, self.template_name, {
                    'my_session': None, 'upload_form': upload_form, 'join_form': None, 'search_form': None,
                })

            # Uploaded successfully -- show the search/friend-link step next rather
            # than requiring one in the same request, since they haven't given one yet.
            return render(request, self.template_name, {
                'my_session': my_session, 'upload_form': None,
                'join_form': JoinCompareForm(), 'search_form': SearchFriendForm(),
            })

        # Two separate <form>s on the page (search vs. paste-a-link) post here --
        # which one was actually submitted is told apart by which field shows up in
        # POST, same as the upload-vs-join branch above is told apart by my_session.
        if 'letterboxd_username' in request.POST:
            search_form = SearchFriendForm(request.POST)
            if not search_form.is_valid():
                return render(request, self.template_name, {
                    'my_session': my_session, 'upload_form': None,
                    'join_form': JoinCompareForm(), 'search_form': search_form,
                })
            friend_session = search_form.cleaned_friend_session
            return redirect(canonical_compare_path(my_session, friend_session))

        join_form = JoinCompareForm(request.POST)
        if not join_form.is_valid():
            return render(request, self.template_name, {
                'my_session': my_session, 'upload_form': None,
                'join_form': join_form, 'search_form': SearchFriendForm(),
            })

        friend_session = join_form.cleaned_friend_session
        return redirect(canonical_compare_path(my_session, friend_session))
