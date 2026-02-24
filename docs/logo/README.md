# Logo

Edit `logo.svg`, then rebuild:

```bash
# PNG
rsvg-convert -w 800 -h 800 logo.svg -o logo.png

# JPG
rsvg-convert -w 800 -h 800 logo.svg -o /tmp/logo.png \
  && sips -s format jpeg /tmp/logo.png --out logo.jpg \
  && rm /tmp/logo.png
```

Requires: `rsvg-convert` (librsvg), `sips` (macOS built-in).
