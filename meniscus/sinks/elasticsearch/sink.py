"""
This module contains operations for implementing an elasticsearch data sink.
It exposes a task that allows messages to be queued for indexing.  It then
exposes the ElasticSearchBulkStreamer which creates a pool of processes for
pulling a stream off of the queue and performing bulk flushes to Elasticsearch.
"""
from multiprocessing import cpu_count, Pool
import signal
import sys
import uuid

from kombu import Connection, Exchange, Queue
from kombu.pools import producers
from elasticsearch import helpers as es_helpers

import meniscus.config as config
from meniscus import env
from meniscus.data.handlers import elasticsearch
from meniscus.queue import celery


_LOG = env.get_logger(__name__)

conf = config.get_config()
broker_url = conf.celery.BROKER_URL

es_handler = elasticsearch.get_handler()

BULK_SIZE = es_handler.bulk_size
TTL = es_handler.ttl
ELASTICSEARCH_QUEUE = 'elasticsearch'


@celery.task
def put_message(message):
    """
    Builds an indexing requests for a message, then sends the request
    to be queued
    """
    try:
        _queue_index_request(
            index=message['meniscus']['tenant'],
            doc_type=message['meniscus']['correlation']['pattern'],
            document=message)
    except Exception as ex:
        _LOG.exception(ex.message)
        put_message.retry()


def _queue_index_request(index, doc_type, document, ttl=TTL):
    """
    places a message index request on the queue
    """

    # The broker where our exchange is.
    connection = Connection(broker_url)

    # The exchange we send our index requests to.
    es_exchange = Exchange(
        ELASTICSEARCH_QUEUE, exchange_type='direct', exchange_durable=True)
    bound_exchange = es_exchange(connection)
    bound_exchange.declare()

    # Queue that exchange will route messages to
    es_queue = Queue(ELASTICSEARCH_QUEUE, exchange=bound_exchange,
                     routing_key=ELASTICSEARCH_QUEUE, queue_durable=True)

    #create the metadata for index operation
    action = {
        '_index': index,
        '_type': doc_type,
        '_id': str(uuid.uuid4()),
        '_ttl': ttl,
        '_source': document
    }

    #publish the message
    with producers[connection].acquire(block=True) as producer:
        producer.publish(action, routing_key=ELASTICSEARCH_QUEUE,
                         serializer='json', declare=[es_queue])


def get_queue_stream(ack_list, bulk_timeout):
    """
    A generator that pulls messages off a queue and yields the result.
    The generator can be used as an iterable to consume messages.
    Messages will be pulled continuously and will block while waiting for new
    messages until the timeout is reached.
    :param ack_list: list of data for messages that pending acknowledgement
    :param bulk_timeout:  length of time to wait for a message from queue
    """
    with Connection(broker_url) as connection:
        simple_queue = connection.SimpleQueue(ELASTICSEARCH_QUEUE)
        while True:
            _LOG.debug("waiting for message")
            msg = simple_queue.get(block=True, timeout=bulk_timeout)
            _LOG.debug(msg)
            ack_list.append(msg)
            yield msg.payload


def flush_to_es(bulk_timeout):
    """
    Flushes a stream of messages to elasticsearch using bulk flushing.
    Uses a generator to pull messages off the queue and passes this as an
     iterable to the streaming_bulk method.  streaming_bulk is also a generator
     that yields message data used for acking from the queue after they
     are flushed.
    :param bulk_size: the number of messages to flush at once to elasticsearch
    :param bulk_timeout:
    :return: length of time to wait for a message from queue
    """

    while True:
        try:
            es_client = es_handler.connection
            ack_list = list()
            actions = get_queue_stream(ack_list, bulk_timeout)
            bulker = es_helpers.streaming_bulk(
                es_client, actions, chunk_size=BULK_SIZE)

            while True:
                for response in bulker:
                    msg = ack_list.pop(0)
                    msg_ok = response[0]

                    if msg_ok:
                        msg.ack()

        except Exception as ex:
            _LOG.exception(ex)


class ElasticSearchStreamBulker(object):
    """
    Controls a mutliprocess pool that pulls a message stream from a queue and
    bulk flushes to elasticsearch
    """
    def __init__(self, bulk_size=BULK_SIZE, bulk_timeout=60, concurrency=None):
        self.bulk_size = bulk_size
        self.bulk_timeout = bulk_timeout
        self.concurrency = concurrency
        if self.concurrency is None:
            self.concurrency = cpu_count()
        self.pool = None

    def start(self):
        """
        Start a process pool to handle streaming
        """
        self.pool = Pool(self.concurrency)

        def signal_handler(signal, frame):
            self.pool.close()
            self.pool.join()
            _LOG.info("ES_FLusher stopped")
            sys.exit(0)

        _LOG.info("ES_FLusher starting process pool")
        self.pool.map(
            flush_to_es, [self.bulk_timeout for x in range(self.concurrency)])
