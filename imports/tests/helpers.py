"""Builds a small in-memory Letterboxd export zip for tests, covering the edge cases
the parser needs to handle: missing year, half-star ratings, a rewatch, blank tags."""

import io
import zipfile

DIARY_CSV = """Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date
2024-01-05,Oppenheimer,2023,https://boxd.it/aaaa,4.5,,,2024-01-04
2024-02-10,Past Lives,2023,https://boxd.it/bbbb,4.0,,road-trip,2024-02-10
2024-03-01,Paddington 2,2017,https://boxd.it/cccc,5.0,,,2024-03-01
2024-06-01,Paddington 2,2017,https://boxd.it/cccc,5.0,Yes,,2024-06-01
2024-04-01,No Year Film,,https://boxd.it/dddd,,,,2024-04-01
"""

RATINGS_CSV = """Date,Name,Year,Letterboxd URI,Rating
2024-01-05,Oppenheimer,2023,https://boxd.it/aaaa,4.5
2024-02-10,Past Lives,2023,https://boxd.it/bbbb,4.0
2024-03-01,Paddington 2,2017,https://boxd.it/cccc,5.0
"""

WATCHLIST_CSV = """Date,Name,Year,Letterboxd URI
2024-05-01,Dune Part Two,2024,https://boxd.it/eeee
"""

LIKES_FILMS_CSV = """Date,Name,Year,Letterboxd URI
2024-03-02,Paddington 2,2017,https://boxd.it/cccc
"""

REVIEWS_CSV = """Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date
2024-01-05,Oppenheimer,2023,https://boxd.it/aaaa,4.5,,"A three-hour fission reaction.",,2024-01-04
"""

# 5 distinct films watched, vs. 4 distinct films logged in diary.csv -- "Barbie" was
# marked watched but never logged, exercising the logged-vs-watched distinction.
WATCHED_CSV = """Date,Name,Year,Letterboxd URI
2024-01-04,Oppenheimer,2023,https://boxd.it/aaaa
2024-02-10,Past Lives,2023,https://boxd.it/bbbb
2024-03-01,Paddington 2,2017,https://boxd.it/cccc
2024-04-01,No Year Film,,https://boxd.it/dddd
2024-07-01,Barbie,2023,https://boxd.it/ffff
"""

# Favorites: aaaa/cccc are also logged+rated (resolvable to a title), zzzz matches
# nothing anywhere in the export (exercises the "Unknown title" fallback).
PROFILE_CSV = (
    'Username,Given Name,Favorite Films\n'
    'moviefan42,Alex,"https://boxd.it/aaaa, https://boxd.it/cccc, https://boxd.it/zzzz"\n'
)


def build_export_zip(*, include_diary=True, include_ratings=True, include_watchlist=True,
                      include_likes=True, include_profile=True, include_watched=True,
                      include_reviews=True, diary_csv=None) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        if include_diary:
            zf.writestr('diary.csv', diary_csv if diary_csv is not None else DIARY_CSV)
        if include_ratings:
            zf.writestr('ratings.csv', RATINGS_CSV)
        if include_watchlist:
            zf.writestr('watchlist.csv', WATCHLIST_CSV)
        if include_likes:
            zf.writestr('likes/films.csv', LIKES_FILMS_CSV)
        if include_watched:
            zf.writestr('watched.csv', WATCHED_CSV)
        if include_reviews:
            zf.writestr('reviews.csv', REVIEWS_CSV)
        if include_profile:
            zf.writestr('profile.csv', PROFILE_CSV)
    buffer.seek(0)
    return buffer
