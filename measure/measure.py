"""Posts the reviews `plan.json` asks for on this pull request, and prints GitHub's every answer.

Each review is one `POST /pulls/{number}/reviews` with `event: COMMENT`, as KeePassGate's
`tools/suppression_review.py` makes it. Every answer is printed as one `RESULT` line of JSON: the
status, how long the request took, GitHub's message and the rate limit headers, and for a review
that was made, how many comments it holds. A refusal ends the run unless the plan asks for retries,
and then the wait is the one GitHub's documentation gives: `retry-after` when it is there, the
`x-ratelimit-reset` time when `x-ratelimit-remaining` is 0, and otherwise a minute, doubled on each
refusal after the first. The run always succeeds, since what it measures is in what it prints.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HEADERS = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-used",
           "x-ratelimit-reset", "x-ratelimit-resource", "x-github-request-id")
API = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}"
TOKEN = os.environ["GITHUB_TOKEN"]
NUMBER = int(os.environ["PR"])
HEAD = os.environ["HEAD"]


def call(method: str, url: str, body: object = None) -> tuple[int, dict, str, float]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url if url.startswith("http") else API + url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
            return response.status, dict(response.headers), text, time.monotonic() - started
    except urllib.error.HTTPError as refused:
        text = refused.read().decode(errors="replace")
        return refused.code, dict(refused.headers), text, time.monotonic() - started


def header(headers: dict, name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name), None)


def comments_of(review: int) -> int:
    count, page = 0, 1
    while True:
        status, _, text, _ = call("GET", f"/pulls/{NUMBER}/reviews/{review}/comments"
                                         f"?per_page=100&page={page}")
        if status != 200:
            return -1
        found = json.loads(text)
        count += len(found)
        if len(found) < 100:
            return count
        page += 1


def wait_for(headers: dict, refusals: int) -> float:
    after = header(headers, "retry-after")
    if after is not None:
        return float(after)
    if header(headers, "x-ratelimit-remaining") == "0":
        return max(float(header(headers, "x-ratelimit-reset") or 0) - time.time(), 0) + 1
    return 60.0 * 2 ** (refusals - 1)


def report(record: dict) -> None:
    print("RESULT " + json.dumps(record), flush=True)


def main() -> None:
    plan = json.loads(Path("measure/plan.json").read_text(encoding="utf-8"))
    pool = json.loads(Path("measure/comments.json").read_text(encoding="utf-8"))[plan["set"]]
    offset = plan["offset"]
    waited = 0.0
    for batch, size in enumerate(plan["batches"], start=1):
        chosen = pool[offset:offset + size]
        payload = {"commit_id": HEAD, "event": "COMMENT", "comments": chosen}
        refusals = 0
        while True:
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            status, headers, text, seconds = call("POST", f"/pulls/{NUMBER}/reviews", payload)
            record = {"step": plan["step"], "batch": batch, "size": size, "offset": offset,
                      "attempt": refusals + 1, "at": at, "status": status,
                      "seconds": round(seconds, 2), "bytes": len(json.dumps(payload)),
                      "headers": {name: header(headers, name) for name in HEADERS}}
            if status == 200:
                review = json.loads(text)["id"]
                record.update(review=review, created=comments_of(review))
                report(record)
                break
            try:
                answer = json.loads(text)
                record.update(message=answer.get("message"),
                              documentation=answer.get("documentation_url"),
                              errors=answer.get("errors"))
            except ValueError:
                record.update(message=text[:500])
            refusals += 1
            limited = status in (403, 429) and "rate limit" in str(record.get("message", "")).lower()
            delay = wait_for(headers, refusals) if limited else None
            record.update(limited=limited, wait=delay)
            report(record)
            if (not plan.get("retry") or not limited or refusals > plan.get("max_retries", 3)
                    or waited + delay > plan.get("max_wait", 900)):
                print(f"stopped at batch {batch} after {refusals} refusal(s)", flush=True)
                return
            time.sleep(delay)
            waited += delay
        offset += size
        if batch < len(plan["batches"]):
            time.sleep(plan.get("pause", 1.0))
    print(f"posted {len(plan['batches'])} review(s), waited {waited:.0f} s on refusals", flush=True)


if __name__ == "__main__":
    main()
