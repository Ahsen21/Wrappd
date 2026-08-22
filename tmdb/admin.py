from django.contrib import admin

from .models import Country, Credit, Genre, Movie, Person, TitleYearLookup


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ('name', 'tmdb_id')
    search_fields = ('name',)


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ('name', 'code')
    search_fields = ('name', 'code')


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ('name', 'tmdb_id')
    search_fields = ('name',)


class CreditInline(admin.TabularInline):
    model = Credit
    extra = 0


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = ('title', 'release_year', 'director_names', 'runtime_minutes', 'original_language', 'tmdb_id', 'fetched_at')
    search_fields = ('title', 'original_title')
    list_filter = ('genres', 'countries')
    inlines = [CreditInline]

    @admin.display(description='Director(s)')
    def director_names(self, obj):
        return ', '.join(p.name for p in obj.directors.all())


@admin.register(TitleYearLookup)
class TitleYearLookupAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'movie', 'is_tv_show', 'resolved_at')
    search_fields = ('title',)
    list_filter = ('resolved_at', 'is_tv_show')
