# Movie Awards Tracker

A self-hosted app that tracks nominated (and winning) films across major
awards, sourced from Wikipedia. Runs entirely in Docker.

**Awards covered:** Oscars, Golden Globes, BAFTA, Cannes, Berlinale, Venice,
Sundance, Independent Spirit Awards, César Awards, Critics' Choice Awards.

**Years:** last 5 years plus upcoming ceremonies (auto-computed from today's
date), and you can add any other year manually per award.

## Putting this on GitHub

```bash
cd movieawards
git init
git add .
git commit -m "Initial commit"
```

Create an empty repo on GitHub (github.com/new — don't initialize it with a
README, since you already have one), then:

```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

`.gitignore` already excludes your database, IMDb import files, and Python
caches, so none of your personal data goes up.

## Building a container image automatically (GHCR)

A GitHub Actions workflow is included at
`.github/workflows/docker-publish.yml`. Once the repo is on GitHub, it runs
automatically on every push to `main` and builds + publishes a Docker image
to **GitHub Container Registry** — no extra setup, no secrets to add (it
uses GitHub's built-in token).

After your first push, check the **Actions** tab on GitHub to watch it
build. Once it finishes, the image is available at:

```
ghcr.io/<your-username>/<repo-name>:latest
```

**One manual step:** GHCR packages are private by default. To pull the
image without authenticating, go to your GitHub profile → **Packages** →
select this image → **Package settings** → change visibility to public.
(Or keep it private and `docker login ghcr.io` on whatever machine pulls
it — either works.)

From then on, anywhere with Docker, you can run the published image
directly instead of rebuilding from source:

```bash
docker run -d -p 5000:5000 \
  -v awards_data:/data \
  -v $(pwd)/imdb-import:/import \
  ghcr.io/<your-username>/<repo-name>:latest
