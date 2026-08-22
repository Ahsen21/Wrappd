from decimal import Decimal

from django.test import TestCase

from imports.models import (
    DiaryEntry,
    ImportSession,
    LikedFilmEntry,
    RatingEntry,
    ReviewEntry,
    WatchedEntry,
    WatchlistEntry,
)
from stats.services.compare import build_compare_context
from stats.services.dashboard import build_dashboard_context
from tmdb.models import Country, Credit, Genre, Movie, Person, TitleYearLookup


def _make_movie(tmdb_id, title, year, runtime, genre_name, director_name=None):
    movie = Movie.objects.create(tmdb_id=tmdb_id, title=title, release_year=year, runtime_minutes=runtime)
    genre, _ = Genre.objects.get_or_create(tmdb_id=hash(genre_name) % 10_000, defaults={'name': genre_name})
    movie.genres.add(genre)
    if director_name:
        director, _ = Person.objects.get_or_create(tmdb_id=hash(director_name) % 10_000, defaults={'name': director_name})
        movie.directors.add(director)
    return movie


def _cast_movie(movie, person, order, total_cast_size):
    """Credits `person` in `movie` at billing `order`, padding the rest of the cast
    with filler Person rows up to `total_cast_size` so cameo-threshold tests can
    control cast size precisely."""
    Credit.objects.create(movie=movie, person=person, order=order)
    for i in range(total_cast_size - 1):
        filler, _ = Person.objects.get_or_create(
            tmdb_id=movie.tmdb_id * 100_000 + i, defaults={'name': f'Filler {movie.tmdb_id}-{i}'}
        )
        Credit.objects.create(movie=movie, person=filler, order=1000 + i)


class BuildDashboardContextTests(TestCase):
    def setUp(self):
        self.session = ImportSession.objects.create(display_name='Alex')
        movie_a = _make_movie(1, 'Film A', 2024, 120, 'Drama', 'Director X')
        movie_b = _make_movie(2, 'Film B', 2023, 90, 'Comedy')

        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2024,
            watched_date='2024-01-01', rating=Decimal('4.5'), rewatch=False, movie=movie_a,
        )
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/b', title='Film B', year=2023,
            watched_date='2023-05-01', rating=Decimal('3.5'), rewatch=True, movie=movie_b,
        )

    def test_basic_counts_and_average(self):
        context = build_dashboard_context(self.session)
        self.assertEqual(context['total_films'], 2)
        self.assertEqual(context['rewatch_count'], 1)
        # avg_rating is sourced from RatingEntry (ratings.csv), not diary -- this
        # fixture has no RatingEntry rows at all, so it's None rather than a
        # diary-derived (rewatch-weighted) average.
        self.assertIsNone(context['avg_rating'])
        self.assertEqual(context['total_runtime_minutes'], 210)
        self.assertEqual(context['total_runtime_hours'], 4)  # 210 min = 3.5h, rounded to a whole number
        self.assertEqual(context['unenriched_count'], 0)

    def test_top_genres_and_directors(self):
        context = build_dashboard_context(self.session)
        genre_names = {row['genres__name'] for row in context['top_genres']}
        self.assertEqual(genre_names, {'Drama', 'Comedy'})
        self.assertEqual(context['top_directors'][0]['directors__name'], 'Director X')
        # "Most watched" avg rating is joined in from RatingEntry separately from the
        # watched-film count. No RatingEntry rows exist in this fixture at all, so
        # Director X's avg_rating is None rather than a single-film "average".
        self.assertIsNone(context['top_directors'][0]['avg_rating'])

    def test_chart_data_films_per_year(self):
        context = build_dashboard_context(self.session)
        self.assertEqual(context['chart_data']['films_per_year']['labels'], ['2023', '2024'])
        self.assertEqual(context['chart_data']['films_per_year']['data'], [1, 1])


