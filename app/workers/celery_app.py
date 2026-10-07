from __future__ import annotations

from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

celery_app = Celery("natheel", broker=settings.redis_url, backend=settings.redis_url,
                    include=["app.workers.publishing_tasks", "app.workers.scheduling_tasks"])

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_acks_late=True,                 # a task is only acknowledged after it finishes...
    task_reject_on_worker_lost=True,     # ...and redelivered if the worker dies (claim() makes that safe)
    worker_prefetch_multiplier=1,        # long uploads: don't hoard tasks
    task_soft_time_limit=1500,           # SoftTimeLimitExceeded -> retryable PUBLISH_TIMEOUT
    task_time_limit=1800,
    broker_transport_options={"visibility_timeout": 7200},
    worker_hijack_root_logger=False,     # keep our JSON logging
    beat_schedule={
        "dispatch-due-schedules": {"task": "scheduling.dispatch_due_schedules", "schedule": 30.0},
        "refresh-expiring-tokens": {"task": "scheduling.refresh_expiring_tokens", "schedule": 3600.0},
        "recover-stale-publications": {"task": "scheduling.recover_stale_publications", "schedule": 600.0},
    },
)
