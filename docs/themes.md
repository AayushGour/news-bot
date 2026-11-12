# Slide themes - how-to (verified by tester, T278)

Themes live in `config/themes/*.json`. `THEME` in `.env` selects one.

## The `doodle` theme
White page, centred handwriting (Shantell Sans headlines, Patrick Hand body), one red wobbly underline under a word of the hook headline. Text only: no kicker, cards, footer or images (photos, insets, backgrounds and repo logos are never drawn).

### Pin it
Set `THEME=doodle` in `.env` (or leave `THEME` empty and let the composer pick). Verified: rendering a real awaiting_approval deck (items 512, 511, from a DB backup copy) through `pipeline.stages.render.render` with `theme="doodle"` produced all slides, no overflow/recompose.

### Random rotation
`THEME=random` and `THEME=rotate` include `doodle` among the themes (`available_themes` lists it). The pick is deterministic per item id.

### No two posts in a row share a theme
An automatic pick (`random`, `rotate` or the composer's choice) avoids the theme
of the last published post, recorded in `items.rendered_theme`. If an item
still lands on that theme by the time it publishes, it is re-rendered with
another theme first and you get one Telegram note. A pinned or named `THEME`
is never swapped.

### Photo decks skip it
If a deck contains a `photo` slide, random/rotate never choose `doodle` (it would turn the photo slide into a headline about an absent image). An explicit `THEME=doodle` pin is still honoured, and then the photo slide renders as a plain headline, no image.

### Fonts and licences
Fonts are bundled in `config/fonts/doodle/` and loaded from local `file://` (no network): `ShantellSans-Variable.ttf` and `PatrickHand-Regular.ttf`, both SIL Open Font License 1.1 (`OFL-ShantellSans.txt`, `OFL-PatrickHand.txt` in the same folder). Fonts are only loaded for doodle decks. If the fonts folder is missing, the render still completes with fallback fonts (verified: 2 slides, no overflows).
