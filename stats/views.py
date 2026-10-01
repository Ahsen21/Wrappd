from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from imports.models import ImportSession
from tmdb.models import Person

from .services.compare import build_compare_context
from .services.dashboard import build_dashboard_context
from .services.insight_films import VALID_KINDS, build_insight_films
from .services.person_filmography import build_person_filmography

# build_dashboard_context is expensive (dozens of queries, real wall-clock cost on
# Render's free-tier CPU), but a READY session's data never mutates in place, and
# a fresh upload gets a brand-new ImportSession id -- so there's no explicit
# invalidation to do. The TTL below is a safety net, not the reason this is safe.
DASHBOARD_CONTEXT_CACHE_TTL = 3600  # seconds


def _dashboard_cache_key(import_session, exclude_shorts, year):
    return f'dashboard_context:{import_session.id}:{exclude_shorts}:{year}'


def _dashboard_query_params(request):
    # ?shorts=exclude opts OUT of short films (under 60 min) across every stat on
    # the page -- default stays "include" so a plain dashboard link/share never
    # silently shows different numbers. Server-computed, so the template's toggle
    # reloads with this param rather than filtering client-side.
    exclude_shorts = request.GET.get('shorts') == 'exclude'
    # ?year=2024 switches to the "Wrapped for a single year" view (see
    # build_dashboard_context's docstring) -- absent/invalid falls back to the
    # all-time page rather than erroring.
    year_param = request.GET.get('year')
    try:
        year = int(year_param) if year_param else None
    except ValueError:
        year = None
    return exclude_shorts, year


def _dashboard_query_string(exclude_shorts, year):
    params = []
    if exclude_shorts:
        params.append('shorts=exclude')
    if year is not None:
        params.append(f'year={year}')
    return ('?' + '&'.join(params)) if params else ''


def _render_dashboard_shell(request, import_session):
    # The real content (dozens of queries, real wall-clock cost on Render's
    # free-tier CPU/cold start) is fetched by the shell's own JS after this
    # renders, not computed here -- this view must stay cheap so a cold instance
    # or an uncached dashboard shows a loading transition instead of hanging on
    # a blank tab. See dashboard_content below and stats/dashboard.html.
    exclude_shorts, year = _dashboard_query_params(request)
    content_url = reverse('stats:dashboard_content', kwargs={'session_id': import_session.id})
    content_url += _dashboard_query_string(exclude_shorts, year)
    return render(request, 'stats/dashboard.html', {'import_session': import_session, 'content_url': content_url})


def dashboard(request, session_id):
    import_session = get_object_or_404(ImportSession, id=session_id)
    return _render_dashboard_shell(request, import_session)


def dashboard_by_username(request, username):
    import_session = ImportSession.latest_for_owner_username(username)
    if import_session is None:
        raise Http404("This account doesn't have a finished upload yet.")
    return _render_dashboard_shell(request, import_session)


def dashboard_content(request, session_id):
    # select_related('owner') -- share_url reads import_session.owner.username for
    # every account-owned session, not just when explicitly asked for.
    import_session = get_object_or_404(ImportSession.objects.select_related('owner'), id=session_id)
    exclude_shorts, year = _dashboard_query_params(request)
    cache_key = _dashboard_cache_key(import_session, exclude_shorts, year)
    context = cache.get(cache_key)
    if context is None:
        context = build_dashboard_context(import_session, exclude_shorts, year)
        cache.set(cache_key, context, DASHBOARD_CONTEXT_CACHE_TTL)
    # The dashboard's own URL doubles as its share link -- neither route it can be
    # reached by has an ownership check, so anyone holding either link can already
    # open it. canonical_dashboard_path prefers the permanent /dashboard/<username>/
    # link when there is one. Carries the shorts/year toggles' current state along
    # so sharing a filtered view doesn't silently reset to the default. dashboard_path
    # (the same canonical path, relative) is what the in-page shorts/year toggle
    # links are built from -- this view's own request.path is the /content/ endpoint,
    # not the page the browser is actually showing.
    dashboard_path = import_session.canonical_dashboard_path()
    share_url = request.build_absolute_uri(dashboard_path) + _dashboard_query_string(exclude_shorts, year)
    context = {**context, 'share_url': share_url, 'dashboard_path': dashboard_path}
    return render(request, 'stats/_dashboard_content.html', context)


def person_filmography(request, session_id, tmdb_id):
    import_session = get_object_or_404(ImportSession, id=session_id)
    role = request.GET.get('role')
    if role not in ('director', 'actor'):
        return JsonResponse({'error': 'role must be "director" or "actor"'}, status=400)
    person = get_object_or_404(Person, pk=tmdb_id)
    # Same "don't trust the query string" posture as _render_dashboard's own
    # ?year= handling -- an absent/invalid value just falls back to the
    # all-time filmography rather than erroring.
    year_param = request.GET.get('year')
    try:
        year = int(year_param) if year_param else None
    except ValueError:
        year = None
    return JsonResponse(build_person_filmography(import_session, person, role, year))


def insight_films(request, session_id):
    # Same JSON-for-a-modal shape as person_filmography above -- backs the
    # click-through on the duo / genre-combo / decade insight tiles.
    import_session = get_object_or_404(ImportSession, id=session_id)
    kind = request.GET.get('kind')
    if kind not in VALID_KINDS:
        return JsonResponse({'error': f'kind must be one of {VALID_KINDS}'}, status=400)
    try:
        data = build_insight_films(import_session, kind, request.GET['p1'], request.GET.get('p2', ''))
    except (KeyError, ValueError):
        return JsonResponse({'error': 'bad or missing params'}, status=400)
    return JsonResponse(data)


def _render_compare(request, session_a_obj, session_b_obj):
    # See _render_dashboard's own comment on this same param.
    exclude_shorts = request.GET.get('shorts') == 'exclude'
    context = build_compare_context(session_a_obj, session_b_obj, exclude_shorts)
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
