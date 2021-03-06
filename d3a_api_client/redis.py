import logging
import json
from enum import Enum
from functools import wraps
from redis import StrictRedis
from d3a_interface.utils import wait_until_timeout_blocking
from d3a_api_client import APIClientInterface
from concurrent.futures.thread import ThreadPoolExecutor


root_logger = logging.getLogger()
root_logger.setLevel(logging.ERROR)


class RedisAPIException(Exception):
    pass


def registered_connection(f):
    @wraps(f)
    def wrapped(self, *args, **kwargs):
        if not self.is_active:
            raise RedisAPIException(f'Registration has not completed yet.')
        return f(self, *args, **kwargs)
    return wrapped


class Commands(Enum):
    OFFER = 1
    BID = 2
    DELETE_OFFER = 3
    DELETE_BID = 4
    LIST_OFFERS = 5
    LIST_BIDS = 6
    LIST_STATS = 7


class RedisClient(APIClientInterface):
    def __init__(self, market_id, client_id, autoregister=True, redis_url='redis://localhost:6379'):
        super().__init__(market_id, client_id, autoregister, redis_url)
        self.redis_db = StrictRedis.from_url(redis_url)
        self.pubsub = self.redis_db.pubsub()
        self.market_id = market_id
        self.client_id = client_id
        self.is_active = False
        self._blocking_command_responses = {}
        self._subscribe_to_response_channels()
        self.executor = ThreadPoolExecutor(max_workers=5)
        if autoregister:
            self.register(is_blocking=False)

    def _subscribe_to_response_channels(self):
        channel_subs = {
            f"{self._command_topics[c]}/response": self._generate_command_response_callback(c)
            for c in Commands
        }

        channel_subs[f'{self.market_id}/register_participant/response'] = self._on_register
        channel_subs[f'{self.market_id}/unregister_participant/response'] = self._on_unregister
        channel_subs[f'{self._channel_prefix}/market_cycle'] = self._on_market_cycle
        self.pubsub.subscribe(**channel_subs)
        self.pubsub.run_in_thread(daemon=True)

    def register(self, is_blocking=False):
        logging.info(f"Trying to register to {self.market_id} as client {self.client_id}")
        if self.is_active:
            raise RedisAPIException(f'API is already registered to the market.')

        self.redis_db.publish(f'{self.market_id}/register_participant',
                              json.dumps({"name": self.client_id}))
        if is_blocking:
            try:
                wait_until_timeout_blocking(lambda: self.is_active, timeout=120)
            except AssertionError:
                raise RedisAPIException(
                    f'API registration process timed out. Server will continue processing your '
                    f'request on the background and will notify you as soon as the registration '
                    f'has been completed.')

    def unregister(self, is_blocking=False):
        logging.info(f"Trying to unregister from {self.market_id} as client {self.client_id}")

        if not self.is_active:
            raise RedisAPIException(f'API is already unregistered from the market.')

        self.redis_db.publish(f'{self.market_id}/unregister_participant',
                              json.dumps({"name": self.client_id}))
        if is_blocking:
            try:
                wait_until_timeout_blocking(lambda: not self.is_active, timeout=120)
            except AssertionError:
                raise RedisAPIException(
                    f'API unregister process timed out. Server will continue processing your '
                    f'request on the background and will notify you as soon as the unregistration '
                    f'has been completed.')

    @property
    def _channel_prefix(self):
        return f"{self.market_id}/{self.client_id}"

    @property
    def _command_topics(self):
        return {
            Commands.OFFER: f'{self._channel_prefix}/offer',
            Commands.BID: f'{self._channel_prefix}/bid',
            Commands.DELETE_OFFER: f'{self._channel_prefix}/delete_offer',
            Commands.DELETE_BID: f'{self._channel_prefix}/delete_bid',
            Commands.LIST_OFFERS: f'{self._channel_prefix}/offers',
            Commands.LIST_BIDS: f'{self._channel_prefix}/bids',
            Commands.LIST_STATS: f'{self._channel_prefix}/stats',

        }

    def _wait_and_consume_command_response(self, command_type):
        wait_until_timeout_blocking(lambda: command_type in self._blocking_command_responses)
        command_output = self._blocking_command_responses.pop(command_type)
        logging.info(f"Command {command_type} got response {command_output}")
        return command_output

    def _generate_command_response_callback(self, command_type):
        def _command_received(msg):
            try:
                message = json.loads(msg["data"])
            except Exception as e:
                logging.error(f"Received incorrect response on command {command_type}. "
                              f"Response {msg}. Error {e}.")
                return
            logging.debug(f"Command {command_type} received response: {message}")
            if 'error' in message:
                logging.error(f"Error when receiving {command_type} command response."
                              f"Error output: {message}")
                return
            else:
                self._blocking_command_responses[command_type] = message
        return _command_received

    @registered_connection
    def offer_energy(self, energy, price):
        logging.debug(f"Client tries to place an offer for {energy} kWh at {price} cents.")
        self.redis_db.publish(
            self._command_topics[Commands.OFFER],
            json.dumps({"energy": energy, "price": price})
        )
        return self._wait_and_consume_command_response(Commands.OFFER)

    @registered_connection
    def bid_energy(self, energy, price):
        logging.debug(f"Client tries to place a bid for {energy} kWh at {price} cents.")
        self.redis_db.publish(
            self._command_topics[Commands.BID],
            json.dumps({"energy": energy, "price": price})
        )
        return self._wait_and_consume_command_response(Commands.BID)

    @registered_connection
    def delete_offer(self, offer_id):
        logging.debug(f"Client tries to delete offer {offer_id}.")
        self.redis_db.publish(
            self._command_topics[Commands.DELETE_OFFER],
            json.dumps({"offer": offer_id})
        )
        return self._wait_and_consume_command_response(Commands.DELETE_OFFER)

    @registered_connection
    def delete_bid(self, bid_id):
        logging.debug(f"Client tries to delete bid {bid_id}.")
        self.redis_db.publish(
            self._command_topics[Commands.DELETE_BID],
            json.dumps({"bid": bid_id})
        )
        return self._wait_and_consume_command_response(Commands.DELETE_BID)

    @registered_connection
    def list_offers(self):
        logging.debug(f"Client tries to read its posted offers.")
        self.redis_db.publish(self._command_topics[Commands.LIST_OFFERS], json.dumps(""))
        return self._wait_and_consume_command_response(Commands.LIST_OFFERS)

    @registered_connection
    def list_bids(self):
        logging.debug(f"Client tries to read its posted bids.")
        self.redis_db.publish(self._command_topics[Commands.LIST_BIDS], json.dumps(""))
        return self._wait_and_consume_command_response(Commands.LIST_BIDS)

    @registered_connection
    def list_stats(self):
        logging.debug(f"Client tries to read its posted bids.")
        self.redis_db.publish(self._command_topics[Commands.LIST_STATS], json.dumps(""))
        return self._wait_and_consume_command_response(Commands.LIST_STATS)

    def _on_register(self, msg):
        message = json.loads(msg["data"])
        if 'available_publish_channels' not in message or \
                'available_subscribe_channels' not in message:
            raise RedisAPIException(f'Registration to the market {self.market_id} failed.')

        logging.info(f"Client was registered to market: {message}")
        self.is_active = True

        def executor_function():
            self.on_register(message)
        self.executor.submit(executor_function)

    def _on_unregister(self, msg):
        message = json.loads(msg["data"])
        self.is_active = False
        if message["response"] != "success":
            raise RedisAPIException(f'Failed to unregister from market {self.market_id}.'
                                    f'Deactivating connection.')

    def _on_market_cycle(self, msg):
        message = json.loads(msg["data"])
        logging.info(f"A new market was created. Market information: {message}")

        def executor_function():
            self.on_market_cycle(message)
        self.executor.submit(executor_function)

    def on_register(self, registration_info):
        pass

    def on_market_cycle(self, market_info):
        pass
