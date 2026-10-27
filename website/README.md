# website/

The public marketing site for [SkulGen](https://skulgen.com) — a static site,
no build step, no framework. Deployed by copying these files straight onto
the server; nginx serves them directly (see `deploy/README.md` for the
server layout this lives alongside — it's the same VPS as the backend,
just a separate nginx server block with nothing dynamic behind it).

## Layout

- `index.html` — the main site (`skulgen.com`, `www.skulgen.com`)
- `app/index.html` — a placeholder at `app.skulgen.com` until the app has a
  real public store listing to send people to
- `assets/` — logo, icon and favicon, derived from the brand kit

## Changing it

Edit the HTML/CSS directly (everything is inlined in each page, on purpose —
there's nothing here that needs a build step). To publish a change:

```
scp -i ~/.ssh/tgcg_claude_ed25519 -r index.html app assets root@159.198.75.103:/var/www/skulgen-site/
```

No restart needed — nginx serves these files directly from disk.
