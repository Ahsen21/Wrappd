from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from imports.models import ImportSession

from .helpers import build_export_zip


class UploadViewTests(TestCase):
    @mock.patch('imports.views.enrich_import_session_fully')
    def test_upload_creates_import_session_and_redirects_to_dashboard(self, mock_enrich):
        upload = SimpleUploadedFile('export.zip', build_export_zip().read(), content_type='application/zip')

        response = self.client.post(reverse('imports:upload'), {'export_file': upload})

        import_session = ImportSession.objects.get()
        self.assertRedirects(response, reverse('stats:dashboard', kwargs={'session_id': import_session.id}))
        self.assertEqual(import_session.status, ImportSession.Status.READY)
        self.assertEqual(import_session.diary_entries.count(), 5)
        mock_enrich.assert_called_once_with(import_session)

    def test_upload_rejects_invalid_file_and_rerenders_form_with_no_session_created(self):
        upload = SimpleUploadedFile('export.txt', b'nope', content_type='text/plain')

        response = self.client.post(reverse('imports:upload'), {'export_file': upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please upload a .zip file')
        self.assertEqual(ImportSession.objects.count(), 0)


class CompareUploadViewTests(TestCase):
    @mock.patch('imports.views.enrich_import_session_fully')
    def test_compare_upload_creates_two_sessions_and_redirects_to_compare(self, mock_enrich):
        upload_a = SimpleUploadedFile('a.zip', build_export_zip().read(), content_type='application/zip')
        upload_b = SimpleUploadedFile('b.zip', build_export_zip().read(), content_type='application/zip')

        response = self.client.post(
            reverse('imports:upload_compare'), {'export_file_a': upload_a, 'export_file_b': upload_b}
        )

        self.assertEqual(ImportSession.objects.count(), 2)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(mock_enrich.call_count, 2)
