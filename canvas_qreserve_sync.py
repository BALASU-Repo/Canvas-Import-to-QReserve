import os
import requests
from datetime import datetime, timedelta, timezone

CANVAS_BASE_URL = "https://canvas.asu.edu/api/v1/"
COURSE_ID = "252864"
QUIZ_ID = "1989166"

QRESERVE_SITE_ID = "qrmfmhjzfgyi33n551936oiq0n0pgfwkmnjg4"
QRESERVE_CREDENTIAL_ID = "nv78xb4w5snaism4fvvay7meefmgxi418t3yl"

CANVAS_ACCESS_TOKEN = os.getenv("CANVAS_ACCESS_TOKEN")
QRESERVE_BOT_TOKEN = os.getenv("QRESERVE_BOT_TOKEN")

# Set this to the QReserve email lookup endpoint your site exposes.
# The public docs confirm the lookup behavior, but I did not verify the exact REST path.
QRESERVE_USER_LOOKUP_URL = os.getenv("QRESERVE_USER_LOOKUP_URL")

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "100"))


def require_env(value, name):
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_canvas_collection(url, headers, params=None, collection_key=None):
    items = []

    while url:
        res = requests.get(url, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        payload = res.json()

        if collection_key is None:
            page_items = payload
        else:
            page_items = payload.get(collection_key, [])

        items.extend(page_items)

        url = res.links.get("next", {}).get("url")
        params = None

    return items


def load_canvas_email_lookup(canvas_headers):
    enrollment_url = f"{CANVAS_BASE_URL}/courses/{COURSE_ID}/enrollments"
    enrollment_params = [("per_page", 100), ("type[]", "StudentEnrollment")]

    email_lookup = {}
    enrollments = get_canvas_collection(
        enrollment_url,
        canvas_headers,
        params=enrollment_params,
        collection_key=None,
    )

    for enroll in enrollments:
        user = enroll.get("user", {}) or {}
        user_id = str(enroll.get("user_id"))

        # Canvas sometimes gives us the ASURITE login_id directly, and sometimes only email.
        login_id = (user.get("login_id") or "").strip().lower()
        email = (user.get("email") or "").strip().lower()

        if login_id:
            email_lookup[user_id] = f"{login_id}@asu.edu"
        elif email:
            email_lookup[user_id] = email

    return email_lookup


def get_qreserve_user_id_from_email(student_email, qreserve_headers, cache):
    key = student_email.strip().lower()
    if key in cache:
        return cache[key]

    if not QRESERVE_USER_LOOKUP_URL:
        raise RuntimeError("Missing required environment variable: QRESERVE_USER_LOOKUP_URL")

    res = requests.get(
        QRESERVE_USER_LOOKUP_URL,
        headers=qreserve_headers,
        params={
            "email": key,
            "include_unverified": "true",
        },
        timeout=30,
    )
    res.raise_for_status()
    payload = res.json()

    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        user_id = data.get("user_id")
        if user_id:
            cache[key] = user_id
            return user_id

    raise LookupError(f"Could not resolve QReserve user for {student_email}")


def award_qreserve_credential(qreserve_user_id, qreserve_headers):
    qreserve_url = f"https://api.qreserve.com/training/record_add/{QRESERVE_CREDENTIAL_ID}"

    payload = {
        "user_id": qreserve_user_id,
        "earned_on": datetime.now().strftime("%Y-%m-%d"),
        "silent": False,
        "return_data": True,
    }

    res = requests.post(qreserve_url, json=payload, headers=qreserve_headers, timeout=30)

    if res.status_code in (200, 201):
        print(f"       SUCCESS: Granted orientation to QReserve user {qreserve_user_id}!")
        return True

    print(f"       FAILURE: {res.status_code} {res.text[:200]}")
    return False


def run_sync_pipeline():
    canvas_token = require_env(CANVAS_ACCESS_TOKEN, "CANVAS_ACCESS_TOKEN")
    qreserve_token = require_env(QRESERVE_BOT_TOKEN, "QRESERVE_BOT_TOKEN")

    print(f"[{datetime.now()}] Initializing orientation sync loop...")

    canvas_headers = {"Authorization": f"Bearer {canvas_token}"}
    qreserve_headers = {
        "Authorization": qreserve_token,
        "Content-Type": "application/json",
    }

    submission_url = f"{CANVAS_BASE_URL}/courses/{COURSE_ID}/quizzes/{QUIZ_ID}/submissions"
    submission_params = [("per_page", 100), ("include[]", "user")]

    try:
        submissions = get_canvas_collection(
            submission_url,
            canvas_headers,
            params=submission_params,
            collection_key="quiz_submissions",
        )
    except Exception as e:
        print(f"CRITICAL: Failed to query Canvas API. Details: {e}")
        return

    print(f"DEBUG: Found {len(submissions)} raw submissions in Canvas payload.")

    canvas_email_lookup = {}
    qreserve_user_cache = {}
    time_window = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)

    processed_count = 0

    for sub in submissions:
        user_id = str(sub.get("user_id"))
        score = sub.get("score")
        workflow = sub.get("workflow_state")

        print(f" -> Inspecting Submission ID {sub.get('id')} (User: {user_id}, Score: {score}, State: {workflow})")

        if workflow != "complete" or score != 100.0:
            continue

        finished_at_str = sub.get("finished_at")
        if not finished_at_str:
            continue

        finished_at = datetime.fromisoformat(finished_at_str.replace("Z", "+00:00"))
        if finished_at <= time_window:
            continue

        user_obj = sub.get("user", {}) or {}
        student_email = (user_obj.get("email") or "").strip().lower()

        if not student_email:
            login_id = (user_obj.get("login_id") or "").strip().lower()
            if login_id:
                student_email = f"{login_id}@asu.edu"

        if not student_email:
            if not canvas_email_lookup:
                try:
                    canvas_email_lookup = load_canvas_email_lookup(canvas_headers)
                except Exception as e:
                    print(f"WARNING: Could not fetch Canvas roster mapping. Details: {e}")
                    continue

            student_email = canvas_email_lookup.get(user_id, "")

        if not student_email:
            print(f"    ↳ ERROR: Could not resolve email for Canvas user {user_id}.")
            continue

        print(f"    ↳ MATCH FOUND: Pushing {student_email} to QReserve.")

        try:
            qreserve_user_id = get_qreserve_user_id_from_email(
                student_email,
                qreserve_headers,
                qreserve_user_cache,
            )
        except Exception as e:
            print(f"       ERROR: Could not resolve QReserve user for {student_email}. Details: {e}")
            continue

        if award_qreserve_credential(qreserve_user_id, qreserve_headers):
            processed_count += 1

    print(f"Sync complete. Processed {processed_count} passing submissions.")


if __name__ == "__main__":
    run_sync_pipeline()
