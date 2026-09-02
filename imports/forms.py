import re
import uuid

from django import forms

from .models import ImportSession
from .validators import validate_export_zip

# Matches a UUID anywhere in the pasted text, so a friend can paste either the bare
# session id or the full dashboard URL it's embedded in -- same tolerant-parsing
# approach either way, no separate "is this a URL" branch needed.
_UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


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


class JoinCompareForm(forms.Form):
    """A friend's pasted dashboard link, resolved to their ImportSession. Used by
    CompareJoinView once the logged-in user already has their own READY session --
    see stats:compare, which already accepts any two session ids with no ownership
    check (compare links stay openly shareable), so this form's only job is turning
    pasted text into a valid, ready one."""

    friend_link = forms.CharField(
        label="Friend's Wrappd link",
        max_length=500,
        widget=forms.TextInput(attrs={
            'class': 'text-input',
            'placeholder': 'Paste the link your friend sent you',
        }),
    )

    def clean_friend_link(self):
        raw = self.cleaned_data['friend_link'].strip()
        match = _UUID_RE.search(raw)
        if not match:
            raise forms.ValidationError(
                "That doesn't look like a Wrappd link. Paste the full dashboard link your friend sent you."
            )
        session_id = uuid.UUID(match.group(0))
        try:
            friend_session = ImportSession.objects.get(id=session_id)
        except ImportSession.DoesNotExist:
            raise forms.ValidationError(
                "We couldn't find that Wrappd link. Double check it was copied in full."
            )
        if friend_session.status != ImportSession.Status.READY:
            raise forms.ValidationError(
                "Your friend's import isn't ready yet -- ask them to wait for it to finish processing."
            )
        self.cleaned_friend_session = friend_session
        return raw
