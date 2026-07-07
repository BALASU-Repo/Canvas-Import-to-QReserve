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

def require_env(value, name):
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_canvas_collection(url, headers, params=None, collection_key=None):
    items = []

    while url:
        res = requests.get(url, headers=headers, params=params)
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


def get_qreserve_user_map(site_id, headers):
    url = f"https://api.qreserve.com/sites/{site_id}/users"
    res = requests.get(url, headers=headers)
    res.raise_for_status()

    payload = res.json()
    users = payload.get("data", []) if isinstance(payload, dict) else payload

    email_to_user_id = {}
    for site_user in users:
        user = site_user.get("user", {})
        email = (user.get("email") or "").strip().lower()
        user_id = user.get("user_id")
        if email and user_id:
            email_to_user_id[email] = user_id

    return email_to_user_id


def award_qreserve_credential(qreserve_user_id, qreserve_headers):
    qreserve_url = f"https://api.qreserve.com/training/record_add/{QRESERVE_CREDENTIAL_ID}"

    payload = {
        "user_id": qreserve_user_id,
        "earned_on": datetime.now().strftime("%Y-%m-%d"),
        "silent": False,
        "return_data": True,
    }

    res = requests.post(qreserve_url, json=payload, headers=qreserve_headers)

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

    enrollment_url = f"{CANVAS_BASE_URL}/courses/{COURSE_ID}/enrollments"
    enrollment_params = {"per_page": 100, "type[]": "StudentEnrollment"}

    email_lookup = {}
    try:
        enrollments = get_canvas_collection(
            enrollment_url,
            canvas_headers,
            params=enrollment_params,
            collection_key=None,
        )

        for enroll in enrollments:
            user = enroll.get("user", {})
            user_id = str(enroll.get("user_id"))
            asurite = user.get("login_id")
            if asurite:
                email_lookup[user_id] = f"{asurite.strip()}@asu.edu"
    except Exception as e:
        print(f"WARNING: Could not fetch roster mapping. Details: {e}")

    try:
        qreserve_user_map = get_qreserve_user_map(QRESERVE_SITE_ID, qreserve_headers)
        print(f"DEBUG: QReserve map contains {len(qreserve_user_map)} users.")
    except Exception as e:
        print(f"WARNING: Could not fetch QReserve user map. Details: {e}")
        qreserve_user_map = {}

    canvas_url = f"{CANVAS_BASE_URL}/courses/{COURSE_ID}/quizzes/{QUIZ_ID}/submissions"

    try:
        submissions = get_canvas_collection(
            canvas_url,
            canvas_headers,
            params={"per_page": 100},
            collection_key="quiz_submissions",
        )
    except Exception as e:
        print(f"CRITICAL: Failed to query Canvas API. Details: {e}")
        return

    print(f"DEBUG: Found {len(submissions)} raw submissions in Canvas payload.")
    print(f"DEBUG: Roster map contains {len(email_lookup)} translated ASURITE accounts.")

    time_window = datetime.now(timezone.utc) - timedelta(minutes=100)
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

        student_email = email_lookup.get(user_id)
        if not student_email:
            print(f"    ↳ ERROR: ASURITE login_id missing from enrollment roster for user {user_id}.")
            continue

        print(f"    ↳ MATCH FOUND: Pushing {student_email} to QReserve.")

        qreserve_user_id = qreserve_user_map.get(student_email.lower())
        if not qreserve_user_id:
            print(f"       ERROR: {student_email} not found in QReserve site users.")
            continue

        if award_qreserve_credential(qreserve_user_id, qreserve_headers):
            processed_count += 1

    print(f"Sync complete. Processed {processed_count} passing submissions.")


if __name__ == "__main__":
    run_sync_pipeline()