class DashboardNewStatsTests(TestCase):
    """Covers the 'beyond the basics' stat blocks: taste vs. crowd, rating by
    genre/decade, rewatch leaderboard, viewing calendar, and favorite directors/actors."""

    def setUp(self):
        self.session = ImportSession.objects.create(display_name='Alex')

        drama, _ = Genre.objects.get_or_create(tmdb_id=1, defaults={'name': 'Drama'})
        comedy, _ = Genre.objects.get_or_create(tmdb_id=2, defaults={'name': 'Comedy'})
        director_x, _ = Person.objects.get_or_create(tmdb_id=901, defaults={'name': 'Dir X'})
        director_y, _ = Person.objects.get_or_create(tmdb_id=902, defaults={'name': 'Dir Y'})
        actor_zed, _ = Person.objects.get_or_create(tmdb_id=911, defaults={'name': 'Actor Zed'})
        actor_solo, _ = Person.objects.get_or_create(tmdb_id=912, defaults={'name': 'Actor Solo'})

        usa, _ = Country.objects.get_or_create(code='US', defaults={'name': 'United States of America'})
        uk, _ = Country.objects.get_or_create(code='GB', defaults={'name': 'United Kingdom'})
        japan, _ = Country.objects.get_or_create(code='JP', defaults={'name': 'Japan'})

        self.alpha = Movie.objects.create(
            tmdb_id=301, title='Alpha', release_year=1995, tmdb_rating=Decimal('8.0'),
            original_language='English',
        )
        self.alpha.directors.add(director_x)
        self.alpha.genres.add(drama)
        self.alpha.countries.set([usa, uk])
        Credit.objects.create(movie=self.alpha, person=actor_zed, order=0)

        self.beta = Movie.objects.create(
            tmdb_id=302, title='Beta', release_year=1998, tmdb_rating=Decimal('6.0'),
            original_language='English',
        )
        self.beta.directors.add(director_x)
        self.beta.genres.add(drama)
        self.beta.countries.set([usa])
        Credit.objects.create(movie=self.beta, person=actor_zed, order=0)

        self.gamma = Movie.objects.create(
            tmdb_id=303, title='Gamma', release_year=2015, tmdb_rating=Decimal('9.0'),
            original_language='Japanese',
        )
        self.gamma.directors.add(director_y)
        self.gamma.genres.add(comedy)
        self.gamma.countries.set([japan])
        Credit.objects.create(movie=self.gamma, person=actor_solo, order=0)

        # Authoritative ratings (one per distinct film) -- drives taste/genre/decade/favorite-people stats.
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/alpha', title='Alpha', year=1995,
            rating=Decimal('5.0'), movie=self.alpha,
        )
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/beta', title='Beta', year=1998,
            rating=Decimal('1.0'), movie=self.beta,
        )
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/gamma', title='Gamma', year=2015,
            rating=Decimal('4.0'), movie=self.gamma,
        )

        # Diary/watch history -- drives rewatch + calendar stats. All in Jan 2024:
        # Jan 1-3 form a 3-day streak, then a 7-day-apart pair on Jan 10-11.
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/alpha', title='Alpha', year=1995,
            watched_date='2024-01-01', rating=Decimal('5.0'), rewatch=False, movie=self.alpha,
        )
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/alpha', title='Alpha', year=1995,
            watched_date='2024-01-02', rating=Decimal('5.0'), rewatch=True, movie=self.alpha,
        )
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/beta', title='Beta', year=1998,
            watched_date='2024-01-03', rating=Decimal('1.0'), rewatch=False, movie=self.beta,
        )
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/gamma', title='Gamma', year=2015,
            watched_date='2024-01-10', rating=Decimal('4.0'), rewatch=False, movie=self.gamma,
        )
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/zeta', title='Zeta', year=2020,
            watched_date='2024-01-11', rewatch=False,
        )

    def test_avg_rating_sourced_from_ratings_not_diary(self):
        # Alpha has 2 diary rows (a rewatch), both rating 5.0, but only 1 RatingEntry --
        # a diary-derived average would over-weight it. Expected: (5.0+1.0+4.0)/3.
        context = build_dashboard_context(self.session)
        self.assertAlmostEqual(float(context['avg_rating']), 10 / 3, places=4)

    def test_taste_vs_crowd(self):
        taste = build_dashboard_context(self.session)['taste']
        self.assertEqual(taste['rated_and_enriched_count'], 3)
        self.assertEqual(taste['generosity_score'], -0.5)
        self.assertEqual(taste['overrates'][0]['title'], 'Alpha')
        self.assertEqual(taste['overrates'][0]['delta'], Decimal('1.0'))
        self.assertEqual(taste['underrates'][0]['title'], 'Beta')
        self.assertEqual(taste['underrates'][0]['delta'], Decimal('-2.0'))

    def test_rating_by_genre_and_decade(self):
        genre_decade = build_dashboard_context(self.session)['genre_decade']

        by_genre = {row['label']: row for row in genre_decade['by_genre']}
        self.assertEqual(by_genre['Drama']['avg'], 3.0)
        self.assertEqual(by_genre['Drama']['count'], 2)
        # Comedy has only 1 rated film (Gamma), below the count>=2 threshold -- excluded.
        self.assertNotIn('Comedy', by_genre)

        self.assertEqual(
            genre_decade['by_decade'],
            [{'label': '1990s', 'avg': 3.0, 'count': 2}],
        )

    def test_rating_distribution_sourced_from_ratings_not_diary(self):
        # Alpha has 2 diary rows (a rewatch), both rating 5.0, but only 1 RatingEntry --
        # the distribution must count it once, not twice.
        chart_data = build_dashboard_context(self.session)['chart_data']['rating_distribution']
        by_rating = dict(zip(chart_data['labels'], chart_data['data']))
        self.assertEqual(by_rating['5.0'], 1)
        self.assertEqual(sum(by_rating.values()), 3)  # Alpha, Beta, Gamma -- one each

    def test_release_year_distribution_fills_continuous_range_with_zeros(self):
        distribution = build_dashboard_context(self.session)['release_year_distribution']
        by_year = {row['year']: row['count'] for row in distribution}
        # Continuous range 1995-2015 (21 years) -- gap years get an explicit 0, not omission.
        self.assertEqual(min(by_year), 1995)
        self.assertEqual(max(by_year), 2015)
        self.assertEqual(len(by_year), 21)
        # Alpha (1995) has 2 diary rows (a rewatch) but should only count once.
        self.assertEqual(by_year[1995], 1)
        self.assertEqual(by_year[1998], 1)
        self.assertEqual(by_year[2015], 1)
        self.assertEqual(by_year[2000], 0)  # a gap year with no films

    def test_rating_by_release_year_leaves_gap_years_as_none(self):
        chart_data = build_dashboard_context(self.session)['chart_data']['rating_by_release_year']
        self.assertEqual(chart_data['labels'][0], '1995')
        self.assertEqual(chart_data['labels'][-1], '2015')
        self.assertEqual(len(chart_data['labels']), 21)

        by_year = dict(zip(chart_data['labels'], chart_data['data']))
        # Release year is a deliberate exception to MIN_COUNT_FOR_AVERAGE -- a single
        # rated film in a year still gets a real average here.
        self.assertEqual(by_year['1995'], 5.0)
        self.assertEqual(by_year['1998'], 1.0)
        self.assertEqual(by_year['2015'], 4.0)
        # A gap year is None, not 0 -- 0 would look like a real bottom rating.
        self.assertIsNone(by_year['2000'])

    def test_films_by_country_counts_once_per_country_and_dedupes_rewatches(self):
        distribution = build_dashboard_context(self.session)['country_distribution']
        by_country = {row['label']: row['count'] for row in distribution}
        # USA: Alpha + Beta = 2 (Alpha's rewatch doesn't double-count). UK/Japan: 1 each.
        self.assertEqual(by_country, {'USA': 2, 'UK': 1, 'Japan': 1})

    def test_films_by_language_dedupes_rewatches(self):
        distribution = build_dashboard_context(self.session)['language_distribution']
        by_language = {row['label']: row['count'] for row in distribution}
        self.assertEqual(by_language, {'English': 2, 'Japanese': 1})

    def test_rating_by_country(self):
        chart_data = build_dashboard_context(self.session)['chart_data']['rating_by_country']
        by_country = dict(zip(chart_data['labels'], chart_data['data']))
        # USA: Alpha (5.0) + Beta (1.0) -> 3.0. UK/Japan each have only 1 rated film
        # (Alpha-only, Gamma-only respectively), below the count>=2 threshold -- excluded.
        self.assertEqual(by_country, {'USA': 3.0})

    def test_rating_by_language(self):
        chart_data = build_dashboard_context(self.session)['chart_data']['rating_by_language']
        by_language = dict(zip(chart_data['labels'], chart_data['data']))
        # English: Alpha (5.0) + Beta (1.0) -> 3.0. Japanese has only 1 rated film
        # (Gamma), below the count>=2 threshold -- excluded.
        self.assertEqual(by_language, {'English': 3.0})

    def test_rewatch_leaderboard(self):
        rewatch = build_dashboard_context(self.session)['rewatch']
        self.assertEqual(rewatch['most_rewatched_films'][0]['title'], 'Alpha')
        self.assertEqual(rewatch['most_rewatched_films'][0]['watch_count'], 2)
        self.assertEqual(rewatch['most_rewatched_directors'][0]['movie__directors__name'], 'Dir X')
        # Only 1 rated rewatch (Alpha) in this fixture -- below the count>=2 threshold,
        # so rewatch_avg_rating is None rather than a single-film "average".
        self.assertIsNone(rewatch['rewatch_avg_rating'])
        self.assertAlmostEqual(float(rewatch['first_watch_avg_rating']), 10 / 3, places=4)

    def test_viewing_calendar(self):
        calendar = build_dashboard_context(self.session)['calendar']
        self.assertEqual(calendar['longest_streak_days'], 3)
        self.assertEqual(calendar['longest_gap_days'], 6)
        self.assertEqual(calendar['busiest_month']['count'], 5)
        total_weekday_count = sum(row['count'] for row in calendar['weekday_distribution'])
        self.assertEqual(total_weekday_count, 5)

    def test_favorite_directors_and_actors(self):
        favorite_people = build_dashboard_context(self.session)['favorite_people']

        # Dir X has 2 rated films (Alpha+Beta), Dir Y has 1 (Gamma) -- both below the
        # director count>=3 threshold, so neither appears here.
        director_names = [row['movie__directors__name'] for row in favorite_people['favorite_directors']]
        self.assertNotIn('Dir X', director_names)
        self.assertNotIn('Dir Y', director_names)

        # Actor Zed has 2 rated films, Actor Solo has 1 -- both below the actor
        # count>=4 threshold (actors need a stronger signal than directors, since a
        # film has a whole cast but only one director).
        actor_names = [row['movie__cast_members__name'] for row in favorite_people['favorite_actors']]
        self.assertNotIn('Actor Zed', actor_names)
        self.assertNotIn('Actor Solo', actor_names)


