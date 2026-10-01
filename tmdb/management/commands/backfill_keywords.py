"""One-time backfill for Movie.keywords -- every Movie enriched from here on
gets keywords for free, but rows created before that field existed each need
one extra TMDB call to fill them in.

Safe to re-run: only targets rows with zero keywords, so an interrupted run
just picks up where it left off. A film that genuinely has no keywords on
TMDB will keep getting re-attempted (no way to distinguish "checked, none"
from "never checked" with a bare M2M), but that's a wasted call at worst.
"""

from django.core.management.base import BaseCommand

from tmdb.models import Keyword, Movie
from tmdb.services.client import TMDBClientError, get_movie_details


class Command(BaseCommand):
    help = 'Backfill Movie.keywords for rows enriched before that field existed.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Only backfill this many movies -- useful for a small test run before letting it run to completion.',
        )

    def handle(self, *args, **options):
        tmdb_ids = list(Movie.objects.filter(keywords__isnull=True).values_list('tmdb_id', flat=True).distinct())
        if options['limit']:
            tmdb_ids = tmdb_ids[:options['limit']]
        total = len(tmdb_ids)
        if not total:
            self.stdout.write('Nothing to backfill -- every movie already has keywords.')
            return

        self.stdout.write(f'Backfilling keywords for {total} movie(s)...')
        updated = 0
        failed = 0
        for i, tmdb_id in enumerate(tmdb_ids, start=1):
            try:
                details = get_movie_details(tmdb_id)
            except TMDBClientError as exc:
                failed += 1
                self.stderr.write(f'  [{i}/{total}] tmdb_id={tmdb_id}: {exc}')
                continue
            keywords = []
            for keyword_data in details.get('keywords', {}).get('keywords', []):
                keyword, _ = Keyword.objects.get_or_create(
                    tmdb_id=keyword_data['id'], defaults={'name': keyword_data['name']},
                )
                keywords.append(keyword)
            Movie.objects.get(tmdb_id=tmdb_id).keywords.set(keywords)
            updated += 1
            if i % 100 == 0:
                self.stdout.write(f'  ...{i}/{total}')

        self.stdout.write(
            self.style.SUCCESS(f'Done: {updated} updated, {failed} failed (safe to re-run to pick those up).')
        )
