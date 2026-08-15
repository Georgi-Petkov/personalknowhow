# personalknowhow.com — Theme spec

## Instructions for Claude Code
Implement as CSS custom properties with a `data-theme` attribute toggle (persist choice
in localStorage). Default to dark. Add a small sun/moon toggle in the top-right nav.

## Dark theme (VS Code Dark+ inspired)

```css
[data-theme="dark"] {
  --bg-primary:    #1e1e1e;  /* editor background */
  --bg-secondary:  #252526;  /* sidebar/panel */
  --bg-tertiary:   #2d2d30;  /* cards, code blocks */
  --border:        #3c3c3c;

  --text-primary:  #d4d4d4;
  --text-secondary:#9d9d9d;
  --text-muted:    #6a6a6a;

  --accent:        #3794ff;  /* VS Code blue, links/CTAs */
  --accent-hover:  #5aa6ff;
  --accent-dim:    #264f78;  /* selection/highlight blue */

  --code-bg:       #1e1e1e;
  --code-border:   #3c3c3c;
}
```

## Light theme (LinkedIn inspired)

```css
[data-theme="light"] {
  --bg-primary:    #ffffff;
  --bg-secondary:  #f3f2ef;  /* LinkedIn's off-white background */
  --bg-tertiary:   #f8f9fa;  /* cards */
  --border:        #e0e0e0;

  --text-primary:  #1d2226;  /* near-black, LinkedIn body text */
  --text-secondary:#565e64;
  --text-muted:    #86888a;

  --accent:        #0a66c2;  /* LinkedIn blue */
  --accent-hover:  #004182;
  --accent-dim:    #e7f0fa;  /* light blue highlight/hover bg */

  --code-bg:       #f6f8fa;
  --code-border:   #d0d7de;
}
```

## Usage notes
- Code/JSON/curl blocks: use `--code-bg` + monospace, keep syntax highlighting readable
  in both themes (don't hardcode a single dark-only highlight theme).
- CTAs (GitHub link, "Try the live demo", "Join the waitlist"): `--accent` background or
  border, `--accent-hover` on hover.
- Body copy: `--text-primary`; secondary/meta text (like "Evidence-based knowledge
  graph · Real MCP servers" tagline): `--text-secondary`.
- Cards/sections (How it works steps): `--bg-tertiary` with `--border`.
- Respect `prefers-color-scheme` for first visit if no localStorage value is set yet.
