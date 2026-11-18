"""Keep the established Giftia web maintenance and push path on its single writer."""

import os
from threading import RLock
from time import monotonic

from giftia.outreach import can_send
from giftia.push import LastMessage, SubscriptionStore, notify_all, public_key_from_private


class Continuity:
    def __init__(self, data):
        self.subscriptions = SubscriptionStore(data / 'push_subs.json')
        self.last = LastMessage(data / 'push_last.json')
        self.key = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
        self.public = public_key_from_private(self.key) if self.key else ''
        self.lock = RLock()
        self.next_outreach = monotonic() + 600

    def subscribe(self, value):
        with self.lock:
            self.subscriptions.add(value)

    def notify(self, text):
        with self.lock:
            return notify_all(self.subscriptions, self.last, text, private_b64=self.key, public_b64=self.public,
                              subject=os.environ.get('VAPID_SUBJECT', 'mailto:aila@giftia.icu'),
                              relay_url=os.environ.get('PUSH_RELAY_URL', '').strip(),
                              relay_secret=os.environ.get('PUSH_RELAY_SECRET', '').strip())

    def poll(self, hub):
        if not hasattr(hub, '_maintenance'):
            hub.schedule_maintenance()
        hub.poll_maintenance()
        now = monotonic()
        if now < self.next_outreach:
            return
        self.next_outreach = now + 600
        if (not hub.settings.outreach.enabled or not self.public or not len(self.subscriptions)
                or not can_send(hub.settings, hub.store, hub.clock.now())):
            return
        plan, text = hub.compose_outreach(record=False, target='owner')
        if plan and text.strip():
            sent, _ = self.notify(text)
            if sent:
                hub.note_outreach_sent(text, keep_in_history=True)
