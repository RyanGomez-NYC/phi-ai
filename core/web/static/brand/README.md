# Brand assets

## The wordmark

`wordmark.svg` — φ(ai), shown in the masthead of every page
(`core/web/templates/base.html`, `login_base.html`).

It is **vector artwork**: paths, not a raster export and not text set in
a typeface. That is the whole asset — there is no source file kept
somewhere else and no export step, so changing the mark means editing
the SVG.

**Why paths and not `<text>`.** An SVG loaded through `<img>` is an
isolated document that loads no external resources, so it cannot reach
the vendored typefaces in `../fonts/`. A `<text>` element here would
fall back to whatever serif the reading machine happened to have, and
the mark would be a different width on every one of them — which for a
wordmark sitting in a fixed masthead box is not a cosmetic problem. The
paths render identically everywhere, and the intrinsic 294 × 120 box is
exactly the ink, so there is no transparent margin to trim.

**Why geometric letterforms in a serif interface.** The name is
mathematical notation — φ applied to `ai` — and notation is set in
neutral forms. The mark does carry the interface's stroke modulation
though: vertical strokes of 11 units against horizontals of 7.5, the
same thick/thin relationship Cormorant Garamond sets the headings in. It
reads as part of the same system without pretending to be a serif face
at 32px.

**Colour is `currentColor` throughout**, so the mark takes the colour of
whatever paints it. Loaded as an image there is no page to inherit from,
so the SVG root carries the interface's ink colour (`#1b2230`) as its own
default and flips to the page ground (`#f6f6f7`) under
`prefers-color-scheme: dark`. A dark surface needs no separate export —
the raster mark this replaced would have needed one.

## How it is sized, and the tradeoff in it

The mark is sized by **height**, not width: `--brand-mark-height: 32px`
in `../app.css`. `--brand-width: 136px` is still there and still
correct, but it is now the sub-line's measure alone — the width
"CLINICAL RECORD PLATFORM" occupies at 9px, which the sub-line
justifies itself across.

Those used to be one number. They are two now because "φ(ai)" is five
glyphs where the previous wordmark was nine: its natural aspect is about
2.45∶1 rather than 5∶1, and scaled to fill 136px it would stand 55px
tall and push the masthead apart.

**Known gap, deliberate:** the mark is therefore narrower than the
sub-line beneath it. It sets flush left, so the left edges align and the
right edges do not. The alternative was to track five glyphs out to five
times their natural width to meet the sub-line, which would read as five
separated characters rather than one word, and would set the parentheses
so far from "ai" that they would stop enclosing it. A narrower mark is
the better of the two, but it is a compromise and not a composition
anyone would have drawn from scratch.

If the sub-line ever shortens enough that the two measures converge,
this is the paragraph to revisit.

## Notes for anyone replacing it

- **Keep it same-origin.** The CSP is `img-src 'self' data:`
  (`core/web/security.py`); a mark on a CDN will not render, and a PHI
  page has no business requesting one from a third party anyway. For the
  same reason the SVG must stay self-contained — no `<image>`, no
  external `@font-face`, no linked stylesheet.
- **No script in the SVG, ever.** `script-src 'none'` covers the whole
  interface; an SVG that needed script would be the one exception, and
  a logo is the worst possible reason to make one.
- **Keep the `alt` text meaningful.** It is the application's name, and
  it is what a screen reader announces and what shows if the file ever
  404s. It reads `φ(ai)`.
- **Keep the `width`/`height` attributes matching the `viewBox`.** They
  reserve the correct box before the file arrives; without them the
  navigation shifts sideways as the masthead settles. Both `<img>` tags
  carry them.
- **Check both templates.** The mark appears in `base.html` and in
  `login_base.html`, which is a separate shell for the pages a person
  sees before they have an identity.
