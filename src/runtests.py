# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/

import Queue
import multiprocessing
import time
import unittest
import uuid
import sys

from mozillapulse import consumers, publishers
from mozillapulse.messages.base import GenericMessage

from management import PulseManagementAPI
from guardian import PulseGuardian
from model.user import User
from model.base import db_session
import config

# Default RabbitMQ host settings are as defined in the accompanying
# vagrant puppet files.
DEFAULT_RABBIT_HOST = 'localhost'
DEFAULT_RABBIT_PORT = 5672
DEFAULT_RABBIT_VHOST = '/'
DEFAULT_RABBIT_USER = 'guest'
DEFAULT_RABBIT_PASSWORD = 'guest'

CONSUMER_USER = 'guardtest'
CONSUMER_PASSWORD = 'guardtest'
CONSUMER_EMAIL = 'akachkach@mozilla.com'

# Global pulse configuration.
pulse_cfg = {}

class ConsumerSubprocess(multiprocessing.Process):

    def __init__(self, consumer_class, config, durable=False):
        multiprocessing.Process.__init__(self)
        self.consumer_class = consumer_class
        self.config = config
        self.durable = durable
        self.queue = multiprocessing.Queue()

    def run(self):
        queue = self.queue
        def cb(body, message):
            queue.put(body)
            message.ack()
        consumer = self.consumer_class(durable=self.durable, **self.config)
        consumer.configure(topic='#', callback=cb)
        consumer.listen()


class GuardianProcess(multiprocessing.Process):

    def __init__(self, management_api):
        multiprocessing.Process.__init__(self)
        self.management_api = management_api
        self.guardian = PulseGuardian(self.management_api, emails=False)

    def run(self):
        self.guardian.guard()

