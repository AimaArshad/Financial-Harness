from celery_app import celery
from harness import dashboard_data as dd

# error names that are worth retrying (temporary problems)
TRANSIENT = ("APIConnectionError", "APITimeoutError", "ConnectionError", "TimeoutError", "InternalServerError")


@celery.task(bind=True, name="fh.chat", max_retries=3)
def chat_task(self, history, goal_name, fake):
    def progress(pct, msg):
        self.update_state(state="PROGRESS", meta={"pct": pct, "msg": msg})

    result = dd.get_chat_answer(history, goal_name, fake, on_progress=progress)

    if "error" in result and result["error"].startswith(TRANSIENT):
        # wait 5s, then 10s, then 20s between attempts; after 3 retries the task is marked FAILED
        raise self.retry(exc=RuntimeError(result["error"]), countdown=5 * 2 ** self.request.retries)

    return result