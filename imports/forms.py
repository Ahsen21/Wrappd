from django import forms

from .validators import validate_export_zip


class UploadForm(forms.Form):
    export_file = forms.FileField(label='Letterboxd export (.zip)')

    def clean_export_file(self):
        uploaded_file = self.cleaned_data['export_file']
        self.cleaned_zip = validate_export_zip(uploaded_file)
        return uploaded_file


class CompareUploadForm(forms.Form):
    export_file_a = forms.FileField(label='First export (.zip)')
    export_file_b = forms.FileField(label='Second export (.zip)')

    def clean_export_file_a(self):
        uploaded_file = self.cleaned_data['export_file_a']
        self.cleaned_zip_a = validate_export_zip(uploaded_file)
        return uploaded_file

    def clean_export_file_b(self):
        uploaded_file = self.cleaned_data['export_file_b']
        self.cleaned_zip_b = validate_export_zip(uploaded_file)
        return uploaded_file
