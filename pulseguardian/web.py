# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import os.path
import re
import sys
from functools import wraps

import requests
import sqlalchemy.orm.exc
import werkzeug.serving
from flask import (Flask,
                   render_template,
                   session,
                   g,
                   redirect,
                   request,
                   jsonify)
from flask_sslify import SSLify
from sqlalchemy.sql.expression import case

from pulseguardian import config
from pulseguardian.logs import setup_logging
from pulseguardian.management import (PulseManagementAPI,
                                      PulseManagementException)
from pulseguardian.model.base import db_session, init_db
from pulseguardian.model.models import User, PulseUser, Queue, Email

# Development cert/key base filename.
DEV_CERT_BASE = 'dev'


# Monkey-patch werkzeug.

def generate_adhoc_ssl_pair(cn=None):
    """Generate a 1024-bit self-signed SSL pair.
    This is a verbatim copy of werkzeug.serving.generate_adhoc_ssl_pair
    from commit 91ec97963c77188cc75ba19b66e1ba0929376a34 except the key
    length has been increased from 768 bits to 1024 bits, since recent
    versions of Firefox and other browsers have increased key-length
    requirements.
    """
    from random import random
    from OpenSSL import crypto

    # pretty damn sure that this is not actually accepted by anyone
    if cn is None:
        cn = '*'

    cert = crypto.X509()
    cert.set_serial_number(int(random() * sys.maxint))
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(60 * 60 * 24 * 365)

    subject = cert.get_subject()
    subject.CN = cn
    subject.O = 'Dummy Certificate'

    issuer = cert.get_issuer()
    issuer.CN = 'Untrusted Authority'
    issuer.O = 'Self-Signed'

    pkey = crypto.PKey()
    pkey.generate_key(crypto.TYPE_RSA, 1024)
    cert.set_pubkey(pkey)
    cert.sign(pkey, 'md5')

    return cert, pkey


# This is used by werkzeug.serving.make_ssl_devcert().
werkzeug.serving.generate_adhoc_ssl_pair = generate_adhoc_ssl_pair


# Initialize the web app.
app = Flask(__name__)
app.secret_key = config.flask_secret_key

# Redirect to https if running on Heroku dyno.
if 'DYNO' in os.environ:
    sslify = SSLify(app)

app.logger.addHandler(setup_logging(config.webapp_log_path))


# Log in with a fake account if set up.  This is an easy way to test
# without requiring Persona (and thus https).
fake_account = None

if config.fake_account:
    fake_account = config.fake_account
    app.config['SESSION_COOKIE_SECURE'] = False
else:
    app.config['SESSION_COOKIE_SECURE'] = True


# Initializing the rabbitmq management API
pulse_management = PulseManagementAPI(
    management_url=config.rabbit_management_url,
    user=config.rabbit_user,
    password=config.rabbit_password)


# Initialize the database.
init_db()


# Decorators and instructions used to inject info into the context or
# restrict access to some pages.

def load_fake_account(fake_account):
    """Load fake user and setup session."""

    # Set session user.
    session['email'] = fake_account
    session['fake_account'] = True
    session['logged_in'] = True

    # Check if user already exists in the database, creating it if not.
    # g.user = User.query.join(Email).filter(Email.address == fake_account).one()
    # if g.user is None:
    #     g.user = User.new_user(fake_account)


def requires_login(f):
    """Decorator for views that require the user to be logged-in."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('email') is None:
            return redirect('/')
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_user():
    """Injects a user and configuration in templates' context."""
    cur_user = User.get_by_email(session.get('email'))
    if cur_user and cur_user.pulse_users:
        pulse_user = cur_user.pulse_users[0]
    else:
        pulse_user = None
    return dict(cur_user=cur_user, pulse_user=pulse_user, config=config,
                session=session)


@app.before_request
def load_user():
    """Loads the currently logged-in user (if any) to the request context."""

    # Check if fake account is set and load user.
    if fake_account:
        load_fake_account(fake_account)

    email = session.get('email')
    if not email:
        g.user = None
    else:
        g.user = User.get_by_email(session.get('email'))
        if not g.user:
            g.user = User.new_user(email)


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()


# Views

@app.route('/')
def index():
    if session.get('email'):
        if g.user.pulse_users:
            return redirect('/profile')
        return redirect('/register')
    return render_template('index.html')


@app.route('/register')
@requires_login
def register():
    return render_template('register.html', email=session.get('email'))


