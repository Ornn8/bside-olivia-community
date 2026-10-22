"""The cloud contact a task is carrying out; every request made for it names it."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

HEADER = 'X-Olivia-Contact-Id'
ID = re.compile(r'contact:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
CURRENT = ContextVar('olivia_cloud_contact', default=None)


@contextmanager
def using(contact_id):
    """Calls made inside (including tasks started here) belong to this contact; no id leaves the context as it was."""
    if not isinstance(contact_id, str) or not ID.fullmatch(contact_id):
        yield
        return
    token = CURRENT.set(contact_id)
    try:
        yield
    finally:
        CURRENT.reset(token)


def current():
    return CURRENT.get()


def headers():
    contact = CURRENT.get()
    return {HEADER: contact} if contact else {}


def request_prefix():
    """Media order ids made for a contact start with this, so the order can be told from any other."""
    contact = CURRENT.get()
    return 'contact-' + contact.removeprefix('contact:').replace('-', '') + '-' if contact else ''