class FavoritePeopleTieBreakTests(TestCase):
    """A tie on the primary sort key must be broken by the other metric -- 'most
    watched' ties broken by rating, 'highest rated' ties broken by watch count."""

    def test_top_directors_tied_on_count_broken_by_avg_rating(self):
        session = ImportSession.objects.create(display_name='Alex')
        low = _make_movie(901, 'Low Rated Film', 2020, 100, 'Drama', 'Director Low')
        high1 = _make_movie(902, 'High Rated Film 1', 2020, 100, 'Drama', 'Director High')
        high2 = _make_movie(903, 'High Rated Film 2', 2021, 100, 'Drama', 'Director High')

        # Both directors have exactly 2 watched films -- tied on count.
        for uri, title, year, movie in [
            ('https://boxd.it/low', 'Low Rated Film', 2020, low),
            ('https://boxd.it/high1', 'High Rated Film 1', 2020, high1),
            ('https://boxd.it/high2', 'High Rated Film 2', 2021, high2),
        ]:
            WatchedEntry.objects.create(import_session=session, letterboxd_uri=uri, title=title, year=year, movie=movie)
        # Director Low needs a 2nd watched film to tie Director High's count of 2.
        low2 = _make_movie(904, 'Low Rated Film 2', 2021, 100, 'Drama', 'Director Low')
        WatchedEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/low2', title='Low Rated Film 2', year=2021,
            movie=low2,
        )

        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/low', title='Low Rated Film', year=2020,
            rating=Decimal('2.0'), movie=low,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/low2', title='Low Rated Film 2', year=2021,
            rating=Decimal('2.0'), movie=low2,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/high1', title='High Rated Film 1', year=2020,
            rating=Decimal('5.0'), movie=high1,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/high2', title='High Rated Film 2', year=2021,
            rating=Decimal('5.0'), movie=high2,
        )

        top_directors = build_dashboard_context(session)['top_directors']
        self.assertEqual(top_directors[0]['count'], top_directors[1]['count'])  # tied on count
        self.assertEqual(top_directors[0]['directors__name'], 'Director High')

    def test_favorite_directors_tied_on_avg_broken_by_count(self):
        session = ImportSession.objects.create(display_name='Alex')
        # Both directors are well above the director count>=2 floor and average 4.0,
        # tied on rating.
        few_movies = [_make_movie(920 + i, f'Few Film {i}', 2020 + i, 100, 'Drama', 'Director Few') for i in range(3)]
        many_movies = [_make_movie(930 + i, f'Many Film {i}', 2020 + i, 100, 'Drama', 'Director Many') for i in range(4)]

        for movie in few_movies + many_movies:
            RatingEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{movie.tmdb_id}', title=movie.title,
                year=movie.release_year, rating=Decimal('4.0'), movie=movie,
            )

        favorite_directors = build_dashboard_context(session)['favorite_people']['favorite_directors']
        self.assertEqual(favorite_directors[0]['avg'], favorite_directors[1]['avg'])  # tied on avg
        self.assertEqual(favorite_directors[0]['movie__directors__name'], 'Director Many')

    def test_favorite_directors_tie_break_uses_displayed_not_raw_average(self):
        # Director Fewer's true average (4.625) is HIGHER than Director More's (4.5714),
        # but both round to the same displayed "4.6" -- so the count tiebreak must use
        # the rounded value, or More (7 films) would wrongly rank below Fewer (4 films).
        session = ImportSession.objects.create(display_name='Alex')
        fewer_ratings = [Decimal('4.5'), Decimal('4.5'), Decimal('4.5'), Decimal('5.0')]
        more_ratings = [Decimal('4.5')] * 6 + [Decimal('5.0')]

        for i, rating in enumerate(fewer_ratings):
            movie = _make_movie(960 + i, f'Fewer Film {i}', 2020 + i, 100, 'Drama', 'Director Fewer')
            RatingEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{movie.tmdb_id}', title=movie.title,
                year=movie.release_year, rating=rating, movie=movie,
            )
        for i, rating in enumerate(more_ratings):
            movie = _make_movie(970 + i, f'More Film {i}', 2020 + i, 100, 'Drama', 'Director More')
            RatingEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{movie.tmdb_id}', title=movie.title,
                year=movie.release_year, rating=rating, movie=movie,
            )

        favorite_directors = build_dashboard_context(session)['favorite_people']['favorite_directors']
        by_name = {row['movie__directors__name']: row for row in favorite_directors}
        self.assertEqual(round(by_name['Director Fewer']['avg'], 1), round(by_name['Director More']['avg'], 1))
        self.assertEqual(favorite_directors[0]['movie__directors__name'], 'Director More')

    def test_favorite_directors_requires_at_least_three_rated_films(self):
        session = ImportSession.objects.create(display_name='Alex')
        two_movies = [_make_movie(940 + i, f'Two Film {i}', 2020 + i, 100, 'Drama', 'Director Two') for i in range(2)]
        three_movies = [_make_movie(950 + i, f'Three Film {i}', 2020 + i, 100, 'Drama', 'Director Three') for i in range(3)]

        for movie in [*two_movies, *three_movies]:
            RatingEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{movie.tmdb_id}', title=movie.title,
                year=movie.release_year, rating=Decimal('4.0'), movie=movie,
            )

        director_names = [
            row['movie__directors__name']
            for row in build_dashboard_context(session)['favorite_people']['favorite_directors']
        ]
        # Director Two has only 2 rated films, below the director count>=3 threshold -- excluded.
        self.assertNotIn('Director Two', director_names)
        self.assertIn('Director Three', director_names)

    def test_favorite_actors_requires_at_least_four_rated_films(self):
        session = ImportSession.objects.create(display_name='Alex')
        few_movies = [_make_movie(960 + i, f'Few Film {i}', 2020 + i, 100, 'Drama') for i in range(3)]
        many_movies = [_make_movie(970 + i, f'Many Film {i}', 2020 + i, 100, 'Drama') for i in range(4)]

        actor_few, _ = Person.objects.get_or_create(tmdb_id=8001, defaults={'name': 'Actor Few'})
        actor_many, _ = Person.objects.get_or_create(tmdb_id=8002, defaults={'name': 'Actor Many'})

        for movie in few_movies:
            Credit.objects.create(movie=movie, person=actor_few, order=0)
        for movie in many_movies:
            Credit.objects.create(movie=movie, person=actor_many, order=0)

        for movie in few_movies + many_movies:
            RatingEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{movie.tmdb_id}', title=movie.title,
                year=movie.release_year, rating=Decimal('4.0'), movie=movie,
            )

        actor_names = [
            row['movie__cast_members__name']
            for row in build_dashboard_context(session)['favorite_people']['favorite_actors']
        ]
        # Actor Few has only 3 rated films, below the actor count>=4 threshold -- excluded.
        self.assertNotIn('Actor Few', actor_names)
        self.assertIn('Actor Many', actor_names)


