import uuid

from django.db import models

from tmdb.models import Movie


class ImportSession(models.Model):
    """
    One uploaded Letterboxd export. Anonymous for now: ownership is tracked via the
    browser's Django session key, not a user account.

    TODO(auth): when real accounts are added, add:
        owner = models.ForeignKey(
            settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
        )
    This is additive only -- existing rows stay owner=NULL and keep resolving via
    session_key. Every other model in this app FKs to ImportSession, never directly
    to a user, so nothing downstream needs to change.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PARSING = 'PARSING', 'Parsing'
        ENRICHING = 'ENRICHING', 'Enriching'
        READY = 'READY', 'Ready'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_key = models.CharField(max_length=40, db_index=True, blank=True)
    display_name = models.CharField(max_length=150, blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # From profile.csv's "Favorite Films" column: an ordered list of up to 4 boxd.it
    # Letterboxd URI strings. Resolved to titles/posters at display time by matching
    # against this session's other entry models -- see stats/services/dashboard.py.
    favorite_letterboxd_uris = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return self.display_name or f'Import {self.id}'


class DiaryEntry(models.Model):
    """One row from diary.csv -- the richest source, includes an explicit watched date."""

    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE, related_name='diary_entries')
    letterboxd_uri = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    watched_date = models.DateField()
    rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    rewatch = models.BooleanField(default=False)
    tags = models.CharField(max_length=500, blank=True)
    movie = models.ForeignKey(Movie, null=True, blank=True, on_delete=models.SET_NULL, related_name='diary_entries')

    class Meta:
        unique_together = ('import_session', 'letterboxd_uri', 'watched_date')
        indexes = [models.Index(fields=['import_session', 'watched_date'])]

    def __str__(self):
        return f'{self.title} ({self.year}) - {self.watched_date}'


class RatingEntry(models.Model):
    """One row from ratings.csv -- the authoritative current rating for a film."""

    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE, related_name='rating_entries')
    letterboxd_uri = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    rating = models.DecimalField(max_digits=2, decimal_places=1)
    movie = models.ForeignKey(Movie, null=True, blank=True, on_delete=models.SET_NULL, related_name='rating_entries')

    class Meta:
        unique_together = ('import_session', 'letterboxd_uri')

    def __str__(self):
        return f'{self.title} ({self.year}) - {self.rating}'


class WatchlistEntry(models.Model):
    """One row from watchlist.csv -- a film the user wants to watch."""

    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE, related_name='watchlist_entries')
    letterboxd_uri = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    added_date = models.DateField(null=True, blank=True)
    movie = models.ForeignKey(
        Movie, null=True, blank=True, on_delete=models.SET_NULL, related_name='watchlist_entries'
    )

    class Meta:
        unique_together = ('import_session', 'letterboxd_uri')

    def __str__(self):
        return f'{self.title} ({self.year})'


class WatchedEntry(models.Model):
    """One row from watched.csv -- every film ever marked watched, logged or not.
    This is the superset that diary.csv (dated log entries only) sits inside."""

    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE, related_name='watched_entries')
    letterboxd_uri = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    movie = models.ForeignKey(Movie, null=True, blank=True, on_delete=models.SET_NULL, related_name='watched_entries')

    class Meta:
        unique_together = ('import_session', 'letterboxd_uri')

    def __str__(self):
        return f'{self.title} ({self.year})'


class LikedFilmEntry(models.Model):
    """One row from likes/films.csv."""

    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE, related_name='liked_film_entries')
    letterboxd_uri = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    date_liked = models.DateField(null=True, blank=True)
    movie = models.ForeignKey(
        Movie, null=True, blank=True, on_delete=models.SET_NULL, related_name='liked_film_entries'
    )

    class Meta:
        unique_together = ('import_session', 'letterboxd_uri')

    def __str__(self):
        return f'{self.title} ({self.year})'


class ReviewEntry(models.Model):
    """One row from reviews.csv -- a diary entry the user wrote a review for. A
    separate file from diary.csv in a Letterboxd export (same log-entry shape, plus
    the review text), not every diary entry has a matching row here."""

    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE, related_name='review_entries')
    letterboxd_uri = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    watched_date = models.DateField(null=True, blank=True)
    rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    review = models.TextField(blank=True)
    movie = models.ForeignKey(Movie, null=True, blank=True, on_delete=models.SET_NULL, related_name='review_entries')

    class Meta:
        unique_together = ('import_session', 'letterboxd_uri', 'watched_date')

    def __str__(self):
        return f'{self.title} ({self.year}) - review'
