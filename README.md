# Hillary Meta Catalog Sync v2

This package contains the 30 approved Idea Lists you supplied. Teacher Style was removed. Duplicate Pilates Princess and duplicate Prime Day Top 100 entries were removed. Work Wear was retained.

## First run
1. Copy these files into your local `hillary-meta-catalog` repository folder.
2. Right-click `setup.command` and choose Open.
3. After setup completes, right-click `run.command` and choose Open.
4. Leave Chrome for Testing alone while it works.

## What it creates
- `public/meta_catalog.csv` — eventual GitHub Pages/Meta feed.
- `reports/approved_lists_resolved.csv` — click and verify every clean URL and actual Amazon title.
- `reports/new_lists_found.csv` — newly discovered storefront lists not yet approved.
- `output/products_needing_review.csv` — missing API records, prices, or images.

## GitHub publishing
Test first. Then change `auto_git_publish` from false to true in `config/settings.json`.

## Meta sets
Products carry their Amazon Idea List names in `custom_label_0` through `custom_label_4`. Create each Meta set rule once; products then update automatically with the feed.

Amazon credentials are requested at runtime and are never saved.
