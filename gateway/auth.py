"""Authentication module."""

from os import getenv

import os
import jwt
from flask import current_app, request, g
from flask_security import UserMixin
from requests import get, exceptions

from gateway.defaults import configuration
from gateway.errors import HTTPError


def fetch_public_key(app):
    """Get public key and caches it on the app object for future use."""
    # TODO: even though saving the key on the app object is not very nice,
    #  it's actually safe - the worst thing that can happen is that we will
    #  fetch and save the same value on the app object multiple times

    keycloak_url = configuration.BAYESIAN_FETCH_PUBLIC_KEY
    if keycloak_url:
        pub_key_url = keycloak_url.strip('/') + '/auth/realms/fabric8/'
        try:
            result = get(pub_key_url, timeout=0.5)
            app.logger.info('Fetching public key from %s, status %d, result: %s',
                            pub_key_url, result.status_code, result.text)
        except exceptions.Timeout:
            app.logger.error('Timeout fetching public key from %s', pub_key_url)
            return ''
        if result.status_code != 200:
            return ''
        pkey = result.json().get('public_key', '')
        app.public_key = \
            '-----BEGIN PUBLIC KEY-----\n{pkey}\n-----END PUBLIC KEY-----'.format(pkey=pkey)
    else:
        app.public_key = app.config.get('BAYESIAN_PUBLIC_KEY')

    return app.public_key


def decode_token():
    """Decode the authorization token read from the request header."""
    token = request.headers.get('Authorization')
    if token is None:
        return {}

    if token.startswith('Bearer '):
        _, token = token.split(' ', 1)

    pub_key = fetch_public_key(current_app)
    audiences = configuration.BAYESIAN_JWT_AUDIENCE.split(',')

    decoded_token = None
    for aud in audiences:
        try:
            decoded_token = jwt.decode(token.encode('ascii'), pub_key, algorithm='RS256',
                                       audience=aud)
        except jwt.InvalidTokenError:
            current_app.logger.error('Auth Token could not be decoded for audience {}'.format(aud))
            decoded_token = None

        if decoded_token is not None:
            break

    if decoded_token is None:
        raise jwt.InvalidTokenError('Auth token audience cannot be verified.')

    return decoded_token


def login_required(view):
    """Login required wrapper."""
    # NOTE: the actual authentication 401 failures are commented out for now and will be
    # uncommented as soon as we know everything works fine; right now this is purely for
    # being able to tail logs and see if stuff is going fine
    def wrapper(*args, **kwargs):
        # Disable authentication for local setup
        if getenv('DISABLE_AUTHENTICATION') in ('1', 'True', 'true'):
            return view(*args, **kwargs)

        lgr = current_app.logger

        try:
            decoded = decode_token()
            if decoded is None:
                lgr.exception('Provide an Authorization token with the API request')
                raise HTTPError(401, 'Authentication failed - token missing')

            lgr.info('Successfully authenticated user {e} using JWT token'.format(
                e=decoded.get('email'))
            )
        except jwt.ExpiredSignatureError as exc:
            lgr.exception('Expired JWT token')
            raise HTTPError(401, 'Authentication failed - token has expired') from exc
        except Exception as exc:
            lgr.exception('Failed decoding JWT token')
            raise HTTPError(401, 'Authentication failed - could not decode JWT token') from exc
        else:
            user = F8aUser(decoded.get('email', 'nobody@nowhere.nodomain'))

        if user:
            if user_whitelisted(current_app, user):
                g.current_user = user
            else:
                g.current_user = F8aUser('unauthenticated@no.auth.token')
                raise HTTPError(401, 'User is not whitelisted')
        else:
            g.current_user = F8aUser('unauthenticated@no.auth.token')
            raise HTTPError(401, 'Authentication required')
        return view(*args, **kwargs)

    return wrapper


def user_whitelisted(app, user):
    """Check if user is authorized to use the gateway."""
    return user.email in get_whitelist(app)


def get_whitelist(app):
    """Return user whitelist."""
    if not getattr(app, 'user_whitelist', ''):
        whitelist_str = os.environ.get('USER_WHITELIST', '')
        domain = os.environ.get('USER_DOMAIN', 'redhat.com')
        whitelist = tuple("{u}@{d}".format(u=x, d=domain) for x in whitelist_str.split(','))
        app.user_whitelist = whitelist

    return app.user_whitelist


class F8aUser(UserMixin):
    """F8a user class."""

    def __init__(self, email):
        """F8a user constructor."""
        self.email = email
