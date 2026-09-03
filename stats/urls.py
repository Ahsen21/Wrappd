from django.urls import path, re_path

from . import views

app_name = 'stats'

urlpatterns = [
    path('dashboard/<uuid:session_id>/', views.dashboard, name='dashboard'),
    # Listed after the uuid pattern above -- Django tries patterns in order, and the
    # uuid converter only matches strict UUID-shaped strings, so a real username
    # falls through to this one automatically. (An adversarial username that's
    # itself valid UUID syntax would be misrouted, but that's not a case worth
    # guarding against for this app.)
    path('dashboard/<str:username>/', views.dashboard_by_username, name='dashboard_by_username'),
    path('dashboard/<uuid:session_id>/person/<int:tmdb_id>/', views.person_filmography, name='person_filmography'),
    path('compare/<uuid:session_a>/<uuid:session_b>/', views.compare, name='compare'),
    # A single path segment containing "-vs-", not two segments joined by "/" like
    # the uuid route above -- different enough in shape that there's no ordering
    # conflict between the two, unlike dashboard's pair. The character class matches
    # Django's own UnicodeUsernameValidator (letters, digits, . @ + - _), so it
    # accepts anything a real username could actually be.
    re_path(
        r'^compare/(?P<username_a>[\w.@+-]+)-vs-(?P<username_b>[\w.@+-]+)/$',
        views.compare_by_usernames, name='compare_by_usernames',
    ),
]