class GuardianTest(unittest.TestCase):

    """Launches a consumer process that creates a queue then disconnects,
    and then floods the exchange with messages and checks that PulseGuardian
    warns the queue's owner and deletes the queue if it get's over the maximum size
    """

    consumer = consumers.PulseTestConsumer
    publisher = publishers.PulseTestPublisher

    proc = None
    QUEUE_CHECK_PERIOD = 0.05
    QUEUE_CHECK_ATTEMPTS = 4000

    def _build_message(self, msg_id):
        msg = TestMessage()
        msg.set_data('id', msg_id)
        return msg

    def setUp(self):
        self.management_api = PulseManagementAPI()
        self.guardian = PulseGuardian(self.management_api, emails=False)

    def tearDown(self):
        self.terminate_proc()

    def terminate_proc(self):
        if self.proc:
            self.proc.terminate()
            self.proc.join()
            self.proc = None

    def _wait_for_queue(self, config, queue_should_exist=True):
        # Wait until queue has been created by consumer process.
        consumer = self.consumer(**config)
        consumer.configure(topic='#', callback=lambda x, y: None)
        attempts = 0
        while attempts < self.QUEUE_CHECK_ATTEMPTS:
            attempts += 1
            if consumer.queue_exists() == queue_should_exist:
                break
            time.sleep(self.QUEUE_CHECK_PERIOD)
        self.assertEqual(consumer.queue_exists(), queue_should_exist)

    def _get_verify_msg(self, msg):
        try:
            received_data = self.proc.queue.get(timeout=5)
        except Queue.Empty:
            self.fail('did not receive message from consumer process')
        self.assertEqual(msg.routing_key, received_data['_meta']['routing_key'])
        received_payload = {}
        for k, v in received_data['payload'].iteritems():
            received_payload[k.encode('ascii')] = v.encode('ascii')
        self.assertEqual(msg.data, received_payload)

    def test_warning(self):
        self.management_api.delete_all_queues()

        consumer_cfg = pulse_cfg.copy()
        consumer_cfg['applabel'] = str(uuid.uuid1())

        # Configure / Create the test user to be used for message consumption
        consumer_cfg['user'], consumer_cfg['password'] = CONSUMER_USER, CONSUMER_PASSWORD
        username, password = consumer_cfg['user'], consumer_cfg['password']
        user = User.query.filter(User.username == username).first()
        if user is None:
            user = User.new_user(username=username, email=CONSUMER_EMAIL, password=password)
            user.activate(self.management_api)
            db_session.add(user)
            db_session.commit()

        publisher = self.publisher(**pulse_cfg)
        
        # Publish some messages
        for i in xrange(10):
            msg = self._build_message(0)
            publisher.publish(msg)

        # Start the consumer
        self.proc = ConsumerSubprocess(self.consumer, consumer_cfg, True)
        self.proc.start()
        self._wait_for_queue(consumer_cfg)

        # Monitor the queues, this should create the queue object and assign it to the user
        for i in xrange(10):
            self.guardian.monitor_queues(self.management_api.queues())
            time.sleep(0.2)

        # Terminate the consumer process
        self.terminate_proc()

        # Queue should still exist.
        self._wait_for_queue(consumer_cfg)

        # Get the queue's object
        db_session.refresh(user)

        # Queue multiple messages while no consumer exists.
        for i in xrange(config.warn_queue_size + 1):
            msg = self._build_message(i)
            publisher.publish(msg)

        # Wait for messages to be taken into account and get the warned messages if any
        for i in xrange(10):
            time.sleep(0.3)
            queues_to_warn = {q_data['name'] for q_data in self.management_api.queues()
                          if config.warn_queue_size < q_data['messages_ready'] <= config.del_queue_size}
            if queues_to_warn:
                break

        # Test that no queue have been warned at the beginning of the process 
        self.assertTrue(not any(q.warned for q in user.queues))
        # ... but some queues should be
        self.assertGreater(len(queues_to_warn), 0)

        # Monitor the queues, this should detect queues that should be warned
        self.guardian.monitor_queues(self.management_api.queues())

        # Refreshing the user's queues state
        db_session.refresh(user)

        # Test that the queues that had to be "warned" were
        self.assertTrue(all(q.warned for q in user.queues if q in queues_to_warn))
        # The queues that needed to be warned haven't been deleted
        queues_to_warn_bis = {q_data['name'] for q_data in self.management_api.queues()
                              if config.warn_queue_size < q_data['messages_ready'] <= config.del_queue_size}
        self.assertEqual(queues_to_warn_bis, queues_to_warn)

        # Deleting the test user (should delete all his queues too)
        db_session.delete(user)
        db_session.commit()


    def test_delete(self):
        self.management_api.delete_all_queues()

        consumer_cfg = pulse_cfg.copy()
        consumer_cfg['applabel'] = str(uuid.uuid1())

        # Configure / Create the test user to be used for message consumption
        consumer_cfg['user'], consumer_cfg['password'] = CONSUMER_USER, CONSUMER_PASSWORD
        username, password = consumer_cfg['user'], consumer_cfg['password']
        user = User.query.filter(User.username == username).first()
        if user is None:
            user = User.new_user(username=username, email=CONSUMER_EMAIL, password=password)
            user.activate(self.management_api)
            db_session.add(user)
            db_session.commit()

        publisher = self.publisher(**pulse_cfg)
        
        # Publish some messages
        for i in xrange(10):
            msg = self._build_message(0)
            publisher.publish(msg)

        # Start the consumer
        self.proc = ConsumerSubprocess(self.consumer, consumer_cfg, True)
        self.proc.start()
        self._wait_for_queue(consumer_cfg)

        # Monitor the queues, this should create the queue object and assign it to the user
        for i in xrange(10):
            self.guardian.monitor_queues(self.management_api.queues())
            time.sleep(0.2)

        # Terminate the consumer process
        self.terminate_proc()

        # Queue should still exist.
        self._wait_for_queue(consumer_cfg)

        # Get the queue's object
        db_session.refresh(user)

        self.assertTrue(len(user.queues) > 0)

        # Queue multiple messages while no consumer exists.
        for i in xrange(config.del_queue_size + 1):
            msg = self._build_message(i)
            publisher.publish(msg)

        # Wait some time for published messages to be taken into account
        for i in xrange(10):
            time.sleep(0.3)
            queues_to_delete = {q_data['name'] for q_data in self.management_api.queues()
                                if q_data['messages_ready'] > config.del_queue_size}
            if queues_to_delete:
                break

        # Tests that there are some queues that should be deleted
        self.assertTrue(len(queues_to_delete) > 0)

        # Monitor the queues, this should create the queue object and assign it to the user
        for i in xrange(20):
            self.guardian.monitor_queues(self.management_api.queues())
            time.sleep(0.2)

        # Tests that the queues that had to be deleted were deleted
        self.assertTrue(not any(q in queues_to_delete for q in self.management_api.queues()))
        # And that those were deleted by guardian
        self.assertEqual(queues_to_delete, self.guardian.deleted_queues)
        # And no queue have overgrown
        queues_to_delete = [q_data['name'] for q_data in self.management_api.queues() if q_data['messages_ready'] > config.del_queue_size]
        self.assertTrue(len(queues_to_delete) == 0)

        # Deleting the test user (should delete all his queues too)
        db_session.delete(user)
        db_session.commit()

class TestMessage(GenericMessage):

    def __init__(self):
        super(TestMessage, self).__init__()
        self.routing_parts.append('test')

def main(pulse_opts):
    global pulse_cfg
    pulse_cfg.update(pulse_opts)
    unittest.main(argv=sys.argv[0:1])


if __name__ == '__main__':
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option('--host', action='store', dest='host',
                      default=DEFAULT_RABBIT_HOST,
                      help='host running RabbitMQ; defaults to %s' %
                      DEFAULT_RABBIT_HOST)
    parser.add_option('--port', action='store', type='int', dest='port',
                      default=DEFAULT_RABBIT_PORT,
                      help='port on which RabbitMQ is running; defaults to %d'
                      % DEFAULT_RABBIT_PORT)
    parser.add_option('--vhost', action='store', dest='vhost',
                      default=DEFAULT_RABBIT_VHOST,
                      help='name of pulse vhost; defaults to "%s"' %
                      DEFAULT_RABBIT_VHOST)
    parser.add_option('--user', action='store', dest='user',
                      default=DEFAULT_RABBIT_USER,
                      help='name of pulse RabbitMQ user; defaults to "%s"' %
                      DEFAULT_RABBIT_USER)
    parser.add_option('--password', action='store', dest='password',
                      default=DEFAULT_RABBIT_PASSWORD,
                      help='password of pulse RabbitMQ user; defaults to "%s"'
                      % DEFAULT_RABBIT_PASSWORD)
    (opts, args) = parser.parse_args()
    main(opts.__dict__)
