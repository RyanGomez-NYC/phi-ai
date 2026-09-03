# The interface: φ(ai)

The web interface (`core/web/`) is drawn in **IBM Plex** on a cool near-white
ground: Plex Sans for the interface, Plex Mono for identifiers and machine
output, Plex Serif where a page wants an editorial voice. Structure is carried
by hairline rules and generous whitespace rather than by boxes, and colour is
spent sparingly so that when it appears it *means* something.

That is a style choice, and it is also the right one for this application. HIM
and compliance staff sit in this interface for hours. Legibility, density and
obvious state matter more than personality, and a page drawn in hairlines
leaves colour free to carry meaning on the few elements that need it — a
refusal, a withheld count, an unsigned draft.

## The values are not in this file, deliberately

**`core/web/static/app.css` is the source of truth for every token.** Read the
`:root` block at the top of it.

This document used to tabulate the hex codes. It went stale the moment the
stylesheet was rewritten, and stayed wrong for ten days while still reading as
authoritative — every one of the 23 custom properties it named had ceased to
exist, and it described a typeface pairing the CSS no longer loaded. A
hand-copied list rots in both directions: the copy drifts from the source, and
a reader who trusts the copy writes code against values that are gone.

So this file documents the *system* — what the token families are for and how
to choose between them. For a value, read the stylesheet.

## Token families, and what each is for

| Family | Use it for |
|---|---|
| `--navy`, `--orange` | The two brand colours. Navy is structural and load-bearing; orange is the accent and is rationed. |
| `--ink` → `--disabled` | The text ramp, darkest to lightest. Pick by how much attention the text should command, never by taste. |
| `--line`, `--line-strong`, `--line-soft` | Rules and borders. Strong for a boundary that separates concerns, soft for one that only groups. |
| `--wash`, `--wash-2`, `--wash-3` | Ground tints. Panels and inset regions, in increasing depth. |
| `--good`, `--warn-bg` / `--warn-fg`, `--amber` | Semantic state, each with a matching background and sometimes a line. **Never** used decoratively — a green in this interface is a claim that something is right. |
| `--sans`, `--mono`, `--serif` | The three faces. Mono is not styling: it marks a string the reader may need to copy exactly — an MRN, an audit action, a config key. |

## Rules that are load-bearing

- **Semantic colour is a claim.** `--good` on a chip asserts a verified state.
  If the state is not verified, the chip is not green.
- **Mono means machine-exact.** Identifiers, event names, file paths, env
  vars. Prose never takes mono for emphasis.
- **Colour as stroke, not fill.** Outlined controls and bordered cards. Solid
  fills are reserved for the single active item in a set.
- **The sticky offsets are hardcoded, and they are load-bearing.** The
  sidebar, header and rails pin with literal pixel values (`top: 63px` in
  several rules), which means the header's height is duplicated across the
  stylesheet rather than derived. Changing the header's padding moves content
  underneath it in every one of those places. Grep `top: 63px` before touching
  header metrics, and measure with `getComputedStyle` afterwards.

## Checking this file against the stylesheet

This document makes no claim that a token exists. If you want to confirm the
families above still match, list what is actually defined:

```bash
awk '/^:root[[:space:]]*\{/{f=1;next} f&&/^\}/{exit} f' \
    core/web/static/app.css | grep -oE '\--[a-z0-9-]+' | sort -u
```

Anything in that list whose purpose is not obvious from its name is a gap in
this document, not in the stylesheet.

## Known dead assets

`core/web/static/fonts/` still ships Cormorant Garamond and Lora `.woff2`
files from the previous design system. Nothing references them — `app.css`
mentions neither family. They are dead weight on every deployment that copies
the static directory and can be removed once someone confirms no downstream
template pulls them directly.
