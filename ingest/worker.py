import os
import time
import json
import traceback
import datetime as dt
import psycopg2
from psycopg2.extras import Json

# Reuse logic from app.py (pool + processing)
from app import process_record, borrow_conn, release_conn

MAX_ATTEMPTS = int(os.getenv("INGEST_MAX_ATTEMPTS", "5"))
BACKOFF_BASE = float(os.getenv("INGEST_BACKOFF_BASE", "5"))  # seconds
BACKOFF_MAX = float(os.getenv("INGEST_BACKOFF_MAX", "300"))  # seconds
IDLE_SLEEP = float(os.getenv("INGEST_IDLE_SLEEP", "1.0"))
LOCK_SECS = float(os.getenv("INGEST_LOCK_SECS", "30"))


def claim_job():
    c = borrow_conn()
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """
                WITH cte AS (
                  SELECT id
                  FROM ingest_queue
                  WHERE status='queued' AND (locked_until IS NULL OR locked_until < now())
                  ORDER BY received_at
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE ingest_queue q
                SET status='processing', locked_until = now() + INTERVAL %s
                FROM cte
                WHERE q.id = cte.id
                RETURNING q.id, q.parent_path, q.ci_rn, q.ct, q.payload, q.attempts
                """,
                (f"{int(LOCK_SECS)} seconds",),
            )
            row = cur.fetchone()
            if not row:
                return None
            job = {
                "id": row[0],
                "parent_path": row[1],
                "ci_rn": row[2],
                "ct": row[3],
                "payload": row[4],
                "attempts": row[5],
            }
            return job
    finally:
        release_conn(c)


def mark_done(job_id):
    c = borrow_conn()
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE ingest_queue
                SET status='done', processed_at=now(), locked_until=NULL
                WHERE id=%s
                """,
                (job_id,),
            )
    finally:
        release_conn(c)


def requeue_with_backoff(job_id, attempts):
    delay = min(BACKOFF_BASE * (2 ** attempts), BACKOFF_MAX)
    locked_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)
    c = borrow_conn()
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE ingest_queue
                SET attempts = attempts + 1,
                    status='queued',
                    locked_until = %s
                WHERE id=%s
                """,
                (locked_until, job_id),
            )
    finally:
        release_conn(c)
    return delay


def move_to_dead_letter(job, error_text):
    c = borrow_conn()
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_dead_letter (parent_path, ci_rn, ct, payload, attempts, last_error)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    job.get("parent_path"),
                    job.get("ci_rn"),
                    job.get("ct"),
                    Json(job.get("payload")),
                    job.get("attempts", 0) + 1,
                    error_text[:1000],
                ),
            )
            cur.execute(
                """
                UPDATE ingest_queue SET status='failed', processed_at=now(), locked_until=NULL, attempts = attempts + 1
                WHERE id=%s
                """,
                (job["id"],),
            )
    finally:
        release_conn(c)


def ensure_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {"raw": v}
    return v


def main():
    print("ingest-worker: starting (pool-based connections)")

    while True:
        try:
            job = claim_job()
            if not job:
                time.sleep(IDLE_SLEEP)
                continue

            parent = job.get("parent_path")
            ci_rn = job.get("ci_rn") or f"cin-job-{job['id']}"
            ct = job.get("ct")
            con = ensure_dict(job.get("payload"))

            try:
                c = borrow_conn()
                try:
                    process_record(c, parent, ci_rn, ct, con)
                finally:
                    release_conn(c)
                mark_done(job["id"])
                print(f"ingest-worker: processed job id={job['id']} ci_rn={ci_rn}")
            except Exception as e:
                err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                if (job.get("attempts", 0) + 1) >= MAX_ATTEMPTS:
                    move_to_dead_letter(job, err)
                    print(f"ingest-worker: job id={job['id']} moved to dead-letter after attempts={job.get('attempts',0)+1}")
                else:
                    delay = requeue_with_backoff(job["id"], job.get("attempts", 0))
                    print(f"ingest-worker: job id={job['id']} requeued with backoff {delay:.1f}s due to error: {e}")
        except Exception as loop_exc:
            print(f"ingest-worker: loop error: {loop_exc}")
            time.sleep(2)


if __name__ == "__main__":
    main()
