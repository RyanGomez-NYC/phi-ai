# The interface: φ(ai)

The web interface (`core/web/`) is built on **Classical** — an editorial,
book-like design system on a soft near-white ground. Cormorant Garamond
headings over Lora body text, hairline rules carrying the structure, and
colour applied as **stroke rather than fill**: outlined buttons, bordered
cards, no solid blocks of accent.

That is a style choice, and it is also the right one for this
application. HIM and compliance staff sit in this interface for hours.
Legibility, density and obvious state matter more than personality, and a
page drawn in hairlines leaves colour free to *mean* something on the few
occasions it appears — a broken audit chain, a record eligible for
disposal, a PHI warning above a decrypted resource.

Everything lives in one stylesheet, `core/web/static/app.css`. There is
no build step, no framework and **no JavaScript** — this interface runs
under `script-src 'none'` (`core/web/security.py`) and nothing in the
design needs it. The ask-the-assistant drawer is a native `<details>`
element for exactly this reason.

**On the wordmark.** `φ(ai)` is the construction this interface has
always used — φ is *phi* is PHI, with the product noun in the
parentheses. Treat it as **provisional, pending an actual branding
decision**: it is a mark chosen for consistency with the construction
that came before, not one anyone has settled on. What it is no longer is
unfinished. `core/web/static/brand/wordmark.svg` is vector artwork of
`φ(ai)`, drawn as paths rather than set in type - an SVG loaded through
`<img>` cannot reach the vendored typefaces, so text would render at a
different width on every machine. Both templates reference it and the
`alt` text matches. See `core/web/static/brand/README.md` for how it is
sized, and for the one compromise in it: the mark is narrower than the
sub-line beneath it and sets flush left, because five glyphs cannot fill
the sub-line's measure without tracking them apart until they stop
reading as one word.

---

## Tokens

Retune the look by editing the `:root` block at the top of `app.css`.
Never hard-code a hex, a font name or a pixel value a token already
carries.

| Role | Token | Value |
|---|---|---|
| Ground | `--color-bg` | `#f6f6f7` |
| Raised surface | `--color-surface` | `#e9eaee` |
| Text | `--color-text` | `#1b2230` |
| Accent | `--color-accent` | `#33507f` — a deep, sober navy |
| Second accent | `--color-accent-2` | `#c9724d` — terracotta, used sparingly |
| Hairline | `--color-divider` | 15% text |

Each role carries a **100–900 tonal ramp** generated on one shared
perceptual lightness scale, so the same step of any role has the same
visual weight. Use 100–300 for tinted fills, hovers and subtle borders,
500 as the base, and 700–900 for text sitting on a tint. Prefer a ramp
step over an ad-hoc `color-mix()`.

Spacing is `--space-1` … `--space-8` on a 1.15× density scale; radii are
`--radius-sm/md/lg`; elevation is `--shadow-sm/md/lg` and is a whisper,
never a drop shadow.

### The signal colours are separate on purpose

`--color-ok`, `--color-warn` and `--color-danger` sit outside the two-hue
palette because "the audit chain is broken" and "this record may be
disposed of" must not read as the same kind of thing as a link. They are
still used the system's way — a 2px rule down the left edge and the
faintest tint, never a filled block.

---

## Type

- `--font-heading` — Cormorant Garamond. `--font-heading-weight` is
  semibold, and that is the ceiling: **bold is retired.** The bigger the
  text sets, the lighter it sets, so `h1` drops to the normal cut.
- `--font-body` — Lora. Emphasis is italics and weight, never a
  sans-serif.
- `--font-mono` — the system monospace, and only for opaque references,
  hashes and stored JSON. An EMR reference like `eAB12cd3` is read
  character by character and `0`/`O` have to be told apart. Everything
  else that is a figure — counts, dates, table columns — uses `.num` and
  the serif's **tabular numerals**. Running prose keeps its text figures;
  Lora's tabular feature widens word-spaces and would loosen paragraphs.

### Typefaces are vendored, not linked