@app.route('/profile')
@requires_login
def profile(error=None, messages=None):
    users = no_owner_queues = []
    if g.user.admin:
        users = User.query.all()
        no_owner_queues = Queue.query.filter(
            Queue.owner.has(PulseUser.owner==None)).all()
    return render_template('profile.html', users=users,
                           no_owner_queues=no_owner_queues,
                           error=error, messages=messages)

@app.route('/all_pulse_users')
@requires_login
def all_pulse_users():
    users = db_session.query(
        PulseUser.username,
        case([(Email.address == None, "None")],
             else_=Email.address)).outerjoin(
                 User, User.id == PulseUser.owner_id).join(
                     Email, User.email_id == Email.id).all()
    return render_template('all_pulse_users.html', users=users)

@app.route('/queues')
@requires_login
def queues():
    users = no_owner_queues = []
    if g.user.admin:
        users = User.query.all()
        no_owner_queues = Queue.query.filter(
            Queue.owner.has(PulseUser.owner==None)).all()
    return render_template('queues.html', users=users,
                           no_owner_queues=no_owner_queues)


@app.route('/queues_listing')
@requires_login
def queues_listing():
    users = no_owner_queues = []
    if g.user.admin:
        users = User.query.all()
        no_owner_queues = Queue.query.filter(
            Queue.owner.has(PulseUser.owner==None)).all()
    return render_template('queues_listing.html', users=users,
                           no_owner_queues=no_owner_queues)


# API

@app.route('/queue/<path:queue_name>', methods=['DELETE'])
@requires_login
def delete_queue(queue_name):
    queue = Queue.query.get(queue_name)

    if queue and (g.user.admin or
                  (queue.owner and queue.owner.owner == g.user)):
        try:
            pulse_management.delete_queue(vhost='/', queue=queue.name)
        except PulseManagementException as e:
            logging.warning("Couldn't delete the queue '{0}' on "
                               "rabbitmq: {1}".format(queue_name, e))
            return jsonify(ok=False)
        db_session.delete(queue)
        db_session.commit()
        return jsonify(ok=True)

    return jsonify(ok=False)


@app.route('/pulse-user/<pulse_username>', methods=['DELETE'])
@requires_login
def delete_pulse_user(pulse_username):
    logging.info('Request to delete Pulse user "{0}".'.format(pulse_username))
    pulse_user = PulseUser.query.filter(PulseUser.username == pulse_username).first()


    if pulse_user and (g.user.admin or pulse_user.owner == g.user):
        try:
            pulse_management.delete_user(pulse_user.username)
        except PulseManagementException as e:
            logging.warning("Couldn't delete user '{0}' on "
                               "rabbitmq: {1}".format(pulse_username, e))
            return jsonify(ok=False)
        logging.info('Pulse user "{0}" deleted.'.format(pulse_username))
        db_session.delete(pulse_user)
        db_session.commit()
        return jsonify(ok=True)

    return jsonify(ok=False)

@app.route('/notification/create', methods=['POST'])
@requires_login
def notification_create():
    error = []
    if ('email' not in request.values or
        not request.values['email']):
       error.append('Email is required')
    if ('queue' not in request.values or
        not request.values['queue']):
       error.append('Queue is required')

    if not error:
        email = request.values['email']
        queue = request.values['queue']
        queue_query = Queue.query.filter(Queue.id==queue)
        if not queue_query.count():
            error.append('Queue is not exist')
        elif Queue.notification_exists(queue, email):
            error.append('This is email address is exist')
        else:
            Queue.create_notification(queue, email)

    if error: 
        return jsonify(ok=False, message=', '.join(error))
    else:
        return jsonify(ok=True)


@app.route('/notification/delete', methods=['POST'])
@requires_login
def notification_delete():
    error = []
    if ('notification' not in request.values or
        not request.values['notification']):
       error.append('Notification is required')
    if ('queue' not in request.values or
        not request.values['queue']):
       error.append('Queue is required')

    if not error:
        queue = request.values['queue'];
        notification = request.values['notification'];
        Queue.delete_notification(queue, notification)

    if error: 
        return jsonify(ok=False, message=', '.join(error))
    else:
        return jsonify(ok=True)


# Authentication related

