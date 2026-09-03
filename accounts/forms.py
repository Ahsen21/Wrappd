from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


class _StyledFormMixin:
    """Adds the site's shared .text-input class to every field's widget, so these
    forms render consistently with the rest of the site's inputs without repeating
    the widget attrs on every field by hand. Skips checkboxes -- .text-input's
    width/padding/background styling is built for a text field, not a checkbox, and
    would visually break one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault('class', 'text-input')


class SignUpForm(_StyledFormMixin, UserCreationForm):
    """Username + password only -- no email, so there's no password-reset flow
    either. A deliberate friction-reducing tradeoff for a friends-stats app, not an
    oversight."""

    # Not a User model field -- Meta.fields below controls what UserCreationForm's
    # own .save() writes to the User it creates, so this extra field is handled
    # explicitly in SignUpView.form_valid (saved onto the new user's Profile,
    # auto-created by the post_save signal in accounts/signals.py) instead.
    is_searchable = forms.BooleanField(
        label='Let friends find me by my Letterboxd username in Double Feature',
        required=False,
        initial=True,
    )

    class Meta(UserCreationForm.Meta):
        fields = ('username',)


class LoginForm(_StyledFormMixin, AuthenticationForm):
    pass
