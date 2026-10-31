"""Deletes the only comment of one thread `github-actions` opened, with the workflow's own token.

The thread is the one on `measure/ü.cs`, which has no reply, so that deleting its comment takes the
thread with it, as the review would take a thread whose suppression is gone if it may not resolve
one. GitHub's answer is printed as one `RESULT` line of JSON, and whether the comment and its
thread are still there as one `AFTER` line.
"""
import json
import os
import urllib.error
import urllib.request

API = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}"
TOKEN = os.environ["GITHUB_TOKEN"]
NUMBER = int(os.environ["PR"])


def call(method: str, path: str) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(
        API + path, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read().decode(errors="replace"), dict(refused.headers)


def comments() -> list[dict]:
    status, text, _ = call("GET", f"/pulls/{NUMBER}/comments?per_page=100")
    return json.loads(text) if status == 200 else []


def main() -> None:
    every = comments()
    target = next((c for c in every if c.get("path") == "measure/ü.cs"
                   and c.get("in_reply_to_id") is None
                   and c.get("user", {}).get("login") == "github-actions[bot]"
                   and not any(r.get("in_reply_to_id") == c["id"] for r in every)), None)
    if target is None:
        print("RESULT " + json.dumps({"label": "delete", "found": False}), flush=True)
        return
    status, text, headers = call("DELETE", f"/pulls/comments/{target['id']}")
    answer = json.loads(text) if text.startswith("{") else {"text": text[:300]}
    print("RESULT " + json.dumps({
        "label": "delete", "status": status, "comment": target["id"], "path": target["path"],
        "message": answer.get("message"), "request": headers.get("X-GitHub-Request-Id")}),
        flush=True)
    still = [c["id"] for c in comments() if c["id"] == target["id"]]
    print("AFTER " + json.dumps({"label": "delete", "still there": bool(still)}), flush=True)


if __name__ == "__main__":
    main()
