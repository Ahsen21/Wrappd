from django.contrib import admin

from .models import DiaryEntry, ImportSession, LikedFilmEntry, RatingEntry, WatchedEntry, WatchlistEntry


class DiaryEntryInline(admin.TabularInline):
    model = DiaryEntry
    extra = 0
    fields = ('title', 'year', 'watched_date', 'rating', 'rewatch', 'movie')


@admin.register(ImportSession)
class ImportSessionAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'id', 'owner', 'status', 'source_filename', 'uploaded_at')
    list_filter = ('status',)
    autocomplete_fields = ('owner',)
    readonly_fields = ('id', 'uploaded_at')
    inlines = [DiaryEntryInline]


@admin.register(DiaryEntry)
class DiaryEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'watched_date', 'rating', 'rewatch', 'import_session', 'movie')
    list_filter = ('rewatch',)
    search_fields = ('title',)


@admin.register(RatingEntry)
class RatingEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'rating', 'import_session', 'movie')
    search_fields = ('title',)


@admin.register(WatchlistEntry)
class WatchlistEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'added_date', 'import_session', 'movie')
    search_fields = ('title',)


@admin.register(LikedFilmEntry)
class LikedFilmEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'date_liked', 'import_session', 'movie')
    search_fields = ('title',)


@admin.register(WatchedEntry)
class WatchedEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'import_session', 'movie')
    search_fields = ('title',)
