# Multnomah County Bookings — Recent & Searchable Archive

**Status: public website preview ready; live data collection NOT tested or enabled.**

This project provides a recent-first booking viewer and a searchable historical archive.
The archive begins **only when authorized, live collection succeeds**; it cannot retrieve
records that were missed in prior years without a separate permitted data source.

The intended upstream source is the official Multnomah County Sheriff's Office
Online Person in Custody Data (PAID): https://apps.mcso.us/PAID.
This project is independently maintained, not affiliated with MCSO, and does not display mugshots.

## Beginner setup: get the website preview online (free)

1. Create a free account: https://github.com/signup. Choose a public username you like.
2. Install GitHub Desktop: https://desktop.github.com and sign into your account.
3. Download and **extract** `multco-bookings-starter.zip`. Don't upload the ZIP as a single file.
4. In GitHub Desktop choose **File > Add Local Repository** and select the **inner**
   `multco-bookings-starter` folder containing `site`, `.github`, `collector.py`, and this README.
   If told that it isn't a Git repository, select **create a repository here**.
5. Enter a commit message like `Initial website`, then choose **Commit to main**.
   Choose **Publish repository**. Use the name `multco-bookings`; **uncheck**
   `Keep this code private` to create a public repo on the free plan.
6. On github.com, open the new repository, then **Settings > Pages > Build and deployment > Source > GitHub Actions**.
   Do **not** select Deploy from a branch; the entry file is in `site/` and the
   supplied workflow deploys that folder automatically.
7. Open **Actions > Publish Multnomah County Bookings**. Select **Run workflow**;
   keep `action: preview`. If GitHub shows an earlier failed deployment from before
   you enabled Pages, re-run it now. Wait for a green successful deployment.
8. Find the public URL in **Settings > Pages** (normally
   `https://YOUR-USERNAME.github.io/multco-bookings/`).

The preview intentionally says **no verified public booking feed**. That's correct.
**Don't enable daily collection yet.** You should still see the website layout,
Recent Bookings, Search Archive, search and date fields.

## Live archive architecture (later stage — not free hosting alone)

A searchable historical archive requires **persistent private object storage**:
GitHub Pages itself is static hosting and GitHub Actions runners are temporary.
This version supports an S3-compatible private bucket, such as Cloudflare R2.

The source code contains an `archive_store.py` module that:
- Retains verified records by booking number, newest first, with deduplication.
- Produces a recent seven-day view and annual JSON files for archive searches.
- Uses an explicit retention policy (default 730 days; adjust after legal review).
- Supports a permanent *suppression list* for known correction/deletion cases.
- Excludes dates of birth, addresses, photos, demographics, and juvenile records.
- Keeps the underlying archive OUT of Git history; only exported public fields are deployed.

Before live collection is enabled:
1. Contact MCSO for permission/terms of automated collection or request an authorized API/CSV feed.
   Public viewing does not automatically grant scraping or republication rights.
2. Test the actual booking search and detail pages. **The current Playwright selectors are
   speculative**, not verified against the live form; the collector fails closed if it can't parse results.
3. Review Oregon HB 3273/ORS 133.870 on booking photos, expungement/correction
   requirements, privacy, and public records reuse terms; establish a published correction
   and deletion policy. Never charge for removals.
4. Set up a private S3-compatible bucket. In **Settings > Secrets and variables > Actions**, create
   secrets `ARCHIVE_BUCKET`, `ARCHIVE_ENDPOINT_URL`, `ARCHIVE_ACCESS_KEY_ID`,
   `ARCHIVE_SECRET_ACCESS_KEY`, and `SCRAPER_CONTACT` (your real contact email).
   Give the storage key only the necessary read/write access to the private bucket.
5. Set the Actions **repository variable** `ENABLE_DAILY_COLLECTION` to `true` only after
   all previous steps are complete and the collector has been validated.
6. In **Actions > Publish Multnomah County Bookings > Run workflow**, select `init` once,
   then select `collect`. Check the run logs and verify the public output.
7. The scheduled run is **11:45 PM America/Los_Angeles (Pacific time) daily**,
   automatically adjusting between PST and PDT. GitHub scheduled workflows are
   not guaranteed to start at the exact minute, and the scheduled run remains
   disabled until `ENABLE_DAILY_COLLECTION=true` is explicitly configured.

**Important:** the `init` action creates a new empty archive only once, and will never
silently overwrite an existing one. The `suppress` action removes one booking ID from
future exports and prevents re-collection, but a formal correction process also needs to
address search-engine indexing, CDN caches and other copies.

For the real site, replace the preview's footer sentence about contact information with
an actual working correction/removal contact before publishing any personal data.

## Developer verification

Python 3.11+; locally:

```bash
python -m pip install -r requirements.txt pytest
pytest -q
python collector.py --test-html fixtures/fake_booking.html
```

The HTML test fixture is fictional and intentionally never published as real data.
For live testing, after all access and compliance checks:

```bash
python -m playwright install chromium
python archive_store.py init  # local only; no S3 credentials needed
export SCRAPER_CONTACT='you@example.com'
python collector.py
python -m http.server 8000 -d site
```

Production uses `ARCHIVE_BACKEND=s3` and the private bucket configuration.

## Licensing and liability

No county access permissions or data republication rights are implied by this
code. Review applicable policies before operating an automated public feed.
