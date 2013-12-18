from __future__ import absolute_import
from contextlib import contextmanager

from kombu import Connection
from kombu.common import itermessages, maybe_declare

from nameko.containers import WorkerContext
from nameko.rpc import ServiceProxy, ReplyListener


class ConsumeEvent(object):
    """ Event for the RPC consumer with the same interface as eventlet.Event.
    """
    def __init__(self, queue_consumer, correlation_id):
        self.correlation_id = correlation_id
        self.queue_consumer = queue_consumer

    def send(self, body):
        self.body = body

    def wait(self):
        """ Makes a blocking call to its queue_consumer until the message
        with the given correlation_id has been processed.
        """
        self.queue_consumer.poll_messages(self.correlation_id)
        return self.body


class PollingQueueConsumer(object):
    """ Implements a minimum interface of the
    :class:`~messaging.QueueConsumer`. Instead of processing messages in a
    separate thread it provides a polling method to block until a message with
    the same correlation ID of the RPC-proxy call arrives.
    """
    def register_provider(self, provider):
        self.provider = provider
        conn = Connection(provider.container.config['AMQP_URI'])
        self.channel = conn.channel()
        self.queue = provider.queue
        maybe_declare(self.queue, self.channel)

    def unregister_provider(self, provider):
        pass

    def ack_message(self, msg):
        msg.ack()

    def poll_messages(self, correlation_id):
        channel = self.channel
        conn = channel.connection
        for body, msg in itermessages(conn, channel, self.queue, limit=None):
            if correlation_id == msg.properties.get('correlation_id'):
                self.provider.handle_message(body, msg)
                break

    def stop(self):
        import eventlet
        while self.channel.connection.connected:
            eventlet.sleep()


class SingleThreadedReplyListener(ReplyListener):
    """ A ReplyListener which uses a custom queue consumer and ConsumeEvent.
    """
    queue_consumer = None

    def __init__(self):
        self.queue_consumer = PollingQueueConsumer()
        super(SingleThreadedReplyListener, self).__init__()

    def get_reply_event(self, correlation_id):
        reply_event = ConsumeEvent(self.queue_consumer, correlation_id)
        self._reply_events[correlation_id] = reply_event
        return reply_event

    def stop(self):
        super(SingleThreadedReplyListener, self).stop()
        self.queue_consumer.stop()


@contextmanager
def rpc_proxy(container_service_name, nameko_config):
    """
    Yield a single-threaded RPC proxy to a named service. Method calls to the
    proxy are converted into RPC calls, with responses returned directly.

    Enables services not hosted by nameko to make RPC requests to a nameko
    cluster.
    """

    class ProxyContainer(object):
        """ Implements a minimum interface of the
        :class:`~containers.ServiceContainer` to be used by the subclasses
        and rpc imports in this module.
        """
        service_name = container_service_name

        def __init__(self, config):
            self.config = config

    container = ProxyContainer(nameko_config)

    worker_ctx = WorkerContext(container, service=None, method_name=None)

    reply_listener = SingleThreadedReplyListener()

    reply_listener.container = container
    reply_listener.prepare()

    service_proxy = ServiceProxy(worker_ctx, container_service_name,
                                 reply_listener)

    yield service_proxy

    reply_listener.stop()