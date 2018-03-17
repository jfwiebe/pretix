import json
from datetime import timedelta
from typing import List, Tuple

from django.db import transaction
from django.dispatch import receiver
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _

from pretix.api.serializers.order import (
    AnswerSerializer, InvoiceAdddressSerializer,
)
from pretix.base.i18n import LazyLocaleException
from pretix.base.models import (
    Event, InvoiceAddress, OrderPosition, QuestionAnswer,
)
from pretix.base.signals import register_data_shredders


class ShredError(LazyLocaleException):
    pass


def shred_constraints(event: Event):
    if (event.date_to or event.date_from) > now() - timedelta(days=60):
        return _('Your event needs to be over for at least 60 days to use this feature.')
    if event.live:
        return _('Your ticket shop needs to be offline to use this feature.')
    return None


class BaseDataShredder:
    """
    This is the base class for all data shredders.
    """

    def __init__(self, event: Event):
        self.event = event

    def __str__(self):
        return self.identifier

    def generate_files(self) -> List[Tuple[str, str, str]]:
        """
        Export the data that is about to be shred and return a list of tuples consisting of a filename,
        a file type and file content.
        """
        raise NotImplementedError()  # NOQA

    def shred_data(self):
        """
        Actually remove the data.
        """
        raise NotImplementedError()  # NOQA

    @property
    def verbose_name(self) -> str:
        """
        A human-readable name for this renderer. This should be short but
        self-explanatory. Good examples include 'German DIN 5008' or 'Italian invoice'.
        """
        raise NotImplementedError()  # NOQA

    @property
    def identifier(self) -> str:
        """
        A short and unique identifier for this shredder.
        This should only contain lowercase letters and in most
        cases will be the same as your package name.
        """
        raise NotImplementedError()  # NOQA

    @property
    def description(self) -> str:
        """
        A description of what this shredder does. Can contain HTML.
        """
        raise NotImplementedError()  # NOQA


def shred_log_fields(logentry, blacklist=None, whitelist=None):
    d = logentry.parsed_data
    if whitelist:
        for k, v in d.items():
            if k not in whitelist:
                d[k] = '█'
    elif blacklist:
        for f in blacklist:
            if f in d:
                d[f] = '█'
    logentry.data = json.dumps(d)
    logentry.shredded = True
    logentry.save(update_fields=['data', 'shredded'])


class EmailAddressShredder(BaseDataShredder):
    verbose_name = _('E-mails')
    identifier = 'order_emails'
    description = _('This will remove all e-mail addresses from orders and attendees, as well as logged email '
                    'contents.')

    def generate_files(self) -> List[Tuple[str, str, str]]:
        yield 'emails-by-order.json', 'application/json', json.dumps({
            o.code: o.email for o in self.event.orders.filter(email__isnull=False)
        }, indent=4)
        yield 'emails-by-attendee.json', 'application/json', json.dumps({
            '{}-{}'.format(op.order.code, op.positionid): op.attendee_email
            for op in OrderPosition.objects.filter(order__event=self.event, attendee_email__isnull=False)
        }, indent=4)

    @transaction.atomic
    def shred_data(self):
        OrderPosition.objects.filter(order__event=self.event, attendee_email__isnull=False).update(attendee_email=None)
        self.event.orders.filter(email__isnull=False).update(email=None)

        for le in self.event.logentry_set.filter(action_type__contains="order.email"):
            shred_log_fields(le, blacklist=['recipient', 'message'])

        for le in self.event.logentry_set.filter(action_type="pretix.event.order.contact.changed"):
            shred_log_fields(le, blacklist=['old_email', 'new_email'])

        for le in self.event.logentry_set.filter(action_type="pretix.event.order.modified").exclude(data=""):
            d = le.parsed_data
            if 'data' in d:
                for row in d['data']:
                    if 'attendee_email' in row:
                        row['attendee_email'] = '█'
                le.data = json.dumps(d)
                le.shredded = True
                le.save(update_fields=['data', 'shredded'])


class AttendeeNameShredder(BaseDataShredder):
    verbose_name = _('Attendee names')
    identifier = 'attendee_names'
    description = _('This will remove all attendee names from order positions, as well as logged changes to them.')

    def generate_files(self) -> List[Tuple[str, str, str]]:
        yield 'attendee-names.json', 'application/json', json.dumps({
            '{}-{}'.format(op.order.code, op.positionid): op.attendee_name
            for op in OrderPosition.objects.filter(order__event=self.event, attendee_name__isnull=False)
        }, indent=4)

    @transaction.atomic
    def shred_data(self):
        OrderPosition.objects.filter(order__event=self.event, attendee_name__isnull=False).update(attendee_name=None)

        for le in self.event.logentry_set.filter(action_type="pretix.event.order.modified").exclude(data=""):
            d = le.parsed_data
            if 'data' in d:
                for i, row in enumerate(d['data']):
                    if 'attendee_name' in row:
                        d['data'][i]['attendee_name'] = '█'
                le.data = json.dumps(d)
                le.shredded = True
                le.save(update_fields=['data', 'shredded'])


class InvoiceAddressShredder(BaseDataShredder):
    verbose_name = _('Invoice addresses')
    identifier = 'invoice_addresses'
    description = _('This will remove all invoice addresses from orders, as well as logged changes to them.')

    def generate_files(self) -> List[Tuple[str, str, str]]:
        yield 'invoice-addresses.json', 'application/json', json.dumps({
            ia.order.code: InvoiceAdddressSerializer(ia).data
            for ia in InvoiceAddress.objects.filter(order__event=self.event)
        }, indent=4)

    @transaction.atomic
    def shred_data(self):
        InvoiceAddress.objects.filter(order__event=self.event).delete()

        for le in self.event.logentry_set.filter(action_type="pretix.event.order.modified").exclude(data=""):
            d = le.parsed_data
            if 'invoice_data' in d and not isinstance(d['invoice_data'], bool):
                for field in d['invoice_data']:
                    if d['invoice_data'][field]:
                        d['invoice_data'][field] = '█'
                le.data = json.dumps(d)
                le.shredded = True
                le.save(update_fields=['data', 'shredded'])


class QuestionAnswerShredder(BaseDataShredder):
    verbose_name = _('Question answers')
    identifier = 'question_answers'
    description = _('This will remove all answers to questions, as well as logged changes to them.')

    def generate_files(self) -> List[Tuple[str, str, str]]:
        yield 'question-answers.json', 'application/json', json.dumps({
            '{}-{}'.format(op.order.code, op.positionid): AnswerSerializer(op.answers.all(), many=True).data
            for op in OrderPosition.objects.filter(order__event=self.event).prefetch_related('answers')
        }, indent=4)

    @transaction.atomic
    def shred_data(self):
        QuestionAnswer.objects.filter(orderposition__order__event=self.event).delete()

        for le in self.event.logentry_set.filter(action_type="pretix.event.order.modified").exclude(data=""):
            d = le.parsed_data
            if 'data' in d:
                for i, row in enumerate(d['data']):
                    for f in row:
                        if f not in ('attendee_name', 'attendee_email'):
                            d['data'][i][f] = '█'
                le.data = json.dumps(d)
                le.shredded = True
                le.save(update_fields=['data', 'shredded'])


@receiver(register_data_shredders, dispatch_uid="shredders_builtin")
def register_payment_provider(sender, **kwargs):
    return [
        EmailAddressShredder,
        AttendeeNameShredder,
        InvoiceAddressShredder,
        QuestionAnswerShredder
    ]
