from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'is_searchable')
    list_filter = ('is_searchable',)
    search_fields = ('user__username',)
