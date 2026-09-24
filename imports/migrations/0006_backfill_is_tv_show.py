from django.db import migrations

# Every entry model that carries (title, year) and can reference a confirmed-TV
# TitleYearLookup row -- same set enrichment.py's ENTRY_MODELS covers.
ENTRY_MODEL_NAMES = (
    'DiaryEntry', 'RatingEntry', 'WatchlistEntry', 'LikedFilmEntry', 'WatchedEntry', 'ReviewEntry',
)


def backfill_is_tv_show(apps, schema_editor):
    """One-time backfill for data enriched before is_tv_show existed on entry rows
    -- sets it from whatever TitleYearLookup already recorded, so exclude_tv_shows()
    (now a plain field filter, see stats/services/filters.py) keeps excluding exactly
    what it always has. Only ever a small number of (title, year) pairs in practice
    (confirmed TV is rare relative to films), so a query per pair is fine here."""
    TitleYearLookup = apps.get_model('tmdb', 'TitleYearLookup')
    tv_pairs = list(TitleYearLookup.objects.filter(is_tv_show=True).values_list('title', 'year'))

    for model_name in ENTRY_MODEL_NAMES:
        Model = apps.get_model('imports', model_name)
        for title, year in tv_pairs:
            Model.objects.filter(title=title, year=year, movie__isnull=True).update(is_tv_show=True)


def noop_reverse(apps, schema_editor):
    """Not worth reversing -- is_tv_show reverts to its default (False) when the
    AddField migrations themselves are unapplied, which is the only case this would
    ever run under."""


class Migration(migrations.Migration):

    dependencies = [
        ('imports', '0005_diaryentry_is_tv_show_likedfilmentry_is_tv_show_and_more'),
        ('tmdb', '0005_movie_vote_count'),
    ]

    operations = [
        migrations.RunPython(backfill_is_tv_show, noop_reverse),
    ]
