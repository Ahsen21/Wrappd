from django.http import JsonResponse
from django.shortcuts import redirect, render

from imports.models import ImportSession
from stats.services.dashboard import home_summary, on_this_day as on_this_day_films


def landing(request):
    """"/" doubles as the app's entry-flow router:

    - a completed upload already exists (ImportSession.ready_for) -> the home
      screen (Director's Cut / Double Feature choice), Director's Cut linking
      straight to that dashboard instead of back through upload.
    - logged in but nothing uploaded yet -> straight to the upload step (no need to
      ask them to choose an identity again).
    - nobody recognized yet -> accounts:login, which doubles as the entry gate (its
      own template leads with the login form, then "New user? Sign up", then
      continue as guest -- see accounts/templates/accounts/login.html).

    There's no "skip upload" branch -- every feature the app has requires one, so a
    bypass would just dead-end back at this same upload step anyway."""
    my_session = ImportSession.ready_for(request)
    if my_session:
        context = {
            'my_session': my_session, 'my_session_url': my_session.canonical_dashboard_path(),
            **home_summary(my_session),
        }
        return render(request, 'core/landing.html', context)
    if request.user.is_authenticated:
        return redirect('imports:upload')
    return redirect('accounts:login')


def on_this_day(request):
    """JSON behind the home screen's "On this day" card. The browser sends its own
    local month/day/year (?m=&d=&y=) so "today" is the visitor's today, not the
    server's (UTC). Only ever the requester's own diary -- no id in the URL to swap."""
    my_session = ImportSession.ready_for(request)
    try:
        month, day, year = (int(request.GET[key]) for key in ('m', 'd', 'y'))
    except (KeyError, ValueError):
        return JsonResponse({'error': 'm, d and y must be integers'}, status=400)
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2200):
        return JsonResponse({'error': 'date out of range'}, status=400)
    if my_session is None:
        return JsonResponse({'films': []})
    return JsonResponse({'films': on_this_day_films(my_session, month, day, year)})