class CameoFilteringTests(TestCase):
    """An actor billed in the back half (CAMEO_RELATIVE_BILLING_THRESHOLD) of a big
    enough cast (MIN_CAST_SIZE_FOR_CAMEO_FILTER) is excluded from every actor stat."""

    def test_cameo_billed_film_excluded_from_top_actors_count(self):
        session = ImportSession.objects.create(display_name='Alex')
        actor, _ = Person.objects.get_or_create(tmdb_id=700, defaults={'name': 'Cameo Actor'})

        lead_movie = _make_movie(701, 'Lead Role Film', 2020, 100, 'Drama')
        _cast_movie(lead_movie, actor, order=5, total_cast_size=40)  # 5/40 = 0.125, not a cameo

        cameo_movie = _make_movie(702, 'Cameo Film', 2021, 100, 'Drama')
        _cast_movie(cameo_movie, actor, order=25, total_cast_size=40)  # 25/40 = 0.625, a cameo

        for movie, uri in [(lead_movie, 'https://boxd.it/lead'), (cameo_movie, 'https://boxd.it/cameo')]:
            WatchedEntry.objects.create(
                import_session=session, letterboxd_uri=uri, title=movie.title, year=movie.release_year, movie=movie,
            )

        top_actors = {row['person__name']: row for row in build_dashboard_context(session)['top_actors']}
        self.assertEqual(top_actors['Cameo Actor']['count'], 1)  # only the lead-role film counts

    def test_same_relative_billing_not_a_cameo_in_a_small_cast(self):
        # Same 0.583 relative billing as the cameo case above, but the cast is only
        # 12 people -- below MIN_CAST_SIZE_FOR_CAMEO_FILTER, so billing position
        # isn't a meaningful cameo signal here and this must still count. Credited in
        # two such films (not one) so this actor's count=2 clearly beats every
        # filler's count=1 in the TOP_N=10 cutoff -- with everyone tied at count=1
        # and unrated, which of the 12 tied-per-film people survives truncation is
        # arbitrary and unrelated to what this test is actually checking.
        session = ImportSession.objects.create(display_name='Alex')
        actor, _ = Person.objects.get_or_create(tmdb_id=710, defaults={'name': 'Small Cast Actor'})

        movie_a = _make_movie(711, 'Small Ensemble Film A', 2020, 100, 'Drama')
        _cast_movie(movie_a, actor, order=7, total_cast_size=12)
        movie_b = _make_movie(712, 'Small Ensemble Film B', 2021, 100, 'Drama')
        _cast_movie(movie_b, actor, order=7, total_cast_size=12)

        for movie, uri in [(movie_a, 'https://boxd.it/smalla'), (movie_b, 'https://boxd.it/smallb')]:
            WatchedEntry.objects.create(
                import_session=session, letterboxd_uri=uri, title=movie.title, year=movie.release_year, movie=movie,
            )

        top_actors = {row['person__name']: row for row in build_dashboard_context(session)['top_actors']}
        self.assertEqual(top_actors['Small Cast Actor']['count'], 2)

    def test_cameo_rating_excluded_from_avg_and_favorite_actors(self):
        # A cameo film's rating must not drag down (or otherwise affect) the actor's
        # avg_rating shown alongside "most watched", nor count toward their
        # "highest rated" appearances -- the exclusion has to be consistent
        # everywhere an actor's numbers show up, not just the raw watched count.
        session = ImportSession.objects.create(display_name='Alex')
        actor, _ = Person.objects.get_or_create(tmdb_id=720, defaults={'name': 'Consistent Actor'})

        lead_movies = []
        for i in range(4):
            movie = _make_movie(730 + i, f'Lead Film {i}', 2020 + i, 100, 'Drama')
            _cast_movie(movie, actor, order=2, total_cast_size=40)
            lead_movies.append(movie)

        cameo_movie = _make_movie(740, 'Cameo Rating Film', 2024, 100, 'Drama')
        _cast_movie(cameo_movie, actor, order=30, total_cast_size=40)  # 30/40 = 0.75, a cameo

        for movie in lead_movies:
            RatingEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{movie.tmdb_id}', title=movie.title,
                year=movie.release_year, rating=Decimal('4.0'), movie=movie,
            )
        # The cameo film is rated very differently -- if it leaked into the average,
        # it would be obviously wrong (not still 4.0).
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/cameo-rating', title=cameo_movie.title,
            year=cameo_movie.release_year, rating=Decimal('0.5'), movie=cameo_movie,
        )

        context = build_dashboard_context(session)
        top_actors = {row['person__name']: row for row in context['top_actors']}
        self.assertEqual(top_actors['Consistent Actor']['avg_rating'], Decimal('4.0'))

        favorite_actors = {
            row['movie__cast_members__name']: row for row in context['favorite_people']['favorite_actors']
        }
        self.assertEqual(favorite_actors['Consistent Actor']['avg'], Decimal('4.0'))
        self.assertEqual(favorite_actors['Consistent Actor']['count'], 4)


