// Category chip colors. The background is stored per category (categories.cat_color);
// the text color is derived from it so the editor only needs one control per category.

// Used when a category has no color yet — a row predating the cat_color column that the
// migration's backfill didn't match.
export const CAT_FALLBACK = '#64748b'

/**
 * Relative luminance of a #rrggbb color, 0 (black) to 1 (white).
 * sRGB coefficients, gamma-corrected per WCAG 2.x.
 */
function luminance(hex) {
  const to_linear = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4))
  const r = to_linear(parseInt(hex.slice(1, 3), 16) / 255)
  const g = to_linear(parseInt(hex.slice(3, 5), 16) / 255)
  const b = to_linear(parseInt(hex.slice(5, 7), 16) / 255)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Normalize a stored or picked color to #rrggbb, or return the fallback. */
export function norm_color(raw) {
  if (!raw) return CAT_FALLBACK
  const hex = raw.startsWith('#') ? raw : '#' + raw
  return /^#[0-9a-fA-F]{6}$/.test(hex) ? hex.toLowerCase() : CAT_FALLBACK
}

// Luminance above which a chip takes dark text. Deliberately higher than the 0.179 point
// where black and white contrast equally: 0.179 would put black text on orangered, and the
// four original categories had white there. This cut reproduces all four of their old text
// colors (white on orangered, dark on orange, mediumseagreen and skyblue). Lower it to 0.179
// to maximize contrast instead, at the cost of flipping Security's text to black.
const DARK_TEXT_ABOVE = 0.30

/** Pick readable text for a background: near-black on light chips, white on dark ones. */
export function text_on(bg) {
  return luminance(norm_color(bg)) > DARK_TEXT_ABOVE ? '#111111' : '#ffffff'
}
