from django.contrib.auth.backends import ModelBackend as DjangoModelBackend
from django.contrib.auth import get_user_model
from django.db.models.query_utils import Q
from django.conf import settings


class ModelBackend(DjangoModelBackend):
    """Extended authentication backend"""

    supports_inactive_user = getattr(settings, 'ENROLL_AUTH_BACKEND_INACTIVE_USER', True )

    #should contains only unique columns !!!
    login_attributes = getattr(settings, 'ENROLL_AUTH_BACKEND_LOGIN_ATTRIBUTES', ['username'] )

    def find_user_by_login(self, login):
        combined = None
        for login_attr in self.login_attributes:
            q = Q(**{login_attr: login})
            combined = q if combined is None else combined | q
        return get_user_model().objects.get(combined)

    def authenticate_user(self, user, password, request=None):
        return user if user.check_password(password) else None

    def authenticate(self, username=None, password=None, request=None):
        try:
            user = self.find_user_by_login(username)
            return self.authenticate_user(user, password, request)
        except get_user_model().DoesNotExist:
            return None


#class SignUpBackend(object):


