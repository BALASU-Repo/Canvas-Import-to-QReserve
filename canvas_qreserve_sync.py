import json
import os
import re
import requests
from datetime import datetime, timedelta, timezone

CANVAS_BASE_URL = "https://canvas.asu.edu/api/v1/"

#======================= Safer Seas =======================
COURSE_ID = "252864"
QUIZ_ID = "1989166"

QRESERVE_SITE_ID = "qrmfmhjzfgyi33n551936oiq0n0pgfwkmnjg4"
QRESERVE_CREDENTIAL_IDS = [
    "nv78xb4w5snaism4fvvay7meefmgxi418t3yl",
    "bv5tne89bsrctvvjta540btuipvpmlkow2qdy",
    "5dsyhhp7g4ev5b8x0ya5esyn4ahv7gal6inu5",
]
#======================= Safer Seas =======================

CANVAS_ACCESS_TOKEN = os.getenv("CANVAS_ACCESS_TOKEN")
QRESERVE_BOT_TOKEN = os.getenv("QRESERVE_BOT_TOKEN")

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "1440"))
AWARD_CACHE_FILE = os.getenv("AWARD_CACHE_FILE", ".qreserve_award_cache.json")


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


def normalize_name(value):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_first_last_from_text(name_text):
    name_text = (name_text or "").strip()
    if not name_text:
        return "", ""

    if "," in name_text:
        last, rest = name_text.split(",", 1)
        first = rest.strip().split()[0] if rest.strip() else ""
        return first, last.strip()

    parts = name_text.split()
    if len(parts) == 1:
        return parts[0], ""

    return parts[0], parts[-1]


def extract_first_last(user_obj):
    if not isinstance(user_obj, dict):
        return "", ""

    first = (
        user_obj.get("first_name")
        or user_obj.get("given_name")
        or user_obj.get("first")
        or ""
    ).strip()
    last = (
        user_obj.get("last_name")
        or user_obj.get("family_name")
        or user_obj.get("surname")
        or user_obj.get("last")
        or ""
    ).strip()

    if first and last:
        return first, last

    for field in ("sortable_name", "name", "full_name", "display_name"):
        candidate = (user_obj.get(field) or "").strip()
        if candidate:
            parsed_first, parsed_last = extract_first_last_from_text(candidate)
            if parsed_first and parsed_last:
                return parsed_first, parsed_last

    return first, last


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

        login_id = (user.get("login_id") or "").strip().lower()
        email = (user.get("email") or "").strip().lower()

        if login_id:
            email_lookup[user_id] = f"{login_id}@asu.edu"
        elif email:
            email_lookup[user_id] = email

    return email_lookup


def get_qreserve_user_maps(site_id, headers):
    url = f"https://api.qreserve.com/sites/{site_id}/users"
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()

    payload = res.json()
    if isinstance(payload, dict):
        users = payload.get("data") or payload.get("users") or payload.get("site_users") or []
    elif isinstance(payload, list):
        users = payload
    else:
        users = []

    email_to_user_id = {}
    name_to_user_ids = {}

    for site_user in users:
        if not isinstance(site_user, dict):
            continue

        user = site_user.get("user", site_user)
        if not isinstance(user, dict):
            continue

        email = (user.get("email") or "").strip().lower()
        user_id = user.get("user_id") or user.get("id")
        first, last = extract_first_last(user)
        name_key = f"{normalize_name(first)} {normalize_name(last)}".strip() if first or last else ""

        if email and user_id:
            email_to_user_id[email] = user_id

        if name_key and user_id:
            name_to_user_ids.setdefault(name_key, []).append(user_id)

    return email_to_user_id, name_to_user_ids


def get_canvas_user_name(canvas_headers, user_id, user_obj=None):
    if isinstance(user_obj, dict):
        first, last = extract_first_last(user_obj)
        if first and last:
            return first, last

    profile_url = f"{CANVAS_BASE_URL}/users/{user_id}/profile"
    res = requests.get(profile_url, headers=canvas_headers, timeout=30)
    res.raise_for_status()
    profile = res.json()
    return extract_first_last(profile)


def load_award_cache():
    try:
        with open(AWARD_CACHE_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            return payload
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"WARNING: Could not read award cache ({AWARD_CACHE_FILE}): {e}")
    return {}


