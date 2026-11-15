---
name: brand-guidelines
description: Applies Anthropic's brand colors with the user's supplied Anthropic Sans, Serif and Mono fonts to artifacts that benefit from Claude's look-and-feel. Use it when brand colors or style guidelines, visual formatting, or company design standards apply.
license: Complete terms in LICENSE.txt
---

# Anthropic Brand Styling

## Overview

Use Anthropic's brand palette with the user's local typography adaptation. Upstream: https://github.com/anthropics/skills/tree/main/skills/brand-guidelines .

**Keywords**: branding, corporate identity, visual identity, post-processing, styling, brand colors, typography, Anthropic brand, visual formatting, visual design

## Brand Guidelines

### Colors

**Main Colors:**

- Dark: `#141413` - Primary text and dark backgrounds
- Light: `#faf9f5` - Light backgrounds and text on dark
- Mid Gray: `#b0aea5` - Secondary elements
- Light Gray: `#e8e6dc` - Subtle backgrounds

**Accent Colors:**

- Orange: `#d97757` - Primary accent
- Blue: `#6a9bcc` - Secondary accent
- Green: `#788c5d` - Tertiary accent

### Typography — local preference

- Headings and interface text: Anthropic Sans Variable Text; Chinese companion PingFang SC.
- Reading text: Anthropic Serif Variable Text; Chinese companion Songti SC.
- Code: Anthropic Mono Web.
- Choose by content role; dense application UI stays in Sans. Large editorial titles may use Serif.
- Read [references/typography.md](references/typography.md) before applying fonts. It provides exact family names, variable weights, italic faces, Chinese fallbacks and native document guidance.
- Web font declarations and all six supplied WOFF2 faces are bundled under assets/. Use [assets/anthropic-fonts.css](assets/anthropic-fonts.css) with its adjacent fonts/ directory.

### Application

Preserve content hierarchy and use readable text contrast. Apply the palette consistently to text and shapes. Use RGB values in native document formats and CSS colors on the web. Preview the final artifact to check font resolution, mixed Chinese/Latin text, wrapping and clipping.
