from django import forms
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _
from django.conf import settings
from django.forms.models import ModelFormMetaclass
from django.contrib.auth.forms import AuthenticationForm as DjangoAuthenticationForm
from django.contrib.auth import authenticate

from enroll.models import VerificationToken
from enroll.validators import UniqueUsernameValidator, UniqueEmailValidator
from enroll import import_class
from enroll.signals import post_registration
from django.forms.forms import DeclarativeFieldsMetaclass

DEFAULT_FORM_VALIDATORS = {
    'username': [ UniqueUsernameValidator ],
    'email': [ UniqueEmailValidator ],
}


def add_validators_to_class_fields(new_class):
    for field, validators in new_class.field_validators.iteritems():
        if field not in new_class.base_fields:
            #field is not present on form - TODO logger.warning
            continue
        for validator in validators:
            if isinstance(validator, basestring):
                validator = import_class(validator)()
            elif isinstance(validator, type):
                validator = validator()
            new_class.base_fields[field].validators.append(validator)


class ExplicitValidationModelFormMetaclass(ModelFormMetaclass):
    """Adds explicit declared field validators to class fields"""
    def __new__(cls, name, bases, attrs):
        new_class = super(ExplicitValidationModelFormMetaclass, cls).__new__(cls, name, bases, attrs)
        add_validators_to_class_fields(new_class)
        return new_class


class ExplicitValidationFormMetaclass(DeclarativeFieldsMetaclass):
    """Adds explicit declared field validators to class fields"""
    def __new__(cls, name, bases, attrs):
        new_class = super(ExplicitValidationFormMetaclass, cls).__new__(cls, name, bases, attrs)
        add_validators_to_class_fields(new_class)
        return new_class


class RequestAcceptingModelForm(forms.ModelForm):
    """Helper class. Store request on form instance."""

    def __init__(self, *args, **kwargs):
        request = kwargs.pop('request')
        super(RequestAcceptingModelForm, self).__init__(*args, **kwargs)
        self.request = request


class RequestAcceptingForm(forms.Form):
    """Helper class. Store request on form instance."""

    def __init__(self, *args, **kwargs):
        request = kwargs.pop('request')
        super(RequestAcceptingForm, self).__init__(*args, **kwargs)
        self.request = request


class BaseSignUpForm(RequestAcceptingModelForm):
    """If username is not between fields uses email as user's username"""

    __metaclass__ = ExplicitValidationModelFormMetaclass

    auto_verify_user = getattr(settings , 'ENROLL_AUTO_VERIFY', False)

    field_validators = getattr(settings , 'ENROLL_FORM_VALIDATORS', DEFAULT_FORM_VALIDATORS)

    class Meta:
        model = User
        fields = getattr(settings , 'ENROLL_SIGNUP_FORM_USER_FIELDS', ('username', 'email'))

    def create_verification_token(self, user, email):
        return VerificationToken.objects.create_token(user, verification_type=VerificationToken.TYPE_SIGN_UP, email=email)

    def get_username(self, cleaned_data):
        """User email as username if username field is not present"""
        return self.cleaned_data.get('username', self.cleaned_data.get('email'))

    def save(self):
        #use UserManager to create instance instead of saving self.instance
        password = self.cleaned_data['password1']
        email = self.cleaned_data.get('email')
        username = self.get_username(self.cleaned_data)

        user = User.objects.create_user(username, email, password)

        if self.auto_verify_user:
            token = None
        else:
            user.is_active = False
            user.save()
            token = self.create_verification_token(user, email)

        post_registration.send(sender=user.__class__, user=user, request=self.request, token=token)
        return user


class PasswordFormMixin(object):
    """Helper class.
    Each form field must be on Form derived class. Declare password1 and password2 in derived class
    Also call clean_password_couple from clean"""

    def check_username_derived_password(self, username, password):
        username = username.lower()
        password = password.lower()
        if password.startswith(username) or password[::-1].startswith(username):
            raise forms.ValidationError(_(u'Password cannot be derived from username'))

    def clean_password_couple(self):
        if 'password1' in self.cleaned_data:
            if getattr(settings, 'ENROLL_FORBID_USERNAME_DERIVED_PASSWORD', False) and 'username' in self.cleaned_data:
                self.check_username_derived_password(self.cleaned_data['username'], self.cleaned_data['password1'])
            if 'password2' in self.cleaned_data:
                if self.cleaned_data['password1'] != self.cleaned_data['password2']:
                    raise forms.ValidationError(_(u'You must type the same password each time'))


