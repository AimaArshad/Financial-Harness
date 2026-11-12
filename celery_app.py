import os
from celery import Celery

REDIS = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
celery = Celery("fh", broker=REDIS, backend=REDIS, include=["tasks"])
celery.conf.update(
    task_track_started=True,
    result_expires=86400,
    task_time_limit=300,
    worker_redirect_stdouts=False,
)