def save_award_cache(cache):
    try:
        with open(AWARD_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
    except Exception as e:
        print(f"WARNING: Could not write award cache ({AWARD_CACHE_FILE}): {e}")


def cache_key(qreserve_user_id, credential_id):
    return f"{qreserve_user_id}:{credential_id}"


def get_qreserve_user_credentials(qreserve_user_id, qreserve_headers):
    candidate_urls = [
        f"https://api.qreserve.com/training/records?user_id={qreserve_user_id}",
        f"https://api.qreserve.com/training/records?site_id={QRESERVE_SITE_ID}&user_id={qreserve_user_id}",
        f"https://api.qreserve.com/users/{qreserve_user_id}/training_records",
        f"https://api.qreserve.com/users/{qreserve_user_id}/credentials",
    ]

    credential_ids = set()

    for url in candidate_urls:
        try:
            res = requests.get(url, headers=qreserve_headers, timeout=30)
        except Exception:
            continue

        if res.status_code == 404:
            continue
        if res.status_code not in (200, 201):
            continue

        try:
            payload = res.json()
        except Exception:
            continue

        if isinstance(payload, dict):
            records = (
                payload.get("data")
                or payload.get("records")
                or payload.get("training_records")
                or payload.get("credentials")
                or payload.get("results")
                or []
            )
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        for record in records:
            if not isinstance(record, dict):
                continue
            for key in ("credential_id", "training_id", "credential", "id", "training_record_id"):
                value = record.get(key)
                if value:
                    credential_ids.add(str(value))
            nested = record.get("credential")
            if isinstance(nested, dict):
                for key in ("credential_id", "id"):
                    value = nested.get(key)
                    if value:
                        credential_ids.add(str(value))

        if credential_ids:
            break

    return credential_ids


def user_already_has_credential(qreserve_user_id, credential_id, qreserve_headers, award_cache):
    if credential_id in get_qreserve_user_credentials(qreserve_user_id, qreserve_headers):
        award_cache[cache_key(qreserve_user_id, credential_id)] = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "qreserve",
        }
        return True

    return cache_key(qreserve_user_id, credential_id) in award_cache


def award_qreserve_credentials(qreserve_user_id, qreserve_headers, award_cache):
    for credential_id in QRESERVE_CREDENTIAL_IDS:
        if user_already_has_credential(qreserve_user_id, credential_id, qreserve_headers, award_cache):
            print(f"       SKIP: Credential {credential_id} already exists for QReserve user {qreserve_user_id}.")
            continue

        qreserve_url = f"https://api.qreserve.com/training/record_add/{credential_id}"

        payload = {
            "user_id": qreserve_user_id,
            "earned_on": datetime.now().strftime("%Y-%m-%d"),
            "silent": False,
            "return_data": True,
        }

        res = requests.post(qreserve_url, json=payload, headers=qreserve_headers, timeout=30)

        if res.status_code not in (200, 201):
            print(f"       FAILURE: {res.status_code} {res.text[:200]}")
            return False

        award_cache[cache_key(qreserve_user_id, credential_id)] = {
            "awarded_at": datetime.now(timezone.utc).isoformat(),
            "source": "script",
        }
        print(f"       SUCCESS: Granted credential {credential_id} to QReserve user {qreserve_user_id}!")

    return True


def run_sync_pipeline():
    canvas_token = require_env(CANVAS_ACCESS_TOKEN, "CANVAS_ACCESS_TOKEN")
    qreserve_token = require_env(QRESERVE_BOT_TOKEN, "QRESERVE_BOT_TOKEN")

    print(f"[{datetime.now()}] Initializing orientation sync loop...")

    canvas_headers = {"Authorization": f"Bearer {canvas_token}"}
    qreserve_headers = {
        "Authorization": qreserve_token,
        "Content-Type": "application/json",
    }

    try:
        qreserve_email_map, qreserve_name_map = get_qreserve_user_maps(QRESERVE_SITE_ID, qreserve_headers)
        print(f"DEBUG: QReserve map contains {len(qreserve_email_map)} emails and {len(qreserve_name_map)} names.")
    except Exception as e:
        print(f"CRITICAL: Could not fetch QReserve user map. Details: {e}")
        return

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
    time_window = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)

    award_cache = load_award_cache()
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

        first_name, last_name = get_canvas_user_name(canvas_headers, user_id, user_obj)

        if not student_email:
            print(f"    ↳ ERROR: Could not resolve email for Canvas user {user_id}.")
            continue

        print(f"    ↳ MATCH FOUND: Pushing {student_email} to QReserve.")

        qreserve_user_id = qreserve_email_map.get(student_email.lower())
        lookup_mode = "email"

        if not qreserve_user_id and first_name and last_name:
            name_key = f"{normalize_name(first_name)} {normalize_name(last_name)}".strip()
            matches = qreserve_name_map.get(name_key, [])
            if len(matches) == 1:
                qreserve_user_id = matches[0]
                lookup_mode = "name"
                print(f"       EMAIL MISS: Falling back to QReserve name match for {first_name} {last_name}.")
            elif len(matches) > 1:
                print(f"       ERROR: Multiple QReserve users matched name {first_name} {last_name}; skipping.")
                continue

        if not qreserve_user_id:
            if first_name and last_name:
                print(f"       ERROR: {student_email} not found in QReserve site users by email or name ({first_name} {last_name}).")
            else:
                print(f"       ERROR: {student_email} not found in QReserve site users.")
            continue

        if lookup_mode == "name":
            print(f"       NAME MATCH FOUND: Pushing QReserve user {qreserve_user_id} to credentials.")

        if award_qreserve_credentials(qreserve_user_id, qreserve_headers, award_cache):
            processed_count += 1
            save_award_cache(award_cache)

    save_award_cache(award_cache)
    print(f"Sync complete. Processed {processed_count} passing submissions.")


if __name__ == "__main__":
    run_sync_pipeline()
