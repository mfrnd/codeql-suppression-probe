"""Posts each review `measure/each.json` lists on this pull request, and prints GitHub's every answer.

Each entry is one `POST /pulls/{number}/reviews` with `event: COMMENT` and the comments it names, so
that a refusal belongs to that entry alone, and the run goes on past it. Every answer is printed as
one `RESULT` line of JSON: the entry's label, the status, GitHub's message and its errors, and for a
review that was made, how many comments it holds. A refusal for rate stops the run, since going on
past one is what GitHub's documentation warns against.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}"
TOKEN = os.environ["GITHUB_TOKEN"]
NUMBER = int(os.environ["PR"])
HEAD = os.environ["HEAD"]


def call(method: str, path: str, body: object = None) -> tuple[int, str]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read().decode(errors="replace")


def main() -> None:
    for entry in json.loads(Path("measure/each.json").read_text(encoding="utf-8")):
        status, text = call("POST", f"/pulls/{NUMBER}/reviews",
                            {"commit_id": HEAD, "event": "COMMENT", "comments": entry["comments"]})
        record: dict[str, object] = {"label": entry["label"], "status": status}
        answer = json.loads(text) if text.startswith("{") else {"message": text[:300]}
        if status == 200:
            listed = call("GET", f"/pulls/{NUMBER}/reviews/{answer['id']}/comments")[1]
            record.update(review=answer["id"], made=[
                {key: c.get(key) for key in ("path", "start_line", "line", "side", "start_side",
                                             "original_line", "subject_type")}
                for c in json.loads(listed)])
        else:
            record.update(message=answer.get("message"), errors=answer.get("errors"))
        print("RESULT " + json.dumps(record), flush=True)
        if status in (403, 429) and "rate limit" in str(answer.get("message", "")).lower():
            print("stopped at a refusal for rate", flush=True)
            return
        time.sleep(3)


if __name__ == "__main__":
    main()
