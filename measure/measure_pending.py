"""Measures one review built up while pending and submitted once, with the workflow's own token.

Three steps, four minutes apart, which lifted every block #9 met. Every answer is printed as one
`RESULT` line of JSON: the step, what was asked, the status, the seconds it took, GitHub's message
and errors, and the request's identifier.

- A: one pending review created with all 112 comments in one request, then submitted.
- B: one pending review created with the first 20, the other 92 added one by one with GraphQL's
  `addPullRequestReviewThread` a second apart, then one on a whole file and one on a line outside
  the diff, then the review submitted.
- C: a pending review left behind: one created, a second asked for beside it, the first looked for
  among the pull request's reviews and deleted.

The comments have the lengths of the 112 notes KeePassGate's review makes on its pull request #51,
`pending/lengths.json`, and each covers seven lines of `measure/lines-1000.txt`, as most of its
threads do.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = os.environ["GITHUB_REPOSITORY"]
API = f"https://api.github.com/repos/{REPOSITORY}"
GRAPHQL = "https://api.github.com/graphql"
TOKEN = os.environ["GITHUB_TOKEN"]
NUMBER = int(os.environ["PR"])
HEAD = os.environ["HEAD"]
LENGTHS = json.loads(Path("measure/pending/lengths.json").read_text(encoding="utf-8"))
PAUSE = 240


def request(method: str, url: str, body: object = None) -> tuple[int, object, dict, float]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
    started = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method),
                                    timeout=120) as response:
            text, status, answered = response.read().decode(), response.status, response.headers
    except urllib.error.HTTPError as refused:
        text, status, answered = refused.read().decode(errors="replace"), refused.code, refused.headers
    elapsed = round(time.monotonic() - started, 2)
    try:
        answer = json.loads(text) if text else None
    except ValueError:
        answer = {"message": text[:300]}
    return status, answer, dict(answered), elapsed


def record(step: str, asked: str, status: int, answer: object, headers: dict, elapsed: float,
           **more: object) -> None:
    shaped = answer if isinstance(answer, dict) else {}
    print("RESULT " + json.dumps({
        "step": step, "asked": asked, "status": status, "seconds": elapsed,
        "message": shaped.get("message"), "errors": shaped.get("errors"),
        "request": headers.get("X-GitHub-Request-Id"),
        "remaining": headers.get("X-RateLimit-Remaining"), **more}), flush=True)


def graphql(query: str, variables: dict) -> tuple[int, object, dict, float]:
    return request("POST", GRAPHQL, {"query": query, "variables": variables})


def body(step: str, index: int) -> str:
    mark = f"\n\n<!-- pending-probe: {step}{index} -->"
    words = f"Pending probe {step}, note {index}. " + "a line of a note, " * 80
    return words[: max(LENGTHS[index] - len(mark), 40)] + mark


def comment(step: str, index: int) -> dict:
    first = 7 * index + 1
    return {"path": "measure/lines-1000.txt", "start_line": first, "line": first + 6,
            "start_side": "RIGHT", "side": "RIGHT", "body": body(step, index)}


def review_comments(review: int) -> int:
    count, page = 0, 1
    while True:
        status, answer, _, _ = request(
            "GET", f"{API}/pulls/{NUMBER}/reviews/{review}/comments?per_page=100&page={page}")
        if status != 200 or not answer:
            return count
        count += len(answer)
        page += 1


def pending_reviews() -> list[dict]:
    status, answer, _, _ = request("GET", f"{API}/pulls/{NUMBER}/reviews?per_page=100")
    return [r for r in answer if r.get("state") == "PENDING"] if status == 200 else []


def submit_rest(step: str, review: int) -> None:
    status, answer, headers, elapsed = request(
        "POST", f"{API}/pulls/{NUMBER}/reviews/{review}/events", {"event": "COMMENT"})
    record(step, "submit, REST", status, answer, headers, elapsed,
           state=answer.get("state") if isinstance(answer, dict) else None)
    time.sleep(5)
    print("RESULT " + json.dumps({"step": step, "asked": "comments in the submitted review",
                                  "count": review_comments(review)}), flush=True)


def step_a() -> None:
    comments = [comment("A", index) for index in range(len(LENGTHS))]
    status, answer, headers, elapsed = request(
        "POST", f"{API}/pulls/{NUMBER}/reviews", {"commit_id": HEAD, "comments": comments})
    shaped = answer if isinstance(answer, dict) else {}
    record("A", f"create pending with {len(comments)}", status, answer, headers, elapsed,
           state=shaped.get("state"), review=shaped.get("id"))
    if status != 200:
        time.sleep(15)
        left = pending_reviews()
        print("RESULT " + json.dumps({"step": "A", "asked": "pending after the refusal",
                                      "found": [r["id"] for r in left]}), flush=True)
        if not left:
            return
        shaped = left[0]
    time.sleep(3)
    submit_rest("A", int(shaped["id"]))


ADD = """
mutation($review: ID!, $path: String!, $line: Int, $side: DiffSide, $start: Int,
         $startSide: DiffSide, $subject: PullRequestReviewThreadSubjectType, $body: String!) {
  addPullRequestReviewThread(input: {pullRequestReviewId: $review, path: $path, line: $line,
      side: $side, startLine: $start, startSide: $startSide, subjectType: $subject,
      body: $body}) {
    thread { id isOutdated subjectType }
  }
}"""

SUBMIT = """
mutation($review: ID!) {
  submitPullRequestReview(input: {pullRequestReviewId: $review, event: COMMENT}) {
    pullRequestReview { id state }
  }
}"""


def add(review: str, label: str, path: str, line: int | None, start: int | None,
        subject: str | None, text: str) -> None:
    status, answer, headers, elapsed = graphql(
        ADD, {"review": review, "path": path, "line": line,
              "side": None if line is None else "RIGHT", "start": start,
              "startSide": None if start is None else "RIGHT", "subject": subject,
              "body": text})
    shaped = answer if isinstance(answer, dict) else {}
    data = shaped.get("data") or {}
    added = (data.get("addPullRequestReviewThread") or {}).get("thread")
    record("B", label, status, answer, headers, elapsed, thread=added)


def step_b() -> None:
    first = [comment("B", index) for index in range(20)]
    status, answer, headers, elapsed = request(
        "POST", f"{API}/pulls/{NUMBER}/reviews", {"commit_id": HEAD, "comments": first})
    shaped = answer if isinstance(answer, dict) else {}
    record("B", "create pending with 20", status, answer, headers, elapsed,
           state=shaped.get("state"), review=shaped.get("id"))
    if status != 200:
        return
    node, review = shaped["node_id"], int(shaped["id"])
    for index in range(20, len(LENGTHS)):
        time.sleep(1)
        start = 7 * index + 1
        add(node, f"add thread {index}", "measure/lines-1000.txt", start + 6, start, None,
            body("B", index))
    time.sleep(1)
    add(node, "add a thread on a whole file", "README-renamed.md", None, None, "FILE",
        body("B", 0))
    time.sleep(1)
    add(node, "add a thread outside the diff", "src/Probe/Cases.cs", 5, None, None, body("B", 1))
    time.sleep(3)
    status, answer, headers, elapsed = graphql(SUBMIT, {"review": node})
    shaped = answer if isinstance(answer, dict) else {}
    submitted = ((shaped.get("data") or {}).get("submitPullRequestReview") or {}).get(
        "pullRequestReview")
    record("B", "submit, GraphQL", status, answer, headers, elapsed, review=submitted)
    time.sleep(5)
    print("RESULT " + json.dumps({"step": "B", "asked": "comments in the submitted review",
                                  "count": review_comments(review)}), flush=True)


def step_c() -> None:
    status, answer, headers, elapsed = request(
        "POST", f"{API}/pulls/{NUMBER}/reviews", {"commit_id": HEAD, "comments": [comment("C", 0)]})
    shaped = answer if isinstance(answer, dict) else {}
    record("C", "create pending with 1", status, answer, headers, elapsed,
           state=shaped.get("state"), review=shaped.get("id"))
    time.sleep(2)
    status, answer, headers, elapsed = request(
        "POST", f"{API}/pulls/{NUMBER}/reviews", {"commit_id": HEAD, "comments": [comment("C", 1)]})
    second = answer if isinstance(answer, dict) else {}
    record("C", "create a second pending beside it", status, answer, headers, elapsed,
           state=second.get("state"), review=second.get("id"))
    time.sleep(2)
    found = pending_reviews()
    print("RESULT " + json.dumps({"step": "C", "asked": "pending among the reviews, REST",
                                  "found": [r["id"] for r in found]}), flush=True)
    for left in found:
        time.sleep(2)
        status, answer, headers, elapsed = request(
            "DELETE", f"{API}/pulls/{NUMBER}/reviews/{left['id']}")
        record("C", f"delete pending {left['id']}", status, answer, headers, elapsed)
    time.sleep(2)
    print("RESULT " + json.dumps({"step": "C", "asked": "pending afterwards",
                                  "found": [r["id"] for r in pending_reviews()]}), flush=True)


def main() -> None:
    step_a()
    time.sleep(PAUSE)
    step_b()
    time.sleep(PAUSE)
    step_c()


if __name__ == "__main__":
    main()