class SignUpForm(PasswordFormMixin, BaseSignUpForm):
    password1 = forms.CharField(required=True, widget=forms.PasswordInput, label=_(u'password'),
                                min_length=getattr(settings , 'ENROLL_PASSWORD_MIN_LENGTH', 4))
    password2 = forms.CharField(required=True, widget=forms.PasswordInput, label=_(u'password (again)'), )

    def clean(self):
        self.clean_password_couple()
        return super(SignUpForm, self).clean()


class RequestPassingAuthenticationForm(DjangoAuthenticationForm):
    """
        Pass request to backend.
        Also patched to allow login by inactive users. Maybe it will be fixed in Django 1.3
    """
    supports_inactive_user = getattr(settings, 'ENROLL_AUTH_FORM_SUPPORTS_INACTIVE_USER', False)

    #copied and patched from parent class
    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username and password:
            self.user_cache = authenticate(username=username, password=password, request=self.request)
            if self.user_cache is None:
                raise forms.ValidationError(_("Please enter a correct username and password. Note that both fields are case-sensitive."))
            elif not self.supports_inactive_user and not self.user_cache.is_active:
                raise forms.ValidationError(_("This account is inactive."))
        self.check_for_test_cookie()
        return self.cleaned_data


class PasswordResetStepOneForm(RequestAcceptingForm):
    email = forms.EmailField(label=_("E-mail"), max_length=75)

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email:
            email = email.strip()
            self.users_cache = User.objects.filter(email__iexact=email)
            if len(self.users_cache) == 0:
                raise forms.ValidationError(_("That e-mail address doesn't have an associated user account. Are you sure you've registered?"))
        return email

    def create_verification_token(self, user):
        return VerificationToken.objects.create_token(user, verification_type=VerificationToken.TYPE_PASSWORD_RESET)

    def save(self):
        for user in self.users_cache:
            self.create_verification_token(user) #Notification mail is triggered by post_save hanlder


class PasswordResetStepTwoForm(PasswordFormMixin, RequestAcceptingForm):

    __metaclass__ = ExplicitValidationFormMetaclass

    password1 = forms.CharField(required=True, widget=forms.PasswordInput, label=_(u'new password'), min_length=getattr(settings , 'ENROLL_PASSWORD_MIN_LENGTH', 4))
    password2 = forms.CharField(required=True, widget=forms.PasswordInput, label=_(u'new password (again)'))

    field_validators = { #we need only validators for password1 and password2 from settings value
        'password1': getattr(settings, 'ENROLL_FORM_VALIDATORS', DEFAULT_FORM_VALIDATORS).get('password1', []),
        'password2': getattr(settings, 'ENROLL_FORM_VALIDATORS', DEFAULT_FORM_VALIDATORS).get('password2', [])
    }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user')
        super(PasswordResetStepTwoForm, self).__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        self.clean_password_couple()
        return super(PasswordResetStepTwoForm, self).clean()

    def save(self):
        self.user.set_password(self.cleaned_data['password1'])
        self.user.save()
        return self.user


class ChangeEmailForm(RequestAcceptingForm):

    __metaclass__ = ExplicitValidationFormMetaclass

    email = forms.EmailField(required=True, widget=forms.TextInput(attrs=dict(maxlength=75)), label=_(u'email address'))

    field_validators = { #we need only validators for email from settings value
        'email': getattr(settings, 'ENROLL_FORM_VALIDATORS', DEFAULT_FORM_VALIDATORS ).get('email', []),
    }

    def create_verification_token(self, user, email):
        return VerificationToken.objects.create_token(user, verification_type=VerificationToken.TYPE_EMAIL_CHANGE, email=email)

    def save(self):
        self.create_verification_token(self.request.user, self.cleaned_data['email'])


