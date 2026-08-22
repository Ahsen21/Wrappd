import io
import zipfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from imports.validators import validate_export_zip

from .helpers import build_export_zip


def _uploaded(name, content_bytes, content_type='application/zip'):
    return SimpleUploadedFile(name, content_bytes, content_type=content_type)


class ValidateExportZipTests(TestCase):
    def test_accepts_a_well_formed_export(self):
        zip_bytes = build_export_zip().read()
        zf = validate_export_zip(_uploaded('export.zip', zip_bytes))
        self.assertIn('diary.csv', zf.namelist())

    def test_rejects_non_zip_extension(self):
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.txt', b'not a zip'))

    def test_rejects_non_zip_content(self):
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.zip', b'this is definitely not a zip file'))

    @override_settings(MAX_UPLOAD_SIZE=10)
    def test_rejects_oversized_file(self):
        zip_bytes = build_export_zip().read()
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.zip', zip_bytes))

    def test_rejects_zip_missing_diary_and_ratings(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('watchlist.csv', 'Date,Name,Year,Letterboxd URI\n')
        buffer.seek(0)
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.zip', buffer.read()))

    @override_settings(MAX_UNCOMPRESSED_ZIP_SIZE=100)
    def test_rejects_zip_bomb_by_declared_uncompressed_size(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('diary.csv', 'x' * 100_000)
        buffer.seek(0)
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.zip', buffer.read()))

    def test_rejects_path_traversal_entry_names(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('diary.csv', 'Date,Name,Year,Letterboxd URI,Watched Date\n')
            zf.writestr('../../etc/passwd', 'malicious')
        buffer.seek(0)
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.zip', buffer.read()))

    @override_settings(MAX_ZIP_ENTRY_COUNT=3)
    def test_rejects_too_many_entries(self):
        zip_bytes = build_export_zip().read()  # has 5 entries with all optional files included
        with self.assertRaises(ValidationError):
            validate_export_zip(_uploaded('export.zip', zip_bytes))
