# Historical review release

Run the commands in [the review guide](research/strong_choch/review/README.md)
from this directory. On the server the directory is
`/opt/signals/review_toolkit`. Use the Python environment documented in the
guide, or a dedicated review environment. Review charts are already prepared.

Set a dedicated review bot token and reviewer allowlist in the review
process environment. Keep the SQLite file and exports private. Configure its
database path outside the checkout for long-term server use. The existing
deployment workflow copies the toolkit and restarts the live bot and dashboard.
The review bot runs only when explicitly started with its own credentials.
