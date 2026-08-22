import logging

from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from tmdb.services.enrichment import enrich_import_session_fully

from .forms import CompareUploadForm, UploadForm
from .models import ImportSession
from .services.parser import ExportParseError, parse_export, persist_parsed_export

logger = logging.getLogger(__name__)


def _session_key(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _process_upload(request, uploaded_file, zf):
    """Create an ImportSession and run it through parse -> persist -> enrich."""
    import_session = ImportSession.objects.create(
        session_key=_session_key(request),
        source_filename=uploaded_file.name,
        status=ImportSession.Status.PARSING,
    )
    try:
        parsed = parse_export(zf)
        persist_parsed_export(import_session, parsed)
        import_session.status = ImportSession.Status.ENRICHING
        import_session.save(update_fields=['status'])
        enrich_import_session_fully(import_session)
        import_session.status = ImportSession.Status.READY
        import_session.save(update_fields=['status'])
    except ExportParseError as exc:
        import_session.status = ImportSession.Status.FAILED
        import_session.error_message = str(exc)
        import_session.save(update_fields=['status', 'error_message'])
        logger.warning('Import %s failed to parse: %s', import_session.id, exc)
    return import_session


class UploadView(View):
    template_name = 'imports/upload.html'

    def get(self, request):
        return render(request, self.template_name, {'form': UploadForm()})

    def post(self, request):
        form = UploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        import_session = _process_upload(request, request.FILES['export_file'], form.cleaned_zip)
        if import_session.status == ImportSession.Status.FAILED:
            form.add_error(None, import_session.error_message)
            return render(request, self.template_name, {'form': form})

        return redirect(reverse('stats:dashboard', kwargs={'session_id': import_session.id}))


class CompareUploadView(View):
    template_name = 'imports/upload_compare.html'

    def get(self, request):
        return render(request, self.template_name, {'form': CompareUploadForm()})

    def post(self, request):
        form = CompareUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        session_a = _process_upload(request, request.FILES['export_file_a'], form.cleaned_zip_a)
        session_b = _process_upload(request, request.FILES['export_file_b'], form.cleaned_zip_b)

        failed = [s for s in (session_a, session_b) if s.status == ImportSession.Status.FAILED]
        if failed:
            form.add_error(None, '; '.join(s.error_message for s in failed))
            return render(request, self.template_name, {'form': form})

        return redirect(reverse('stats:compare', kwargs={'session_a': session_a.id, 'session_b': session_b.id}))