`core/web/static/fonts/` holds the `latin` and `latin-ext` subsets of
both faces (SIL OFL, ~176 KB total). A Google Fonts link would mean every
page a clinician opens announces itself to a third party, and would leave
air-gapped and restricted-egress deployments — the normal shape of a
hospital network — with no typeface at all. **The CSP stays
`default-src 'self'`; do not widen it for a font.** See
`core/web/static/fonts/README.md`.

---

## Page furniture

| Class | What it is |
|---|---|
| `.kicker` | The small-caps line above an `h1`, naming the section — *Governance*, *Worklist*, *Disposition* |
| `.lede` | The standing explanation under the title, capped at 74ch |
| `.card` | A bordered, **unfilled** surface. `.card.bare` drops the border where a table's own rules already draw the edge |
| `.tablewrap` | Wraps every table so wide content scrolls in its own container instead of pushing the page sideways — an EHR panel is narrow |
| `.grid.stats` | The figures strip: one bordered band divided by hairlines, numbers set large and light in the heading face |
| `.status` `.ok/.warn/.danger` | A rule and a tint. Its `<strong>` is the headline |
| `.pill` | A small state label. Tinted from a ramp, or **outlined** where the state is one someone must act on (`denied`, `eligible for disposal`) |
| `.purpose` | Purpose of use, in the second accent — the one column that answers "why" |
| `.num` | Tabular figures in the serif, for counts |
| `.keyvalue` | A two-column table read as a definition list |
| `.phi-notice` | The warning that sits directly above decrypted content |
| `.colophon` | The footer band: release, the reminder that the session is on the record, and who is signed in |
| `.brand` | The masthead wordmark — the vector mark at `static/brand/wordmark.svg`, sized by height, over its small-caps sub-line. See `core/web/static/brand/README.md` |

The masthead is **one row at every width**. It holds the wordmark, the
navigation and the patient lookup, and nothing else — the signed-in
identity lives in the footer instead, because a masthead that grows a
second line moves the whole page down and puts the tabs somewhere
different on every window. Below 1180px the tabs give up their padding
first; past that the navigation scrolls sideways, with a thin scrollbar
so it is visible that it does. `--chrome-height` and `--footer-height`
are tokens because the assistant workspace sizes itself against them.

Interactive states are themed, never browser defaults: hovers and pressed
states come from the accent ramp, keyboard focus is a 2px accent
`:focus-visible` ring, `::selection` is an accent tint, and disabled
controls drop to 45% opacity.

---

## Rules of thumb

**Do**

- Let hairlines carry the structure, and give text room — the spacing
  scale is airy by design.
- Draw with borders, rules and underlines.
- Set every figure that stands as a figure in tabular numerals.

**Don't**

- Don't fill a card or a button with solid accent colour.
- Don't reach for a heavy drop shadow; elevation here is a whisper.
- Don't swap in a sans-serif for emphasis — weight and italics do that.
- Don't add a third hue. If something needs to stand out and is not a
  signal state, it probably needs a rule rather than a colour.

## The assistant workspace

The assistant is the only page that leaves the ordinary page frame: it
sets `fullbleed` and draws a three-pane `.workspace` — the thread on the
left, the exchange in the middle, what the answer was built from on the
right. A citation should be checkable without scrolling away from the
answer that made it, which is the whole reason for the layout.

The rails are full-height and sticky; below 1100px they stop being rails
and stack under the exchange. Two things in it are load-bearing rather
than decorative:

- **Starter chips are separate forms.** A submit button carrying
  `name="question"` inside the ask form collides with the textarea of the
  same name, and the empty textarea — first in the document — is the
  value that wins. Each chip posts its own question and nothing else.
- **It is still `script-src 'none'`.** Every affordance here is a form.
  Nothing types into the box for you, because nothing can.

Where the assistant is enabled and the user's role includes it, `/`
redirects to it and it leads the navigation. Where it is absent — the
default — `/` is the platform overview exactly as before. The overview
keeps its own address, `/overview`, so making the assistant the front
door never makes it unreachable.

## The narrow panel

`body.embedded` is the same interface inside an EHR's iframe, where the
app gets a short, narrow panel beside the chart rather than a browser
window. Density goes up, the masthead stops being sticky and the
colophon is dropped; **nothing is removed**, because a clinician in the
frame can still need any of it. See `runbooks/RUNBOOK_SMART_LAUNCH.md`.
