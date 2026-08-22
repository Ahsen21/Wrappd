"""
Validation for uploaded Letterboxd export zips: size caps, zip-bomb protection,
and path-traversal defense in depth. None of this ever writes to disk -- members
are only ever read into memory via ZipFile.open(), never extracted.
"""

import zipfile

from django.conf import settings
from django.core.exceptions import ValidationError

REQUIRED_ANY_OF = ('diary.csv', 'ratings.csv')


def validate_export_zip(uploaded_file):
    """
    Run all checks against an uploaded file. Raises ValidationError with a clear
    message on the first failure. Returns the opened ZipFile on success so the
    caller doesn't have to reopen it.
    """
    _validate_size(uploaded_file)
    _validate_extension(uploaded_file.name)

    try:
        zf = zipfile.ZipFile(uploaded_file)
    except zipfile.BadZipFile:
        raise ValidationError('That file is not a valid zip archive.')

    _validate_entry_count(zf)
    _validate_uncompressed_size(zf)
    _validate_entry_names(zf)
    _validate_looks_like_export(zf)

    return zf


def _validate_size(uploaded_file):
    if uploaded_file.size > settings.MAX_UPLOAD_SIZE:
        max_mb = settings.MAX_UPLOAD_SIZE // (1024 * 1024)
        raise ValidationError(f'File is too large. Maximum size is {max_mb} MB.')


def _validate_extension(filename):
    if not filename.lower().endswith('.zip'):
        raise ValidationError('Please upload a .zip file (the export downloaded from Letterboxd).')


def _validate_entry_count(zf):
    if len(zf.infolist()) > settings.MAX_ZIP_ENTRY_COUNT:
        raise ValidationError('This zip has more files than a Letterboxd export should. Rejecting as suspicious.')


def _validate_uncompressed_size(zf):
    total_uncompressed = sum(info.file_size for info in zf.infolist())
    if total_uncompressed > settings.MAX_UNCOMPRESSED_ZIP_SIZE:
        raise ValidationError('This zip decompresses to an unexpectedly large size. Rejecting as suspicious.')


def _validate_entry_names(zf):
    for info in zf.infolist():
        name = info.filename
        if name.startswith('/') or '..' in name.split('/'):
            raise ValidationError('This zip contains unsafe file paths and was rejected.')


def _validate_looks_like_export(zf):
    names = set(zf.namelist())
    if not any(required in names for required in REQUIRED_ANY_OF):
        raise ValidationError(
            "This doesn't look like a Letterboxd export -- expected to find diary.csv or ratings.csv inside."
        )
