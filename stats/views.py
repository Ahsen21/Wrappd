from django.shortcuts import get_object_or_404, render

from imports.models import ImportSession

from .services.compare import build_compare_context
from .services.dashboard import build_dashboard_context


def dashboard(request, session_id):
    import_session = get_object_or_404(ImportSession, id=session_id)
    context = build_dashboard_context(import_session)
    return render(request, 'stats/dashboard.html', context)


def compare(request, session_a, session_b):
    session_a_obj = get_object_or_404(ImportSession, id=session_a)
    session_b_obj = get_object_or_404(ImportSession, id=session_b)
    context = build_compare_context(session_a_obj, session_b_obj)
    return render(request, 'stats/compare.html', context)
