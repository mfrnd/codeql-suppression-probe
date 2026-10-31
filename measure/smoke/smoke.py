"""Runs KeePassGate's own review against this pull request, twice, with the workflow's own token.

`suppression_review.py`, `code_scanning.py` and `suppressions.py` beside this are copies of
KeePassGate's from the commit that builds a push's threads into one pending review. Before the
first run, a pending review is left behind as a run that failed would leave it. Each run posts 30
notes over seven lines of `measure/lines-1000.txt`, one on a file no line takes and one on a file
outside the diff; the second, with the same notes, should post nothing. What became of them is
printed as one `RESULT` line of JSON a run.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import suppression_review as review  # noqa: E402

NUMBER = int(os.environ["PR"])
HEAD = os.environ["HEAD"]
GITHUB = review.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])


def notes() -> list[review.Note]:
    made = [review.Note("added", "measure/lines-1000.txt", 7 * i + 4, "RIGHT",
                        f"Smoke note {i}, over seven lines.", f"smoke-{i}", (7 * i + 1, 7 * i + 7))
            for i in range(30)]
    made.append(review.Note("added", "README-renamed.md", 1, "RIGHT",
                            "Smoke note on a file no line takes.", "smoke-file"))
    made.append(review.Note("added", "src/Probe/Cases.cs", 5, "RIGHT",
                            "Smoke note on a file outside the diff.", "smoke-outside"))
    return made


def main() -> None:
    left = review.Note("added", "measure/lines-1000.txt", 900, "RIGHT",
                       "Left behind by a run that failed.", "smoke-left")
    opened = GITHUB.open_review(NUMBER, HEAD, review._comment(left, False))
    print("RESULT " + json.dumps({"step": "left behind", "review": opened[0],
                                  "pending": GITHUB.pending_reviews(NUMBER)}), flush=True)
    for run in ("first", "second"):
        seen: list[review.Posted] = []

        def compose(posted: review.Posted) -> str:
            seen.append(posted)
            return review.summary(review.Review(), [], True, posted)

        count = review.post(GITHUB, NUMBER, HEAD, notes(), compose)
        posted = seen[-1]
        print("RESULT " + json.dumps({
            "step": run, "threads": count, "on lines": posted.lines,
            "on files": [n.key for n in posted.files],
            "nowhere": [[n.key, why] for n, why in posted.nowhere],
            "pending afterwards": GITHUB.pending_reviews(NUMBER)}), flush=True)


if __name__ == "__main__":
    main()