```

Or point `docker-compose.yml`'s `build: .` at `image:
ghcr.io/<your-username>/<repo-name>:latest` instead, and `docker compose
pull && docker compose up -d` picks up new versions without a local build.

## Running it

```bash
docker compose up -d --build
```

Then open **http://localhost:5000**.

Data persists in a Docker volume (`awards_data`), so it survives restarts
and rebuilds.

## How it works — fully automatic

A background job runs inside the container itself, once at startup and then
every 7 days. No clicking required. Weekly is plenty — these awards only
announce nominees/winners a handful of times a year, so daily checks would
mostly just hit unchanged Wikipedia pages. Each cycle:

1. **Seeds** ceremony rows for every award across the tracked year range
   (adds next year automatically as time passes).
2. **Scrapes** any ceremony that hasn't been successfully pulled yet — it
   guesses the Wikipedia page title (e.g. "98th Academy Awards"), falls
   back to Wikipedia's search API if the guess is off, and just retries on
   the next weekly cycle if the page doesn't exist yet (nominations
   announced, page not created yet, etc.). Requests are spaced out (a few
   seconds apart, with automatic backoff on a 429) and only one cycle can
   run at a time, so the first big run — which seeds ~80 ceremonies across
   all 10 awards — doesn't hammer Wikipedia all at once.
3. **Re-checks** any ceremony within a year of today even if it was already
   scraped, so nominee lists that get filled in and winners that get added
   the night of the ceremony are picked up automatically.
4. Leaves anything you've entered manually alone (marked `manual`).

You never have to tell it a new year has started or that nominations were
announced — it just keeps checking. If you don't want to wait for the next
cycle, "Check for updates now" on the home page (or "Check this ceremony
now" on a ceremony page) triggers an immediate pass.

### Ongoing: automatic sync from Plex

IMDb can't be pulled automatically (see above), but **Plex has a real,
documented API** for watch history — so use it as the ongoing source of
truth instead: go to **Settings** in the app, enter your Plex server URL
and token, and save. From then on, anything marked watched in Plex gets
added to your watched list automatically, checked every 15 minutes. No
CSV, no drop folder, no manual step.

Finding your Plex token: open any item in the Plex web app → ⋯ → *Get
Info* → *View XML* → copy the `X-Plex-Token` value from the URL bar.

A practical workflow: do a **one-time IMDb import** (drop-folder or
upload) to backfill everything you've already watched, then let **Plex
sync** take over for everything from today onward.

## Watched list

Three related pages, linked in the top nav:

- **Watched list** — see everything recorded as watched, whichever source
  it came from (IMDb import or Plex sync), and remove individual entries
  or clear the list.
- **Not watched** — every nominated film across all tracked awards, minus
  whatever's on your watched list. Ceremony pages also show a ✓ badge next
  to nominees you've already watched.
- **Settings** — connect Plex for automatic ongoing sync.

### One-time import from IMDb

IMDb blocks automated scraping of its pages (robots.txt disallows it), and
the CSV export button only works in a logged-in browser — there's no way
for a background job to pull your list directly from imdb.com without
either violating IMDb's terms or handling your login session inside the
container, neither of which this app does.

The closest practical equivalent: an `imdb-import/` folder next to
`docker-compose.yml`, mounted into the container. Export your CSV from
IMDb (still a manual click, that part can't be avoided) and drop the file
in that folder — the app checks it every 5 minutes and imports
automatically, filing the file into `imdb-import/processed/` afterward so
it's never re-imported. No need to open the app at all for that step.

Uploading/pasting directly on the Watched list page still works too, for
one-off imports.

Matching is by normalized title (case/punctuation/leading article
insensitive), not IMDb ID, since Wikipedia doesn't give us one — so two
different films that happen to share a bare title would collide. Fine for
personal use, just worth knowing.

## Interface

- **Left sidebar**: Awards, Watched list, To watch list.
- **Top-right ⚙ Settings dropdown**: jumps to the IMDb import, Plex, and
  TMDb/Radarr sections — all consolidated on one Settings page.
- **Posters**: once TMDb is connected (see below), posters show on the
  Awards ceremony pages, Watched list, and To-watch list.
- **Watched ticks**: on ceremony pages, any nominee already on your
  watched list shows a ✓.

## Radarr integration

The "Not watched" page exposes a live JSON feed at `/radarr.json` — the
format Radarr's **Custom List** import type expects.

In Radarr: **Settings → Lists → + → Custom List** (under "Advanced" if
it's hidden) → paste the URL shown on the Not-watched page → save. Radarr
polls it on whatever interval you configure and adds anything new.

**For exact matches, connect TMDb** (Settings page in this app — a free
API key from themoviedb.org). Without it, the feed sends bare titles and
Radarr guesses the match via its own search, which is usually right but
can occasionally pick the wrong film for an ambiguous or very common
title. With a TMDb key, a background job resolves every unwatched film to
an exact TMDb ID (cross-checking release year inferred from which
ceremony it appeared in — award shows typically honor the *previous*
year's films, festivals the same year), and the feed sends that ID
directly. Radarr's match is then exact, not a guess. The Not-watched page
shows which films are ID-matched vs. still title-only.

## Known limitations

Wikipedia's award pages aren't perfectly standardized — table structure
varies a bit between awards and even between years of the same award. The
scraper only pulls from sections that are plausibly about nominations
(skipping cast lists, In Memoriam tributes, box office tables, and
"films with multiple nominations" summary tables, which otherwise produce
garbage), and identifies film titles by their italics — Wikipedia's own
convention for titles — rather than guessing from word order, since that
position flips between categories (e.g. "Film – Person" for Best Picture
vs. "Person – Film" for Best Actor). Still:

- Some categories may be skipped if a page uses a nonstandard layout.
- The nominee/person name isn't captured anymore — only the film. This
  was a deliberate tradeoff: reliably identifying *who* was nominated
  requires knowing each category's word order, which varies too much to
  get right consistently, whereas the film is almost always italicized
  and unambiguous. You can still add a nominee name by hand on the
  manual-add form if you want it for a specific entry.
- Anything the scraper misses can be added by hand from the ceremony page.
- If a scrape looks off, just re-scrape — it's idempotent, it always
  replaces (never duplicates) that ceremony's data.

## Project structure

```
app.py          Flask routes
db.py           SQLite schema + queries
scraper.py      Wikipedia fetch + HTML parsing
scheduler.py    Background jobs (Wikipedia scrape, drop-folder scan, Plex sync, TMDb resolution)
plex_client.py  Plex watch-history API client
tmdb_client.py  TMDb search/resolution client
films.py        Shared film-grouping logic (app.py + scheduler.py)
imdb_import.py  IMDb CSV/text parsing
watch_import.py Drop-folder watcher
config.py       Award list and Wikipedia title patterns
templates/      HTML views
static/         CSS
```

## Adding another award

Add an entry to `AWARDS` in `config.py` with a `title_template` matching
its Wikipedia page naming convention — see the existing entries for the
two supported patterns ("ordinal" vs "year"). No other code changes
needed.
