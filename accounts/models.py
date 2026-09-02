from django.db import models

# No models of our own -- accounts are Django's built-in auth.User as-is. See
# imports.models.ImportSession.owner for the one place that FKs to it.