class TagDistributionTests(TestCase):
    """Tags come from diary.csv's Tags column -- a comma-separated string per log
    entry, parsed and counted independently of the film-level breakdowns."""

    def test_tags_split_trimmed_and_counted_per_diary_row(self):
        session = ImportSession.objects.create(display_name='Alex')
        DiaryEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2020,
            watched_date='2024-01-01', tags='comfort, rewatch',
        )
        # A rewatch tagged differently the second time -- both applications count,
        # since a tag describes the watch, not the film.
        DiaryEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2020,
            watched_date='2024-02-01', rewatch=True, tags='comfort',
        )
        DiaryEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/b', title='Film B', year=2021,
            watched_date='2024-03-01', tags='',
        )

        top_tags = {row['label']: row['count'] for row in build_dashboard_context(session)['top_tags']}
        self.assertEqual(top_tags, {'comfort': 2, 'rewatch': 1})

    def test_no_tags_returns_empty_list(self):
        session = ImportSession.objects.create(display_name='Alex')
        DiaryEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2020,
            watched_date='2024-01-01',
        )
        self.assertEqual(build_dashboard_context(session)['top_tags'], [])


class ReviewsLikesAndWatchlistCountTests(TestCase):
    """Overview stats for reviews.csv, likes/films.csv, and watchlist.csv -- each
    deduped by (title, year) since a rewatch's review or a re-like shouldn't count
    twice."""

    def test_counts_dedupe_and_ignore_blank_reviews(self):
        session = ImportSession.objects.create(display_name='Alex')
        ReviewEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2020,
            watched_date='2024-01-01', review='Loved it.',
        )
        # A second review of the same film (a rewatch) -- counts once, not twice.
        ReviewEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a2', title='Film A', year=2020,
            watched_date='2024-02-01', review='Still loved it.',
        )
        # A logged entry with no actual review text -- shouldn't count as a review.
        ReviewEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/b', title='Film B', year=2021,
            watched_date='2024-03-01', review='',
        )
        LikedFilmEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2020,
        )
        LikedFilmEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/c', title='Film C', year=2022,
        )
        WatchlistEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/d', title='Film D', year=2023,
        )

        context = build_dashboard_context(session)
        self.assertEqual(context['reviews_count'], 1)
        self.assertEqual(context['likes_count'], 2)
        self.assertEqual(context['watchlist_count'], 1)

    def test_zero_when_no_reviews_likes_or_watchlist(self):
        session = ImportSession.objects.create(display_name='Alex')
        context = build_dashboard_context(session)
        self.assertEqual(context['reviews_count'], 0)
        self.assertEqual(context['likes_count'], 0)
        self.assertEqual(context['watchlist_count'], 0)


