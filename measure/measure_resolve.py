"""Resolves three of this pull request's review threads with the workflow's own token.

Each is a thread `github-actions` opened, with no reply: the one on `measure/noeol.cs`, whose line
the push that runs this changed, so that GitHub has marked it outdated; one on a line nothing
changed; and one on a whole file. Each is resolved with GraphQL's `resolveReviewThread`, as the
review job would resolve a thread whose suppression is gone, and every answer is printed as one
`RESULT` line of JSON: the label, the status, the thread as GitHub had it before, GitHub's answer
and errors and the request's identifier. The threads are read again afterwards, one `AFTER` line
each.
"""
import json
import os
import time
import urllib.error
import urllib.request

GRAPHQL = "https://api.github.com/graphql"
TOKEN = os.environ["GITHUB_TOKEN"]
OWNER, NAME = os.environ["GITHUB_REPOSITORY"].split("/")
NUMBER = int(os.environ["PR"])

THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved isOutdated path line subjectType
          comments(first: 2) { totalCount nodes { databaseId author { login } } }
        }
      }
    }
  }
}"""

RESOLVE = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}"""


def call(query: str, variables: dict[str, object]) -> tuple[int, dict[str, object], dict[str, str]]:
    request = urllib.request.Request(
        GRAPHQL, data=json.dumps({"query": query, "variables": variables}).encode(), method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode()), dict(response.headers)
    except urllib.error.HTTPError as refused:
        text = refused.read().decode(errors="replace")
        answer = json.loads(text) if text.startswith("{") else {"message": text[:300]}
        return refused.code, answer, dict(refused.headers)


def threads() -> list[dict]:
    status, answer, _ = call(THREADS, {"owner": OWNER, "name": NAME, "number": NUMBER})
    print("THREADS " + json.dumps({"status": status, "errors": answer.get("errors")}), flush=True)
    return answer["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]


def own(thread: dict) -> bool:
    comments = thread["comments"]
    return (not thread["isResolved"] and comments["totalCount"] == 1
            and comments["nodes"][0]["author"]["login"] == "github-actions")


def main() -> None:
    every = threads()
    chosen = {
        "outdated by this push": next(
            (t for t in every if own(t) and t["path"] == "measure/noeol.cs"), None),
        "on a line nothing changed": next(
            (t for t in every if own(t) and t["path"] == "src/Probe/Copy.cs"), None),
        "on a whole file": next(
            (t for t in every if own(t) and t["subjectType"] == "FILE"), None),
    }
    for label, thread in chosen.items():
        if thread is None:
            print("RESULT " + json.dumps({"label": label, "found": False}), flush=True)
            continue
        status, answer, headers = call(RESOLVE, {"thread": thread["id"]})
        before = {key: thread[key] for key in
                  ("id", "path", "line", "subjectType", "isOutdated", "isResolved")}
        print("RESULT " + json.dumps({
            "label": label, "status": status, "before": before, "answer": answer.get("data"),
            "errors": answer.get("errors"), "message": answer.get("message"),
            "request": headers.get("X-GitHub-Request-Id")}), flush=True)
        time.sleep(3)
    after = {t["id"]: t for t in threads()}
    for label, thread in chosen.items():
        if thread is not None:
            again = after[thread["id"]]
            print("AFTER " + json.dumps({"label": label, "isResolved": again["isResolved"],
                                         "isOutdated": again["isOutdated"]}), flush=True)


if __name__ == "__main__":
    main()
