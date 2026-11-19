# Quiet Wire

A news-only reader. It pulls headlines from about 20 newsrooms (BBC, Al Jazeera, The Guardian, DW, France 24, NPR, CBC, The Hindu, Indian Express, NDTV, Hindustan Times, Scroll, CNBC, Economic Times, Mint, Business Standard, Ars Technica, The Verge, TechCrunch, ScienceDaily), groups the same story across outlets, and ranks stories by how many outlets carry them, not by engagement.

No images, no comments, no autoplay, no endless scroll. Each tab ends with "You're caught up."

## Run it

Double-click `start.command` in Finder, or:

```bash
python3 server.py
```

Then open http://localhost:8791. It uses only the Python standard library, so there's nothing to install.

**On your phone, anywhere:** open the published site (see below) and add it to your home screen. On iPhone, tap Share, then Add to Home Screen. On Android, open Chrome's menu and tap Add to Home screen. It then opens full-screen like an app.

**On your phone, home Wi-Fi only:** run `python3 server.py --lan` and open the address it prints.

## The published site

`.github/workflows/publish.yml` runs on GitHub every 30 minutes. It fetches the feeds with `python3 server.py --build site` and publishes the result to GitHub Pages, so the site stays fresh without your Mac. To publish right away, open the repo's **Actions** tab and choose **Publish news → Run workflow**. Pushing to `main` also publishes.

If a run fails because no feed answered, the previous version stays up. GitHub can delay scheduled runs by a few minutes when it's busy.

**Check the feeds without opening the page:** `python3 server.py --check`

## Tabs

- **Top stories**: stories carried by 3 or more outlets, ranked by coverage and freshness.
- **World, India, Business, Tech, Science & Health**: each tab shows stories its own outlets carry. A big story also appears in a second tab when at least two of that tab's outlets cover it.

Tap **Also BBC, CNBC +3** under a story to compare how other outlets framed it.

## Filters

- **Muted words** hide topics you don't want, such as `cricket, celebrity`.
- **Hide stories I've already opened** clears what you've read.

Both are saved in your browser.

## Change the sources

Edit `feeds.json`. Each section has a list of `{ "name", "url" }` RSS or Atom feeds; feeds listed first are preferred as the headline. `window_hours` sets how far back a section looks (default 36). `skip_title_patterns` drops headlines such as gift guides and quizzes. Restart the server after editing.

News is fetched when you open the page and cached for 10 minutes. The Refresh button refetches, at most once a minute.
