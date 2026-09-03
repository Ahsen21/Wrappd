from django.conf import settings
from django.db import models


class Profile(models.Model):
    """One-to-one account-level settings that don't belong on Django's own User
    model -- it can't be extended with a new field in place this late without a
    much riskier full AUTH_USER_MODEL swap, since real data already rests on the
    default User (ImportSession.owner, existing accounts). Auto-created for every
    User via the post_save signal in accounts/signals.py (wired up in
    AccountsConfig.ready()), so every user is guaranteed to have one -- no need to
    .get_or_create defensively at each call site."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    # Whether this account can be found via Double Feature's "search for a friend's
    # Letterboxd username" lookup (see ImportSession.latest_for_letterboxd_username).
    # Defaults to searchable, matching Letterboxd's own default posture (most
    # profiles there are public) -- opting into privacy is the deliberate action,
    # not opting into visibility.
    is_searchable = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.user.username} profile'