@app.route('/auth/login', methods=['POST'])
def auth_handler():
    # The request has to have an assertion for us to verify
    if 'assertion' not in request.form:
        return jsonify(ok=False, message="Assertion parameter missing")

    # Send the assertion to Mozilla's verifier service.
    data = dict(assertion=request.form['assertion'],
                audience=config.persona_audience)
    resp = requests.post(config.persona_verifier, data=data, verify=True)

    # Did the verifier respond?
    if resp.ok:
        # Parse the response
        verification_data = resp.json()
        if verification_data['status'] == 'okay':
            email = verification_data['email']
            session['email'] = email
            session['logged_in'] = True

            user = User.get_by_email(email)
            if user is None:
                user = User.new_user(email)

            if user.pulse_users:
                return jsonify(ok=True, redirect='/')

            return jsonify(ok=True, redirect='/register')

    # Oops, something failed. Abort.
    error_msg = "Couldn't connect to the Persona verifier ({0})".format(
        config.persona_verifier)
    logging.error(error_msg)
    return jsonify(ok=False, message=error_msg)


@app.route("/update_info", methods=['POST'])
@requires_login
def update_info():
    pulse_username = request.form['pulse-user']
    new_password = request.form['new-password']
    password_verification = request.form['new-password-verification']

    try:
        pulse_user = PulseUser.query.filter(
            PulseUser.username == pulse_username).one()
    except sqlalchemy.orm.exc.NoResultFound:
        return profile(messages=["Invalid user."])

    if pulse_user.owner != g.user:
        return profile(messages=["Invalid user."])

    if not new_password:
        return profile(messages=["You didn't enter a new password."])

    if new_password != password_verification:
        return profile(error="Password verification doesn't match the "
                       "password.")

    if not PulseUser.strong_password(new_password):
        return profile(error="Your password must contain a mix of "
                       "letters and numerical characters and be at "
                       "least 6 characters long.")

    pulse_user.change_password(new_password, pulse_management)
    return profile(messages=["Password updated for user {0}.".format(
                pulse_username)])


@app.route('/register', methods=['POST'])
def register_handler():
    username = request.form['username']
    password = request.form['password']
    password_verification = request.form['password-verification']
    email = session['email']
    errors = []

    if password != password_verification:
        errors.append("Password verification doesn't match the password.")
    elif not PulseUser.strong_password(password):
        errors.append("Your password must contain a mix of letters and "
                      "numerical characters and be at least 6 characters long.")

    if not re.match('^[a-zA-Z][a-zA-Z0-9._-]*$', username):
        errors.append("The submitted username must start with an "
                      "alphabetical character and contain only alphanumeric "
                      "characters, periods, underscores, and hyphens.")

    # Checking if a user exists in RabbitMQ OR in our db
    try:
        user_response = pulse_management.user(username=username)
        in_rabbitmq = True
    except PulseManagementException:
        in_rabbitmq = False
    else:
        if 'error' in user_response:
            in_rabbitmq = False

    if (in_rabbitmq or
        PulseUser.query.filter(PulseUser.username == username).first()):
        errors.append("A user with the same username already exists.")

    if errors:
        return render_template('register.html', email=email,
                               signup_errors=errors)

    PulseUser.new_user(username, password, g.user, pulse_management)

    return redirect('/profile')


@app.route('/auth/logout', methods=['POST'])
def logout_handler():
    session['email'] = None
    session['logged_in'] = False
    return jsonify(ok=True, redirect='/')


@app.route('/whats_pulse')
def why():
    return render_template('index.html')


def cli(args):
    """Command-line handler.

    Since moving to Heroku, it is preferable to set a .env file with
    environment variables and start up the system with 'foreman start'
    rather than executing web.py directly.
    """
    global fake_account

    # Add StreamHandler for development purposes
    logging.getLogger().addHandler(logging.StreamHandler())

    # If fake account is provided we need to do some setup.
    if fake_account:
        ssl_context = None
    else:
        dev_cert = '%s.crt' % DEV_CERT_BASE
        dev_cert_key = '%s.key' % DEV_CERT_BASE
        if not os.path.exists(dev_cert) or not os.path.exists(dev_cert_key):
            logging.info('Creating dev certificate and key.')
            werkzeug.serving.make_ssl_devcert(DEV_CERT_BASE, host='localhost')
        ssl_context = (dev_cert, dev_cert_key)

    app.run(host=config.flask_host,
            port=config.flask_port,
            debug=config.flask_debug_mode,
            ssl_context=ssl_context)


if __name__ == "__main__":
    cli(sys.argv[1:])
