# Lincoln Hall Sports Calendar

This folder contains everything needed to host a Lincoln Hall athletics
calendar that **updates itself automatically**, including picking up brand
new teams/sports the moment QuickScores creates a schedule for them (ex. a
Boys Volleyball team after tryouts) with no manual editing required.

## How it works

- `scrape_schedule.py` checks QuickScores' full list of current leagues (not
  a hardcoded set of 4), visits each one, and pulls out every game involving
  Lincoln Hall into `events.json`.
- A GitHub Action (`.github/workflows/update-schedule.yml`) runs that script
  automatically a few times a day and saves the result.
- `index.html` (the calendar page) loads `events.json` from the same site
  it's hosted on. **The visitor's browser never contacts quickscores.com at
  all** — this is the part that gets around a school network filter, since
  the browser only ever talks to your GitHub Pages site, not the sports
  site.

## Design

The page uses a dedicated design system rather than default styling:
condensed "Anton" display type for the wordmark, month labels, and headings,
paired with "Inter" for everything functional; a varsity black-and-red
identity with a distinct accent color per sport (green for soccer, gold for
volleyball, blue for basketball) so color carries information, not just
decoration; a "Next up" banner that always shows the next Lincoln Hall game
regardless of which month you're viewing; and two interchangeable views —
**Month** (a classic grid) and **Agenda** (a scrollable list that auto-jumps
to today and fades out past games) — so it's equally usable on a phone or a
widescreen monitor.

## Embedding as a full page in Google Sites

1. In your Google Site, add an **Embed** element → **By URL** → paste your
   GitHub Pages URL.
2. Drag the embed box to be tall (roughly 800–1000px) and set its section to
   **Full width**. The page fills whatever box you give it and scrolls
   internally, so it won't look cramped or double-scroll inside the iframe.
3. On phones, Google Sites stacks the embed at the device's width
   automatically; the calendar itself switches to a mobile-friendly layout
   (bigger tap targets, Agenda view by default) below 641px wide.

## One-time setup (about 20–30 minutes)

1. **Create a free GitHub account** at github.com if you don't have one.
2. **Create a new repository**: click the "+" in the top right → "New
   repository" → name it something like `lincoln-hall-calendar` → make sure
   it's set to **Public** → Create repository.
3. **Upload these files**, keeping the folder structure:
   - `index.html`
   - `scrape_schedule.py`
   - `.github/workflows/update-schedule.yml`
   - `README.md` (optional, just for your own reference)

   Easiest way: on the repo page, click "Add file" → "Upload files", drag
   everything in, and commit. (For the `.github/workflows` folder, you may
   need to create that path when uploading, or use "Add file" → "Create new
   file" and type `.github/workflows/update-schedule.yml` as the filename,
   then paste the contents.)
4. **Turn on GitHub Pages**: repo → Settings → Pages (left sidebar) → under
   "Build and deployment", set Source to "Deploy from a branch", Branch to
   `main` and folder to `/ (root)` → Save. GitHub will give you a URL like
   `https://yourusername.github.io/lincoln-hall-calendar/`.
5. **Run the scraper once manually** so there's real data right away: repo →
   Actions tab → click "Update Lincoln Hall schedule" on the left → "Run
   workflow" button → Run workflow. Wait about a minute, then refresh — you
   should see a new `events.json` file appear in the repo with real games in
   it.
6. **Visit your GitHub Pages URL** — you should now see the live,
   auto-updating calendar.
7. **Embed it in Google Sites**: in your Google Site, Insert → Embed → "By
   URL" → paste your GitHub Pages URL.

After that, it just runs itself — the Action re-checks QuickScores three
times a day (edit the `cron` line in the workflow file if you want it more
or less often) and commits any changes, and the calendar page always shows
whatever's in the latest `events.json`.

## If something looks wrong

Go to the Actions tab and open the latest run — the script prints exactly
which leagues it found and how many Lincoln Hall events it pulled from each
one, so it's usually obvious where to look. QuickScores occasionally
tweaking their page layout is the most likely thing to eventually break the
parsing; if that happens, paste the Action's log output back to Claude and
it can patch `scrape_schedule.py`.
