import re
from unittest import mock

import requests_mock
from django.test import TestCase, override_settings

from imports.models import DiaryEntry, ImportSession
from tmdb.models import Country, Credit, Movie, TitleYearLookup
from tmdb.services.enrichment import enrich_import_session, enrich_import_session_fully


def _make_session_with_diary(title, year, uri='https://boxd.it/x'):
    session = ImportSession.objects.create()
    DiaryEntry.objects.create(
        import_session=session, letterboxd_uri=uri, title=title, year=year, watched_date='2024-01-01'
    )
    return session


@override_settings(TMDB_API_KEY='test-key')
class EnrichImportSessionTests(TestCase):
    def test_cache_hit_with_no_match_skips_api_call_entirely(self):
        TitleYearLookup.objects.create(title='Foo', year=2020, movie=None)
        session = _make_session_with_diary('Foo', 2020)

        with requests_mock.Mocker() as m:
            enrich_import_session(session)
            self.assertEqual(m.call_count, 0)

        self.assertIsNone(session.diary_entries.get().movie)

    def test_cache_miss_resolves_and_persists_full_metadata(self):
        session = _make_session_with_diary('Oppenheimer', 2023)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 872585,
                'title': 'Oppenheimer',
                'original_title': 'Oppenheimer',
                'release_date': '2023-07-19',
                'poster_path': '/poster.jpg',
                'overview': 'A film.',
                'vote_average': 8.2,
            }]})
            m.get('https://api.themoviedb.org/3/movie/872585', json={
                'runtime': 180,
                'overview': 'A film.',
                'vote_average': 8.2,
                'original_title': 'Oppenheimer',
                'release_date': '2023-07-19',
                'original_language': 'en',
                'spoken_languages': [{'iso_639_1': 'en', 'name': 'English', 'english_name': 'English'}],
                'production_countries': [{'iso_3166_1': 'US', 'name': 'United States of America'}],
                'genres': [{'id': 18, 'name': 'Drama'}],
                'credits': {
                    'crew': [{'id': 1, 'name': 'Christopher Nolan', 'job': 'Director', 'profile_path': None}],
                    'cast': [{'id': 2, 'name': 'Cillian Murphy', 'character': 'Oppenheimer', 'order': 0, 'profile_path': None}],
                },
            })
            enrich_import_session(session)

        movie = Movie.objects.get(tmdb_id=872585)
        self.assertEqual(movie.runtime_minutes, 180)
        self.assertEqual(list(movie.directors.values_list('name', flat=True)), ['Christopher Nolan'])
        self.assertEqual(list(movie.genres.values_list('name', flat=True)), ['Drama'])
        self.assertEqual(Credit.objects.filter(movie=movie).count(), 1)
        self.assertEqual(movie.original_language, 'English')
        self.assertEqual(list(movie.countries.values_list('name', flat=True)), ['United States of America'])
        self.assertTrue(Country.objects.filter(code='US').exists())

        entry = session.diary_entries.get()
        self.assertEqual(entry.movie_id, 872585)

        lookup = TitleYearLookup.objects.get(title='Oppenheimer', year=2023)
        self.assertEqual(lookup.movie_id, 872585)

    def test_co_directed_film_credits_every_director(self):
        # Regression test for a real bug: crew lists can have more than one 'Director'
        # entry (e.g. Joel and Ethan Coen on the same film), but the old code only
        # ever stored the first one, silently dropping the film from the other
        # director's stats entirely.
        session = _make_session_with_diary('Burn After Reading', 2008)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 5147, 'title': 'Burn After Reading', 'release_date': '2008-09-12',
            }]})
            m.get('https://api.themoviedb.org/3/movie/5147', json={
                'runtime': 96, 'genres': [], 'credits': {'crew': [
                    {'id': 1223, 'name': 'Joel Coen', 'job': 'Director', 'profile_path': None},
                    {'id': 1224, 'name': 'Ethan Coen', 'job': 'Director', 'profile_path': None},
                ]}},
            )
            enrich_import_session(session)

        movie = Movie.objects.get(tmdb_id=5147)
        self.assertEqual(
            set(movie.directors.values_list('name', flat=True)), {'Joel Coen', 'Ethan Coen'}
        )

    def test_cast_beyond_the_old_top_ten_cutoff_is_still_credited(self):
        # Regression test for a real bug, same class as the co-director one above:
        # the old code only stored the first 10 cast entries, so a real, memorable
        # role billed lower than that (confirmed for real: Willem Dafoe as Green
        # Goblin is cast position 12 in 'Spider-Man 2') silently never got a Credit
        # row, undercounting that actor's "most watched" total.
        session = _make_session_with_diary('Big Ensemble Film', 2004)
        cast = [
            {'id': 100 + i, 'name': f'Actor {i}', 'character': f'Role {i}', 'order': i, 'profile_path': None}
            for i in range(15)
        ]

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 42424, 'title': 'Big Ensemble Film', 'release_date': '2004-01-01',
            }]})
            m.get('https://api.themoviedb.org/3/movie/42424', json={
                'runtime': 120, 'genres': [], 'credits': {'crew': [], 'cast': cast},
            })
            enrich_import_session(session)

        movie = Movie.objects.get(tmdb_id=42424)
        self.assertEqual(Credit.objects.filter(movie=movie).count(), 15)
        self.assertTrue(Credit.objects.filter(movie=movie, person__tmdb_id=112).exists())  # position 12

    def test_original_language_falls_back_to_raw_code_without_a_spoken_languages_match(self):
        session = _make_session_with_diary('Mystery Film', 2020)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 999, 'title': 'Mystery Film', 'release_date': '2020-01-01',
            }]})
            m.get('https://api.themoviedb.org/3/movie/999', json={
                'runtime': 90, 'original_language': 'xx', 'spoken_languages': [], 'genres': [], 'credits': {},
            })
            enrich_import_session(session)

        movie = Movie.objects.get(tmdb_id=999)
        self.assertEqual(movie.original_language, 'xx')

    def test_no_movie_or_tv_match_caches_a_negative_non_tv_lookup(self):
        session = _make_session_with_diary('Totally Fake Film', 2099)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': []})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            enrich_import_session(session)

        lookup = TitleYearLookup.objects.get(title='Totally Fake Film', year=2099)
        self.assertIsNone(lookup.movie)
        self.assertFalse(lookup.is_tv_show)

        with requests_mock.Mocker() as m2:
            enrich_import_session(session)
            self.assertEqual(m2.call_count, 0)

    def test_weak_movie_match_is_rejected_and_falls_through_to_tv_search(self):
        # Regression test for a real bug: TMDB's movie search returned "Bad
        # Binoculars" (an unrelated documentary) as the top result for "Twin Peaks:
        # The Return" -- the old code trusted result[0] blindly and attached the
        # wrong movie. A title that doesn't actually match must be treated the same
        # as "no result", falling through to the TV-search check.
        session = _make_session_with_diary('Twin Peaks: The Return', 2017)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 498842, 'title': 'Bad Binoculars', 'original_title': 'Bad Binoculars',
            }]})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': [{
                'id': 63247, 'name': 'Twin Peaks', 'original_name': 'Twin Peaks',
            }]})
            enrich_import_session(session)

        self.assertFalse(Movie.objects.filter(tmdb_id=498842).exists())
        lookup = TitleYearLookup.objects.get(title='Twin Peaks: The Return', year=2017)
        self.assertIsNone(lookup.movie)
        self.assertIsNone(session.diary_entries.get().movie)

    def test_single_high_vote_subtitle_variant_is_accepted(self):
        # "Glass Onion" logged on Letterboxd, but TMDB's actual title carries a
        # subtitle: "Glass Onion: A Knives Out Mystery". A real, popular film (high
        # vote_count) that's an exact prefix + subtitle should be accepted even
        # though it's not a byte-for-byte title match.
        session = _make_session_with_diary('Glass Onion', 2022)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 661374, 'title': 'Glass Onion: A Knives Out Mystery',
                'original_title': 'Glass Onion: A Knives Out Mystery',
                'release_date': '2022-11-23', 'vote_count': 7066, 'genres': [], 'credits': {},
            }]})
            m.get('https://api.themoviedb.org/3/movie/661374', json={
                'runtime': 139, 'genres': [], 'credits': {},
            })
            enrich_import_session(session)

        self.assertEqual(session.diary_entries.get().movie_id, 661374)

    def test_low_vote_subtitle_variant_is_rejected(self):
        # Regression test for a real bug this exact heuristic could reintroduce:
        # TMDB's only result for "Neon Genesis Evangelion" is a promotional VHS
        # clip reel titled "Neon Genesis Evangelion: Genesis 0:0 - In the
        # Beginning" (vote_count=4) -- same colon-subtitle shape as the legitimate
        # Glass Onion case above, but it's the wrong item. A low vote_count must
        # keep this from being auto-accepted.
        session = _make_session_with_diary('Neon Genesis Evangelion', 1995)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 1614880, 'title': 'Neon Genesis Evangelion: Genesis 0:0 - In the Beginning',
                'original_title': 'Neon Genesis Evangelion: Genesis 0:0 - In the Beginning',
                'vote_count': 4,
            }]})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            enrich_import_session(session)

        self.assertFalse(Movie.objects.filter(tmdb_id=1614880).exists())
        self.assertIsNone(session.diary_entries.get().movie)

    def test_subtitle_variant_rejected_when_multiple_results_exist(self):
        # The single-result guard matters -- with more than one candidate, ambiguity
        # means we can't be confident that the colon-shaped one is even the right
        # film, so it must not be accepted just because it happens to be present.
        session = _make_session_with_diary('Glass Onion', 2022)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [
                {'id': 661374, 'title': 'Glass Onion: A Knives Out Mystery', 'vote_count': 7066},
                {'id': 999999, 'title': 'Some Other Glass Onion Thing', 'vote_count': 100},
            ]})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            enrich_import_session(session)

        self.assertFalse(Movie.objects.filter(tmdb_id=661374).exists())
        self.assertIsNone(session.diary_entries.get().movie)

    def test_bare_prefix_without_separator_is_not_a_subtitle_variant(self):
        # "Batman" must not match "Batman Begins" just because one is a prefix of
        # the other -- there's no colon/dash marking an actual subtitle, so this is
        # just two different, unrelated films that happen to share a first word.
        session = _make_session_with_diary('Batman', 2005)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 272, 'title': 'Batman Begins', 'vote_count': 15000,
            }]})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            enrich_import_session(session)

        self.assertFalse(Movie.objects.filter(tmdb_id=272).exists())
        self.assertIsNone(session.diary_entries.get().movie)

    def test_part_suffix_variant_is_accepted(self):
        # "Mission: Impossible – Dead Reckoning" logged on Letterboxd, but TMDB's
        # actual title has "Part One" tacked on with no colon/dash before it -- a
        # different (word-suffix, not punctuation-separated) shape than the Glass
        # Onion case, but the same underlying pattern: a franchise film split into
        # parts, where TMDB includes the part designator and Letterboxd doesn't.
        session = _make_session_with_diary('Mission: Impossible – Dead Reckoning', 2023)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 575264, 'title': 'Mission: Impossible - Dead Reckoning Part One',
                'original_title': 'Mission: Impossible - Dead Reckoning Part One',
                'release_date': '2023-07-08', 'vote_count': 5293, 'genres': [], 'credits': {},
            }]})
            m.get('https://api.themoviedb.org/3/movie/575264', json={
                'runtime': 164, 'genres': [], 'credits': {},
            })
            enrich_import_session(session)

        self.assertEqual(session.diary_entries.get().movie_id, 575264)

    def test_unrelated_word_suffix_is_not_a_part_variant(self):
        # A word tacked on after the prefix that isn't "Part/Chapter/Volume N" must
        # still be rejected -- e.g. "Dune" is not a match for "Dune Messiah" just
        # because the words happen to be adjacent.
        session = _make_session_with_diary('Dune', 2021)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 123, 'title': 'Dune Messiah', 'vote_count': 9000,
            }]})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': []})
            enrich_import_session(session)

        self.assertFalse(Movie.objects.filter(tmdb_id=123).exists())
        self.assertIsNone(session.diary_entries.get().movie)

    def test_known_title_alias_resolves_with_no_search_call(self):
        # "Nausicaa of the Valley of the Wind" (1984) doesn't turn up under any
        # search phrasing -- TMDB only has it filed as "Warriors of the Wind" (a
        # different AKA title entirely, not a subtitle/part variant), so it's a
        # manual override rather than something the search-based heuristics can
        # reach. The override must skip search/TV lookups entirely and go straight
        # to fetching the known id's details.
        session = _make_session_with_diary('Nausicaä of the Valley of the Wind', 1984)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/movie/81', json={
                'runtime': 95, 'vote_average': 7.945, 'genres': [{'id': 16, 'name': 'Animation'}],
                'credits': {},
            })
            enrich_import_session(session)
            self.assertEqual(m.call_count, 1)  # only the details fetch -- no search call at all

        movie = Movie.objects.get(tmdb_id=81)
        self.assertEqual(movie.title, 'Nausicaä of the Valley of the Wind')
        self.assertEqual(movie.runtime_minutes, 95)
        self.assertEqual(session.diary_entries.get().movie_id, 81)

    def test_weak_tv_match_does_not_get_confirmed_as_tv(self):
        session = _make_session_with_diary('Totally Unrelated Title', 2015)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': []})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': [{
                'id': 1, 'name': 'Something Else Entirely', 'original_name': 'Something Else Entirely',
            }]})
            enrich_import_session(session)

        lookup = TitleYearLookup.objects.get(title='Totally Unrelated Title', year=2015)
        self.assertFalse(lookup.is_tv_show)

    def test_movie_search_empty_but_tv_search_matches_flags_as_tv(self):
        session = _make_session_with_diary('The Queen\'s Gambit', 2020)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': []})
            m.get('https://api.themoviedb.org/3/search/tv', json={'results': [{'id': 87739, 'name': "The Queen's Gambit"}]})
            enrich_import_session(session)

        lookup = TitleYearLookup.objects.get(title="The Queen's Gambit", year=2020)
        self.assertIsNone(lookup.movie)
        self.assertTrue(lookup.is_tv_show)
        # Confirmed-TV entries never get a Movie assigned.
        self.assertIsNone(session.diary_entries.get().movie)

    def test_tv_search_failure_does_not_cache_anything(self):
        session = _make_session_with_diary('Uncertain Title', 2021)

        with requests_mock.Mocker() as m:
            m.get('https://api.themoviedb.org/3/search/movie', json={'results': []})
            m.get('https://api.themoviedb.org/3/search/tv', status_code=500)
            enrich_import_session(session)

        # No definitive answer yet (movie search: no match; TV search: failed) -- must
        # not be cached either way, so a later run can retry it.
        self.assertFalse(TitleYearLookup.objects.filter(title='Uncertain Title', year=2021).exists())

    def test_api_failure_does_not_cache_a_false_negative(self):
        # Regression test: a rate limit / network failure must NOT be treated the same
        # as "TMDB genuinely has no match" -- that would permanently poison the cache
        # for a film that actually exists (this happened for real: a bulk enrichment
        # run got rate-limited partway through and cached "Back to the Future" etc. as
        # not-found, since the old code caught TMDBClientError and treated it as a
        # zero-result search).
        session = _make_session_with_diary('Back to the Future', 1985)

        with requests_mock.Mocker() as m, mock.patch('tmdb.services.client.time.sleep'):
            m.get('https://api.themoviedb.org/3/search/movie', status_code=429)
            enrich_import_session(session)

        self.assertFalse(TitleYearLookup.objects.filter(title='Back to the Future', year=1985).exists())
        self.assertIsNone(session.diary_entries.get().movie)

        # A later run, once TMDB is reachable again, must still be able to resolve it.
        with requests_mock.Mocker() as m2:
            m2.get('https://api.themoviedb.org/3/search/movie', json={'results': [{
                'id': 105, 'title': 'Back to the Future', 'release_date': '1985-07-03',
            }]})
            m2.get('https://api.themoviedb.org/3/movie/105', json={
                'runtime': 116, 'genres': [], 'credits': {},
            })
            enrich_import_session(session)

        lookup = TitleYearLookup.objects.get(title='Back to the Future', year=1985)
        self.assertEqual(lookup.movie_id, 105)

    def test_enrichment_cap_limits_new_lookups_per_run(self):
        session = ImportSession.objects.create()
        for i in range(3):
            DiaryEntry.objects.create(
                import_session=session,
                letterboxd_uri=f'https://boxd.it/{i}',
                title=f'Film {i}',
                year=2020,
                watched_date='2024-01-01',
            )

        with requests_mock.Mocker() as m:
            m.get(re.compile(r'.*/search/movie.*'), json={'results': []})
            m.get(re.compile(r'.*/search/tv.*'), json={'results': []})
            enrich_import_session(session, cap=1)

        self.assertEqual(TitleYearLookup.objects.count(), 1)

    def test_enrich_fully_loops_past_the_cap_until_everything_resolves(self):
        # Same 3-film, cap=1 setup as the capped test above -- a single capped call
        # would leave 2 unresolved, but the "fully" variant must keep calling until
        # all 3 have an answer (used by the upload flow so a user never lands on
        # their dashboard with films sitting unresolved after just one round).
        session = ImportSession.objects.create()
        for i in range(3):
            DiaryEntry.objects.create(
                import_session=session,
                letterboxd_uri=f'https://boxd.it/{i}',
                title=f'Film {i}',
                year=2020,
                watched_date='2024-01-01',
            )

        with requests_mock.Mocker() as m:
            m.get(re.compile(r'.*/search/movie.*'), json={'results': []})
            m.get(re.compile(r'.*/search/tv.*'), json={'results': []})
            enrich_import_session_fully(session, cap=1)

        self.assertEqual(TitleYearLookup.objects.count(), 3)

    def test_enrich_fully_assigns_from_an_already_warm_cache_with_zero_api_calls(self):
        # Regression test for a real bug: if every (title, year) pair this session
        # references already has a TitleYearLookup row (e.g. someone else imported
        # the same films first), the very first "how much work is left" check would
        # see zero unattempted pairs and return immediately -- without ever calling
        # enrich_import_session, which is the only thing that actually copies the
        # cached movie onto *this* session's entries. Every entry was left null
        # despite TMDB already having an answer for all of them.
        session = _make_session_with_diary('Cached Film', 2020)
        movie = Movie.objects.create(tmdb_id=555, title='Cached Film', release_year=2020)
        TitleYearLookup.objects.create(title='Cached Film', year=2020, movie=movie)

        with requests_mock.Mocker() as m:
            enrich_import_session_fully(session)
            self.assertEqual(m.call_count, 0)  # already cached -- no TMDB call needed

        self.assertEqual(session.diary_entries.get().movie_id, 555)

    def test_enrich_fully_stops_instead_of_looping_forever_on_persistent_failure(self):
        # If TMDB stays unreachable, no round ever makes progress (nothing gets
        # cached either way -- see enrich_import_session's docstring on why a
        # failure must never be cached as a negative match). enrich_import_session_
        # fully must detect the plateau and stop rather than loop forever.
        session = _make_session_with_diary('Unreachable Film', 2020)

        with requests_mock.Mocker() as m:
            m.get(re.compile(r'.*/search/movie.*'), status_code=500)
            enrich_import_session_fully(session)

        self.assertFalse(TitleYearLookup.objects.filter(title='Unreachable Film', year=2020).exists())
