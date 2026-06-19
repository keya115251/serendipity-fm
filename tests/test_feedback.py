"""
CLI utility to record like/dislike feedback, for testing the feedback
loop end to end (the discovery walk's exclusion of disliked artists is
otherwise unverifiable without a way to actually write feedback first).

Run with:
  python -m tests.test_feedback artist <username> <artist_name> <liked|disliked>
  python -m tests.test_feedback album <username> <artist_name> <album_name> <liked|disliked>
  python -m tests.test_feedback show <username>
"""

import sys

from app.core.config import settings
from app.db.feedback_store import FeedbackStore


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    store = FeedbackStore(settings.database_path)
    mode = sys.argv[1]

    if mode == "artist":
        _, _, username, artist_name, sentiment = sys.argv
        store.record_artist_feedback(username, artist_name, sentiment)
        print(f"Recorded: {username} {sentiment} artist '{artist_name}'")

    elif mode == "album":
        _, _, username, artist_name, album_name, sentiment = sys.argv
        store.record_album_feedback(username, artist_name, album_name, sentiment)
        print(f"Recorded: {username} {sentiment} album '{album_name}' by '{artist_name}'")

    elif mode == "show":
        _, _, username = sys.argv
        print(f"--- Disliked artists for '{username}' ---")
        for a in sorted(store.get_disliked_artists(username)):
            print(f"  {a}")
        print(f"--- Liked artists for '{username}' ---")
        for a in sorted(store.get_liked_artists(username)):
            print(f"  {a}")
        print(f"--- Album feedback for '{username}' ---")
        for (artist, album), sentiment in store.get_album_feedback(username).items():
            print(f"  {sentiment}: '{album}' by {artist}")

    else:
        print(f"Unknown mode '{mode}'")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