class FilmsWatchedAndFavoritesTests(TestCase):
    """Covers the 'films watched (logged + not logged)' header stat and the
    profile.csv-derived favorites row."""

    def setUp(self):
        self.session = ImportSession.objects.create(
            display_name='Alex',
            favorite_letterboxd_uris=['https://boxd.it/a', 'https://boxd.it/watched-only', 'https://boxd.it/unknown'],
        )
        self.movie_a = _make_movie(501, 'Film A', 2024, 120, 'Drama')

        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2024,
            watched_date='2024-01-01', rating=Decimal('4.5'), movie=self.movie_a,
        )
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2024,
            rating=Decimal('4.5'), movie=self.movie_a,
        )
        WatchedEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/a', title='Film A', year=2024,
            movie=self.movie_a,
        )
        WatchedEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/watched-only',
            title='Watched Only Film', year=2020,
        )
        WatchedEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/b', title='Film B', year=2019,
        )

    def test_films_watched_total_uses_watched_csv_when_present(self):
        context = build_dashboard_context(self.session)
        self.assertEqual(context['films_watched_total'], 3)  # a, watched-only, b

    def test_films_watched_total_falls_back_without_watched_csv(self):
        WatchedEntry.objects.filter(import_session=self.session).delete()
        context = build_dashboard_context(self.session)
        self.assertEqual(context['films_watched_total'], 1)  # union of diary+ratings uris = {a}

    def test_favorites_resolve_title_and_movie_when_available(self):
        by_uri = {f['letterboxd_uri']: f for f in build_dashboard_context(self.session)['favorites']}
        self.assertEqual(by_uri['https://boxd.it/a']['title'], 'Film A')
        self.assertEqual(by_uri['https://boxd.it/a']['movie'], self.movie_a)

    def test_favorites_fall_back_to_watched_only_title_with_no_movie(self):
        by_uri = {f['letterboxd_uri']: f for f in build_dashboard_context(self.session)['favorites']}
        self.assertEqual(by_uri['https://boxd.it/watched-only']['title'], 'Watched Only Film')
        self.assertIsNone(by_uri['https://boxd.it/watched-only']['movie'])

    def test_favorites_unresolvable_uri_has_no_title(self):
        by_uri = {f['letterboxd_uri']: f for f in build_dashboard_context(self.session)['favorites']}
        self.assertIsNone(by_uri['https://boxd.it/unknown']['title'])

    def test_favorites_preserve_profile_order(self):
        favorites = build_dashboard_context(self.session)['favorites']
        self.assertEqual([f['letterboxd_uri'] for f in favorites], self.session.favorite_letterboxd_uris)

    def test_no_favorites_returns_empty_list(self):
        session = ImportSession.objects.create()
        self.assertEqual(build_dashboard_context(session)['favorites'], [])


class MismatchedLetterboxdUriTests(TestCase):
    """Regression coverage for a real Letterboxd export quirk: 'Letterboxd URI' is a
    per-log-entry short link, not a stable per-film id -- the same film gets a
    *different* boxd.it code in diary.csv than in ratings.csv (confirmed against a real
    export: 'The Holdovers' was 'https://boxd.it/5OCOMF' in diary.csv but
    'https://boxd.it/vHza' in ratings.csv). Everything that joins across entry models
    must match on (title, year), never letterboxd_uri."""

    def setUp(self):
        self.session = ImportSession.objects.create(display_name='Alex')

        # Same film, three different letterboxd_uri values across three files -- exactly
        # what a real export looks like.
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/diary-code', title='The Holdovers',
            year=2023, watched_date='2024-01-10', rating=Decimal('4.5'),
        )
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/ratings-code', title='The Holdovers',
            year=2023, rating=Decimal('4.5'),
        )

    def test_films_watched_total_does_not_inflate_from_uri_mismatch(self):
        # Without watched.csv, the diary+ratings fallback must recognize this as ONE
        # film, not two, even though every letterboxd_uri differs.
        context = build_dashboard_context(self.session)
        self.assertEqual(context['films_watched_total'], 1)

    def test_compare_matches_shared_film_despite_different_uri(self):
        other = ImportSession.objects.create(display_name='Sam')
        RatingEntry.objects.create(
            import_session=other, letterboxd_uri='https://boxd.it/a-totally-different-code',
            title='The Holdovers', year=2023, rating=Decimal('3.5'),
        )
        context = build_compare_context(self.session, other)
        self.assertEqual(context['shared_count'], 1)
        self.assertEqual(context['biggest_disagreements'][0]['title'], 'The Holdovers')
        self.assertEqual(context['biggest_disagreements'][0]['delta'], Decimal('1.0'))

    def test_rewatch_leaderboard_groups_rewatches_despite_different_uri(self):
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/rewatch-code', title='The Holdovers',
            year=2023, watched_date='2024-02-01', rating=Decimal('5.0'), rewatch=True,
        )
        rewatch = build_dashboard_context(self.session)['rewatch']
        self.assertEqual(rewatch['most_rewatched_films'][0]['title'], 'The Holdovers')
        self.assertEqual(rewatch['most_rewatched_films'][0]['watch_count'], 2)


