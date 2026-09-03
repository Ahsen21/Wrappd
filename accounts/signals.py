from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=get_user_model())
def create_profile_for_new_user(sender, instance, created, **kwargs):
    """Guarantees every User gets a Profile, regardless of how the account was
    created -- SignUpView is the normal path, but this also covers
    createsuperuser and any account made directly through the admin, neither of
    which go through SignUpView."""
    if created:
        Profile.objects.get_or_create(user=instance)
