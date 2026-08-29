# Vendored typefaces

Cormorant Garamond and Lora, the two faces the φ(ai) design system
specifies. Both are licensed under the **SIL Open Font License 1.1**,
which permits redistribution alongside this software.

They are vendored rather than linked from Google Fonts on purpose. The
interface serves PHI under a `default-src 'self'` Content-Security-Policy
(`core/web/security.py`); a webfont link would mean every page a
clinician opens announces itself to a third party, and would leave
air-gapped and restricted-egress deployments — the normal shape of a
hospital network — with no typeface at all.

Files are the `latin` and `latin-ext` subsets of the upstream variable
fonts. One file per subset carries every weight the interface uses, which
is why the `@font-face` rules in `../app.css` declare a `font-weight`
range rather than a single value.

Refresh them from the upstream sources when the faces are updated:

- https://fonts.google.com/specimen/Cormorant+Garamond
- https://fonts.google.com/specimen/Lora
