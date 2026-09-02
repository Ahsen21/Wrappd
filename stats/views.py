from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render

from imports.models import ImportSession
from tmdb.models import Person

from .services.compare import build_compare_context
from .services.dashboard import build_dashboard_context
from .services.person_filmography import build_person_filmography


def dashboard(request, session_id):
    import_session = get_object_or_404(ImportSession, id=session_id)
    context = build_dashboard_context(import_session)
    # This page's own URL doubles as its share link (see the dashboard's "Share your
    # dashboard" box) -- the view has no ownership check tying it to this browser's
    # session, so anyone holding the link can already open it as-is.
    context['share_url'] = request.build_absolute_uri()
    return render(request, 'stats/dashboard.html', context)


def person_filmography(request, session_id, tmdb_id):
    import_session = get_object_or_404(ImportSession, id=session_id)
    role = request.GET.get('role')
    if role not in ('director', 'actor'):
        return JsonResponse({'error': 'role must be "director" or "actor"'}, status=400)
    person = get_object_or_404(Person, pk=tmdb_id)
    return JsonResponse(build_person_filmography(import_session, person, role))


def compare(request, session_a, session_b):
    session_a_obj = get_object_or_404(ImportSession, id=session_a)
    session_b_obj = get_object_or_404(ImportSession, id=session_b)
    context = build_compare_context(session_a_obj, session_b_obj)
    return render(request, 'stats/compare.html', context)