class TvShowExclusionTests(TestCase):
    """Letterboxd lets people log some TV content alongside films. A (title, year)
    confirmed as TV via TMDB's TV search (TitleYearLookup.is_tv_show=True) must be
    excluded from every stat, not just the ones that already require a movie match."""

    def setUp(self):
        self.session = ImportSession.objects.create(display_name='Alex')

        TitleYearLookup.objects.create(title='Some TV Show', year=2020, movie=None, is_tv_show=True)

        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/real', title='Real Film', year=2021,
            watched_date='2024-01-01', rating=Decimal('4.0'),
        )
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/real', title='Real Film', year=2021,
            rating=Decimal('4.0'),
        )
        DiaryEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/tv', title='Some TV Show', year=2020,
            watched_date='2024-01-02', rating=Decimal('2.0'),
        )
        RatingEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/tv', title='Some TV Show', year=2020,
            rating=Decimal('2.0'),
        )
        WatchedEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/real', title='Real Film', year=2021,
        )
        WatchedEntry.objects.create(
            import_session=self.session, letterboxd_uri='https://boxd.it/tv', title='Some TV Show', year=2020,
        )

    def test_dashboard_excludes_confirmed_tv_from_counts_and_ratings(self):
        context = build_dashboard_context(self.session)
        self.assertEqual(context['total_films'], 1)
        self.assertEqual(context['films_watched_total'], 1)
        # Only 1 rated diary entry once TV is excluded, below the count>=2 threshold,
        # so avg_rating is None rather than a single-film "average".
        self.assertIsNone(context['avg_rating'])
        self.assertEqual(context['excluded_tv_count'], 1)
        self.assertEqual(context['excluded_tv_titles'], [{'title': 'Some TV Show', 'year': 2020}])

        ratings_seen = context['chart_data']['rating_distribution']['labels']
        self.assertNotIn('2.0', ratings_seen)

    def test_dashboard_reflects_zero_when_nothing_excluded(self):
        other = ImportSession.objects.create()
        DiaryEntry.objects.create(
            import_session=other, letterboxd_uri='https://boxd.it/x', title='Untainted Film', year=2019,
            watched_date='2024-01-01', rating=Decimal('3.0'),
        )
        context = build_dashboard_context(other)
        self.assertEqual(context['excluded_tv_count'], 0)
        self.assertEqual(context['total_films'], 1)

    def test_compare_excludes_confirmed_tv_from_shared_and_unique_films(self):
        other = ImportSession.objects.create(display_name='Sam')
        RatingEntry.objects.create(
            import_session=other, letterboxd_uri='https://boxd.it/real-b', title='Real Film', year=2021,
            rating=Decimal('3.5'),
        )
        RatingEntry.objects.create(
            import_session=other, letterboxd_uri='https://boxd.it/tv-b', title='Some TV Show', year=2020,
            rating=Decimal('5.0'),
        )

        context = build_compare_context(self.session, other)
        # Only "Real Film" should count as shared -- "Some TV Show" is excluded on
        # both sides despite being rated by both.
        self.assertEqual(context['shared_count'], 1)
        self.assertEqual(context['biggest_disagreements'][0]['title'], 'Real Film')
        self.assertEqual(context['only_a_count'], 0)
        self.assertEqual(context['only_b_count'], 0)


class BuildCompareContextTests(TestCase):
    def setUp(self):
        self.session_a = ImportSession.objects.create(display_name='Alex')
        self.session_b = ImportSession.objects.create(display_name='Sam')

        # Shared, close ratings (agreement)
        RatingEntry.objects.create(
            import_session=self.session_a, letterboxd_uri='https://boxd.it/shared1',
            title='Shared Close', year=2020, rating=Decimal('4.0'),
        )
        RatingEntry.objects.create(
            import_session=self.session_b, letterboxd_uri='https://boxd.it/shared1',
            title='Shared Close', year=2020, rating=Decimal('4.5'),
        )

        # Shared, big disagreement
        RatingEntry.objects.create(
            import_session=self.session_a, letterboxd_uri='https://boxd.it/shared2',
            title='Shared Far', year=2021, rating=Decimal('1.0'),
        )
        RatingEntry.objects.create(
            import_session=self.session_b, letterboxd_uri='https://boxd.it/shared2',
            title='Shared Far', year=2021, rating=Decimal('5.0'),
        )

        # Unique to A
        RatingEntry.objects.create(
            import_session=self.session_a, letterboxd_uri='https://boxd.it/onlya',
            title='Only A', year=2019, rating=Decimal('3.0'),
        )

    def test_shared_and_unique_counts(self):
        context = build_compare_context(self.session_a, self.session_b)
        self.assertEqual(context['shared_count'], 2)
        self.assertEqual(context['only_a_count'], 1)
        self.assertEqual(context['only_b_count'], 0)
        self.assertEqual(context['overlap_pct'], round(2 / 3 * 100, 1))

    def test_agreement_and_disagreement_ordering(self):
        context = build_compare_context(self.session_a, self.session_b)
        self.assertEqual(context['biggest_disagreements'][0]['title'], 'Shared Far')
        self.assertEqual(context['most_agreed'][0]['title'], 'Shared Close')

    def test_agreement_pct_uses_half_star_threshold(self):
        context = build_compare_context(self.session_a, self.session_b)
        # Only "Shared Close" (delta 0.5) counts as agreement; "Shared Far" (delta 4.0) doesn't.
        self.assertEqual(context['agreement_pct'], 50.0)

    def test_only_a_films_lists_unique_film(self):
        context = build_compare_context(self.session_a, self.session_b)
        self.assertEqual([f['title'] for f in context['only_a_films']], ['Only A'])

    def test_avg_delta_present_with_two_shared_rated_films(self):
        context = build_compare_context(self.session_a, self.session_b)
        self.assertIsNotNone(context['avg_delta'])


