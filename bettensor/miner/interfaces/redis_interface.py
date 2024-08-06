import json
import redis
import bittensor as bt
import uuid
import time

class RedisInterface:
    def __init__(self, host="localhost", port=6379):
        self.host = host
        self.port = port
        self.redis_client = None
        self.is_connected = False

    def connect(self):
        bt.logging.info("Initializing Redis connection")
        try:
            self.redis_client = redis.Redis(
                host=self.host,
                port=self.port,
                socket_connect_timeout=5
            )
            self.redis_client.ping()
            self.is_connected = True
            bt.logging.info("Redis connection successful")
            return True
        except redis.ConnectionError:
            bt.logging.warning("Failed to connect to Redis server. GUI interfaces will not be available. Only CLI will work.")
            self.is_connected = False
            return False

    def publish(self, channel, message):
        if not self.is_connected:
            bt.logging.warning("Redis is not connected. Cannot publish message.")
            return False
        try:
            self.redis_client.publish(channel, message)
            return True
        except Exception as e:
            bt.logging.error(f"Error publishing to Redis: {e}")
            return False

    def subscribe(self, channel):
        if not self.is_connected:
            bt.logging.warning("Redis is not connected. Cannot subscribe to channel.")
            return None
        try:
            pubsub = self.redis_client.pubsub()
            pubsub.subscribe(channel)
            return pubsub
        except Exception as e:
            bt.logging.error(f"Error subscribing to Redis channel: {e}")
            return None

    def get(self, key):
        if not self.is_connected:
            bt.logging.warning("Redis is not connected. Cannot get value.")
            return None
        try:
            return self.redis_client.get(key)
        except Exception as e:
            bt.logging.error(f"Error getting value from Redis: {e}")
            return None

    def set(self, key, value):
        if not self.is_connected:
            bt.logging.warning("Redis is not connected. Cannot set value.")
            return False
        try:
            self.redis_client.set(key, value)
            return True
        except Exception as e:
            bt.logging.error(f"Error setting value in Redis: {e}")
            return False

    def execute_db_operation(self, operation, **params):
        if not self.is_connected:
            bt.logging.warning("Redis is not connected. Cannot execute database operation.")
            return None
        try:
            message_id = str(uuid.uuid4())
            message = {
                'id': message_id,
                'operation': operation,
                'params': params
            }
            self.publish('db_operations', json.dumps(message))
            result = self.wait_for_result(message_id)
            return json.loads(result) if result else None
        except Exception as e:
            bt.logging.error(f"Error executing database operation: {e}")
            return None

    def wait_for_result(self, message_id, timeout=5):
        start_time = time.time()
        while time.time() - start_time < timeout:
            result = self.redis_client.get(f"result:{message_id}")
            if result:
                self.redis_client.delete(f"result:{message_id}")
                return result
            time.sleep(0.1)
        raise TimeoutError("Timeout waiting for database operation result")