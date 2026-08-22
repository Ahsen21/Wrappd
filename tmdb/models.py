from django.db import models


class Genre(models.Model):
    """A TMDB genre. TMDB genre ids are stable, so we use them directly as our PK."""

    tmdb_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Country(models.Model):
    """A production country. TMDB identifies these by ISO 3166-1 alpha-2 code, not a
    numeric id, so the code itself is the natural primary key."""

    code = models.CharField(max_length=2, primary_key=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Person(models.Model):
    """A director or cast member, keyed by TMDB's own person id."""

    tmdb_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=300)
    profile_path = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return self.name

    @property
    def profile_url(self):
        if not self.profile_path:
            return ''
        return f'https://image.tmdb.org/t/p/w185{self.profile_path}'


class Movie(models.Model):
    """
    The global TMDB metadata cache, shared across every import. A film watched
    by many different users is looked up from TMDB exactly once and reused here.
    """

    tmdb_id = models.PositiveIntegerField(primary_key=True)
    title = models.CharField(max_length=500)
    original_title = models.CharField(max_length=500, blank=True)
    release_year = models.PositiveSmallIntegerField(null=True, blank=True)
    runtime_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    poster_path = models.CharField(max_length=300, blank=True)
    overview = models.TextField(blank=True)
    tmdb_rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    # Human-readable name (e.g. "English"), resolved from TMDB's spoken_languages list
    # during enrichment since original_language on its own is just an ISO 639-1 code.
    original_language = models.CharField(max_length=100, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    genres = models.ManyToManyField(Genre, related_name='movies', blank=True)
    countries = models.ManyToManyField(Country, related_name='movies', blank=True)
    # Many-to-many, not a single FK -- a film can have co-directors (e.g. most Coen
    # Brothers films list both Joel and Ethan in TMDB's crew), and crediting only one
    # of them silently dropped the film from the other's stats entirely.
    directors = models.ManyToManyField(Person, related_name='directed_movies', blank=True)
    cast_members = models.ManyToManyField(Person, through='Credit', related_name='acted_in')

    def __str__(self):
        return f'{self.title} ({self.release_year})'

    @property
    def poster_url(self):
        if not self.poster_path:
            return ''
        return f'https://image.tmdb.org/t/p/w342{self.poster_path}'


class Credit(models.Model):
    """Through table for cast, so billing order is preserved for 'top cast' display."""

    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    character_name = models.CharField(max_length=300, blank=True)
    order = models.PositiveSmallIntegerField()

    class Meta:
        unique_together = ('movie', 'person')
        ordering = ['order']

    def __str__(self):
        return f'{self.person} as {self.character_name} in {self.movie}'


class TitleYearLookup(models.Model):
    """
    Cache of (title, year) -> resolved TMDB movie, checked before ever calling
    TMDB's search endpoint. A row with movie=None means "searched, no match found"
    so we don't repeat a wasted API call for a title TMDB doesn't have.
    """

    title = models.CharField(max_length=500, db_index=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    movie = models.ForeignKey(Movie, null=True, blank=True, on_delete=models.CASCADE)
    # True only when a movie search came up empty AND a follow-up TV search confirmed
    # a match -- i.e. we *know* this is TV (Letterboxd lets people log some TV content
    # alongside films), not just "TMDB doesn't have a movie by this name for some
    # other reason" (rate limit, obscure title, typo). Only entries confirmed this way
    # get excluded from stats; a plain unmatched title does not.
    is_tv_show = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('title', 'year')

    def __str__(self):
        return f'{self.title} ({self.year}) -> {self.movie_id or "no match"}'