class HighlightsTests(TestCase):
    """Single-film superlatives -- longest/shortest, oldest/newest, highest/lowest
    rated, most watched."""

    def test_highlights_pick_correct_extremes(self):
        session = ImportSession.objects.create(display_name='Alex')
        short = _make_movie(801, 'Short Film', 1980, 70, 'Drama')
        long_ = _make_movie(802, 'Long Film', 2010, 220, 'Drama')
        mid = _make_movie(803, 'Mid Film', 2000, 120, 'Drama')

        for movie, uri in [(short, 'a'), (long_, 'b'), (mid, 'c')]:
            WatchedEntry.objects.create(
                import_session=session, letterboxd_uri=f'https://boxd.it/{uri}', title=movie.title,
                year=movie.release_year, movie=movie,
            )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/a', title='Short Film', year=1980,
            rating=Decimal('2.0'), movie=short,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/b', title='Long Film', year=2010,
            rating=Decimal('5.0'), movie=long_,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/c', title='Mid Film', year=2000,
            rating=Decimal('3.5'), movie=mid,
        )

        highlights = build_dashboard_context(session)['highlights']
        self.assertEqual(highlights['longest_film'].title, 'Long Film')
        self.assertEqual(highlights['shortest_film'].title, 'Short Film')
        self.assertEqual(highlights['oldest_film'].title, 'Short Film')
        self.assertEqual(highlights['newest_film'].title, 'Long Film')
        self.assertEqual(highlights['highest_rated'].title, 'Long Film')
        self.assertEqual(highlights['lowest_rated'].title, 'Short Film')
        # No rewatches in this fixture.
        self.assertIsNone(highlights['most_watched_film'])

    def test_most_watched_film_reflects_rewatch_leaderboard(self):
        session = ImportSession.objects.create(display_name='Alex')
        movie = _make_movie(810, 'Rewatched Film', 2000, 100, 'Drama')

        DiaryEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/rewatch', title='Rewatched Film', year=2000,
            watched_date='2024-01-01', rewatch=False, movie=movie,
        )
        DiaryEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/rewatch', title='Rewatched Film', year=2000,
            watched_date='2024-02-01', rewatch=True, movie=movie,
        )

        highlights = build_dashboard_context(session)['highlights']
        self.assertEqual(highlights['most_watched_film']['title'], 'Rewatched Film')
        self.assertEqual(highlights['most_watched_film']['watch_count'], 2)

    def test_highlights_are_none_for_an_empty_session(self):
        session = ImportSession.objects.create(display_name='Alex')
        highlights = build_dashboard_context(session)['highlights']
        self.assertTrue(all(value is None for value in highlights.values()))


class AverageRequiresAtLeastTwoEntriesTests(TestCase):
    """A single data point isn't a meaningful average -- every average-producing stat
    must fall back to None (or exclude the row/bucket entirely) when it's backed by
    fewer than 2 entries, rather than asserting a fake single-film "average"."""

    def test_generosity_score_is_none_with_only_one_rated_and_enriched_film(self):
        session = ImportSession.objects.create(display_name='Alex')
        movie = _make_movie(701, 'Solo Film', 2020, 100, 'Drama')
        movie.tmdb_rating = Decimal('7.0')
        movie.save()
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/solo', title='Solo Film', year=2020,
            rating=Decimal('4.0'), movie=movie,
        )
        taste = build_dashboard_context(session)['taste']
        self.assertEqual(taste['rated_and_enriched_count'], 1)
        self.assertIsNone(taste['generosity_score'])

    def test_avg_delta_is_none_with_only_one_shared_rated_film(self):
        session_a = ImportSession.objects.create(display_name='Alex')
        session_b = ImportSession.objects.create(display_name='Sam')
        RatingEntry.objects.create(
            import_session=session_a, letterboxd_uri='https://boxd.it/shared', title='Shared', year=2020,
            rating=Decimal('4.0'),
        )
        RatingEntry.objects.create(
            import_session=session_b, letterboxd_uri='https://boxd.it/shared', title='Shared', year=2020,
            rating=Decimal('3.0'),
        )
        context = build_compare_context(session_a, session_b)
        self.assertEqual(context['shared_count'], 1)
        self.assertIsNone(context['avg_delta'])

    def test_top_directors_sourced_from_watched_and_ratings_not_diary(self):
        # No DiaryEntry rows at all in this fixture -- "most watched" counts must come
        # from watched.csv (WatchedEntry) and avg_rating from ratings.csv (RatingEntry),
        # not diary.csv, or this data wouldn't show up at all.
        session = ImportSession.objects.create(display_name='Alex')
        movie_a = _make_movie(801, 'Solo Directed Film', 2020, 100, 'Drama', 'Solo Director')
        movie_b = _make_movie(802, 'Paired Directed Film 1', 2021, 100, 'Drama', 'Paired Director')
        movie_c = _make_movie(803, 'Paired Directed Film 2', 2022, 100, 'Drama', 'Paired Director')

        for uri, title, year, movie in [
            ('https://boxd.it/solo', 'Solo Directed Film', 2020, movie_a),
            ('https://boxd.it/paired1', 'Paired Directed Film 1', 2021, movie_b),
            ('https://boxd.it/paired2', 'Paired Directed Film 2', 2022, movie_c),
        ]:
            WatchedEntry.objects.create(import_session=session, letterboxd_uri=uri, title=title, year=year, movie=movie)

        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/solo', title='Solo Directed Film', year=2020,
            rating=Decimal('4.0'), movie=movie_a,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/paired1', title='Paired Directed Film 1',
            year=2021, rating=Decimal('3.0'), movie=movie_b,
        )
        RatingEntry.objects.create(
            import_session=session, letterboxd_uri='https://boxd.it/paired2', title='Paired Directed Film 2',
            year=2022, rating=Decimal('5.0'), movie=movie_c,
        )

        top_directors = {row['directors__name']: row for row in build_dashboard_context(session)['top_directors']}
        self.assertEqual(top_directors['Solo Director']['count'], 1)
        self.assertEqual(top_directors['Paired Director']['count'], 2)
        # Solo Director has only 1 rated film, below the count>=2 threshold.
        self.assertIsNone(top_directors['Solo Director']['avg_rating'])
        # Paired Director has 2 rated films -- a real average.
        self.assertEqual(top_directors['Paired Director']['avg_rating'], Decimal('4.0'))
        # Paired Director has 2 rated diary rows -- a real average.
        self.assertEqual(top_directors['Paired Director']['avg_rating'], Decimal('4.0'))
