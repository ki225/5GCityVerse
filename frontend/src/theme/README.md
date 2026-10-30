# Global colors

`colors.json` is the shared frontend palette. Rebuild the frontend after changing colors.

- `primary`: City green for step selection, buttons, links, and AI decision emphasis.
- `slate` and `gray`: Warm gray scales for text, borders, and disabled states.
- `surface`: Backgrounds for pages, panels, maps, cards, and inputs.
- `red`, `amber`, and `green`: Semantic scales for errors, warnings, and success.
- `slice`: Identity colors for the four existing slices.
- `map`: Canvas background, UE identity colors, and shared building materials.

Usage:

```tsx
// React / Tailwind
<button className="bg-primary-600 text-white hover:bg-primary-700">Save</button>

// SVG / Canvas
import { colors } from './colors'
context.fillStyle = colors.map.background
```

```css
/* main.tsx installs all palette variables before React mounts. */
.card { background: var(--color-surface-card); color: var(--color-primary-900); }
```

Tailwind reads the same JSON directly; regular CSS uses `--color-{category}-{shade}`. New product components should reference the palette instead of hard-coding a separate purple or blue theme. Each painter still defines local light and shade for parts of the 3D city model; the controls' primary color does not replace those material details.
