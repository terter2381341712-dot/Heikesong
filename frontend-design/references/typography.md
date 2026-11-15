# Local Anthropic typography

User-selected adaptation of Anthropic's official skill, installed 2026-10-21. Font choices below are local preferences; retain the upstream LICENSE.txt for the skill. The six user-supplied WOFF2 font files are bundled in assets/fonts/.

## Roles

| Role | Native macOS family | Chinese companion |
|---|---|---|
| Headings, navigation, buttons, forms, dense UI | Anthropic Sans Variable Text | PingFang SC |
| Reading passages, articles, reports, explanatory prose | Anthropic Serif Variable Text | Songti SC |
| Code and technical literals | Anthropic Mono Web | PingFang SC |

Use Sans for ordinary headings and Serif for reading text. A large editorial cover title may use Serif. Keep controls in Sans, including controls beside Serif prose. Use Mono for code, rather than all numbers or metadata. Select by semantic role, not a fixed point-size threshold.

All six fonts have a wght axis from 300 to 800; normal and italic are separate files. Sans and Serif also have an opsz axis from 16 to 48. Start with body weight 400, headings 500–600, restrained italics, and optical sizing auto. Use real italic faces when supported.

These font files lack Chinese glyphs. Assign PingFang SC to Chinese UI text and Songti SC to Chinese reading text. For other machines, fall back to Noto Sans CJK SC / Source Han Sans SC and Noto Serif CJK SC / Source Han Serif SC when available. Check mixed Chinese/Latin line heights and punctuation in the rendered output.

## Web output

Copy assets/anthropic-fonts.css and assets/fonts/ together into the generated project's assets directory, retaining the relative paths. Import the CSS and apply its --font-ui, --font-reading and --font-code variables to the appropriate elements. CSS aliases Anthropic Sans / Anthropic Serif / Anthropic Mono refer to the bundled WOFF2 files. Prefer the bundled files for reproducible rendering over a dependency on the viewer's system fonts.

Use font-optical-sizing: auto. Wait for document.fonts.ready before capturing a preview or printing. Verify that the requested font actually loaded and that Chinese fallback remains legible.

## Native documents and presentations

This package includes web fonts in WOFF2 format. For native documents, first check whether corresponding Anthropic TTF fonts are installed on the destination machine; TTF files are not included in this package. Use the native family names from the table, not the CSS aliases. Their normal-face PostScript names are AnthropicSansVariable-TextRegular, AnthropicSerifVariable-TextRegular and AnthropicMonoWebWeb-Regular; italic names append Italic.

For DOCX/PPTX, set Latin and East Asian font assignments separately where the format allows it. Verify font resolution in the actual document renderer. If that renderer cannot handle variable fonts, create static TTF instances at the required weights with fontTools.varLib.instancer for the output workflow and verify them; keep the installed variable fonts intact. When falling back, state which face was substituted.
