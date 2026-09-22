import redis
from rq import Queue

from app.core.config import get_settings

settings = get_settings()
redis_conn = redis.from_url(settings.REDIS_URL)
image_queue = Queue(settings.RQ_QUEUE_NAME, connection=redis_conn)
banner_queue = Queue(settings.BANNER_QUEUE_NAME, connection=redis_conn)