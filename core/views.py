from django.shortcuts import redirect, render

from imports.models import ImportSession


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
        return render(request, 'core/landing.html', {'my_session': my_session})
    if request.user.is_authenticated:
        return redirect('imports:upload')
    return redirect('accounts:login')
