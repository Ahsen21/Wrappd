"""One-time backfill for Movie.vote_count -- see that field's own comment in
tmdb/models.py for why it exists (feeds the "hidden gem" insight). Every Movie
enriched from here on gets vote_count for free (see enrichment.py's
_populate_details, which now captures it alongside tmdb_rating), but rows created
before that field existed each need one extra TMDB call to fill it in.

Safe to re-run: only ever targets rows still missing the field, so an interrupted
run (rate-limited, network hiccup, Ctrl-C) just picks up where it left off next
time rather than redoing work or double-counting.
"""

from django.core.management.base import BaseCommand

from tmdb.models import Movie
from tmdb.services.client import TMDBClientError, get_movie_details


class Command(BaseCommand):
    help = 'Backfill Movie.vote_count for rows enriched before that field existed.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Only backfill this many movies -- useful for a small test run before letting it run to completion.',
        )

    def handle(self, *args, **options):
        # Snapshot the id list up front rather than iterating the queryset live --
        # each movie gets updated individually below, and re-querying a filtered
        # queryset while rows are actively leaving that filter is exactly the kind
        # of "which cursor sees what" ambiguity worth just not risking.
        tmdb_ids = list(Movie.objects.filter(vote_count__isnull=True).values_list('tmdb_id', flat=True))
        if options['limit']:
            tmdb_ids = tmdb_ids[:options['limit']]
        total = len(tmdb_ids)
        if not total:
            self.stdout.write('Nothing to backfill -- every movie already has a vote_count.')
            return

        self.stdout.write(f'Backfilling vote_count for {total} movie(s)...')
        updated = 0
        failed = 0
        for i, tmdb_id in enumerate(tmdb_ids, start=1):
            try:
                details = get_movie_details(tmdb_id)
            except TMDBClientError as exc:
                # Left for a future run (still vote_count=None, so still matches
                # the filter above next time) -- one unreachable film shouldn't
                # abort backfilling the other thousand.
                failed += 1
                self.stderr.write(f'  [{i}/{total}] tmdb_id={tmdb_id}: {exc}')
                continue
            Movie.objects.filter(tmdb_id=tmdb_id).update(vote_count=details.get('vote_count'))
            updated += 1
            if i % 100 == 0:
                self.stdout.write(f'  ...{i}/{total}')

        self.stdout.write(
            self.style.SUCCESS(f'Done: {updated} updated, {failed} failed (safe to re-run to pick those up).')
        )
