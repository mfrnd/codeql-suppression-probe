"""Posts each comment `measure/file.json` lists on this pull request, about a whole file, one by one.

Each entry is one `POST /pulls/{number}/comments` with `subject_type: file`, which the endpoint for
reviews does not take, so that what GitHub answers about a comment on a file belongs to that file
alone. Every answer is printed as one `RESULT` line of JSON: the label, the status, GitHub's message
and its errors, and for a comment that was made, where it was put.
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
    for entry in json.loads(Path("measure/file.json").read_text(encoding="utf-8")):
        status, text = call("POST", f"/pulls/{NUMBER}/comments",
                            {"commit_id": HEAD, "subject_type": "file", "path": entry["path"],
                             "body": f"Files probe: {entry['label']}.\n\n"
                                     f"<!-- files-probe: {entry['label']} -->"})
        answer = json.loads(text) if text.startswith("{") else {"message": text[:300]}
        record: dict[str, object] = {"label": entry["label"], "status": status}
        if status == 201:
            record.update(made={key: answer.get(key) for key in (
                "id", "path", "subject_type", "line", "side", "pull_request_review_id")})
        else:
            record.update(message=answer.get("message"), errors=answer.get("errors"))
        print("RESULT " + json.dumps(record), flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
