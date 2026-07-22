---
type: reference
tags: [meta, wiki-app]
created: 2026-07-06
updated: 2026-07-22
---

## Text styling

Some **bold**, *italic*, ~~strikethrough~~, ==highlighted text==, `inline code`, and a #tag plus #nested/tag.

Here is a [[conventions]] wikilink, an aliased [[conventions|vault rules]] link, an unresolved [[does-not-exist]] link, and an [external link](https://obsidian.md).

%%This comment should be invisible in reading view.%%

## Callouts

> [!note]
> A plain note callout body.

> [!tip] Callouts can have custom titles
> And body text under the custom title.

> [!warning]
> Watch out for this.

> [!danger] Collapsible open
> Danger body content.

> [!faq]- Collapsed by default
> This body is hidden until expanded.

> [!quote]
> Someone said something memorable.

> [!example]
> - item one
> - item two

> A plain blockquote for contrast.

## Code

```python
def egg_throw(angle: float) -> str:
    """Return the lane covered by an egg."""
    if angle > 45:
        return "high"
    return "low"
```

Hover the code block above — a small `copy` pill fades in top-right and copies the raw block to the clipboard.

## Diff

The `diff` artifact renders unified patches with hairline hunk separators, add/remove tones from the theme palette, and horizontal scroll on long lines. Sample source:

```diff
--- a/frontend/src/session.tsx
+++ b/frontend/src/session.tsx
@@ -14,7 +14,8 @@ export function SessionView() {
   const [open, setOpen] = useState(false);
-  const items = load();
+  const items = load({ recent: true });
+  const now = Date.now();
   return items.map(render);
 }
```

## Table

| Move | Startup | On shield |
| ---- | ------- | --------- |
| Nair | 3 | -2 |
| Fair | 17 | -8 |

## Tasks

- [x] Done task
- [ ] Open task

## Footnotes

Yoshi's double jump has armor.[^1]

[^1]: Knockback below a threshold does not break it.

---

That horizontal rule above ends the test.
