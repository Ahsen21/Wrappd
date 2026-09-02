from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


class _StyledFormMixin:
    """Adds the site's shared .text-input class to every field's widget, so these
    forms render consistently with the rest of the site's inputs without repeating
    the widget attrs on every field by hand."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'text-input')


class SignUpForm(_StyledFormMixin, UserCreationForm):
    """Username + password only -- no email, so there's no password-reset flow
    either. A deliberate friction-reducing tradeoff for a friends-stats app, not an
    oversight."""

    class Meta(UserCreationForm.Meta):
        fields = ('username',)


class LoginForm(_StyledFormMixin, AuthenticationForm):
    pass
