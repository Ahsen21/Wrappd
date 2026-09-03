from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render

from imports.models import ImportSession
from tmdb.models import Person

from .services.compare import build_compare_context
from .services.dashboard import build_dashboard_context
from .services.person_filmography import build_person_filmography


def _render_dashboard(request, import_session):
    context = build_dashboard_context(import_session)
    # The dashboard's own URL doubles as its share link (see the "Share your
    # dashboard" box) -- neither route this can be reached by has an ownership
    # check tying it to this browser's session, so anyone holding either kind of
    # link can already open it as is. canonical_dashboard_path prefers the
    # account's permanent /dashboard/<username>/ link when there is one, even if
    # this particular request came in on the raw UUID route.
    context['share_url'] = request.build_absolute_uri(import_session.canonical_dashboard_path())
    return render(request, 'stats/dashboard.html', context)


def dashboard(request, session_id):
    # select_related('owner') -- _share_url reads import_session.owner.username for
    # every account-owned session, not just when explicitly asked for.
    import_session = get_object_or_404(ImportSession.objects.select_related('owner'), id=session_id)
    return _render_dashboard(request, import_session)


def dashboard_by_username(request, username):
    import_session = ImportSession.latest_for_owner_username(username)
    if import_session is None:
        raise Http404("This account doesn't have a finished upload yet.")
    return _render_dashboard(request, import_session)


def person_filmography(request, session_id, tmdb_id):
    import_session = get_object_or_404(ImportSession, id=session_id)
    role = request.GET.get('role')
    if role not in ('director', 'actor'):
        return JsonResponse({'error': 'role must be "director" or "actor"'}, status=400)
    person = get_object_or_404(Person, pk=tmdb_id)
    return JsonResponse(build_person_filmography(import_session, person, role))


def _render_compare(request, session_a_obj, session_b_obj):
    context = build_compare_context(session_a_obj, session_b_obj)
    return render(request, 'stats/compare.html', context)


def compare(request, session_a, session_b):
    session_a_obj = get_object_or_404(ImportSession.objects.select_related('owner'), id=session_a)
    session_b_obj = get_object_or_404(ImportSession.objects.select_related('owner'), id=session_b)
    return _render_compare(request, session_a_obj, session_b_obj)


def compare_by_usernames(request, username_a, username_b):
    session_a_obj = ImportSession.latest_for_owner_username(username_a)
    session_b_obj = ImportSession.latest_for_owner_username(username_b)
    if session_a_obj is None or session_b_obj is None:
        raise Http404("One of these accounts doesn't have a finished upload yet.")
    return _render_compare(request, session_a_obj, session_b_obj)
