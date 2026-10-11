// How a name becomes a picture. The glyph is 1 row tall and as wide, so 2 to 2.5 cells, and
// the font centers it in `cells` (3). wezterm only lets a glyph spill out of its own cell
// when the NEXT cell is a real space (allow_square_glyphs_to_overflow_width =
// "WhenFollowedBySpace", its default); NBSP does not count. The rest of the pad is braille
// blanks: they draw nothing but are not whitespace, so neither markdown (two trailing spaces
// = hard break) nor the renderer's end-of-block trim eats the space the glyph needs.
// Trimmed pad = emote squeezed into one cell.
export const padFor = (cells: number) => ' ' + '⠀'.repeat(Math.max(1, cells - 2))

const LEAD = '([{"\'*_~'
const TRAIL = '.,!?;:)]}"\'*_~'

function swapToken(token: string, glyphs: Readonly<Record<string, string>>, pad: string): string {
  let start = 0
  while (start < token.length && LEAD.includes(token[start]!)) start++
  // Longest match first, so `D:` wins before its colon gets peeled off as punctuation
  for (let end = token.length; end > start; end--) {
    const name = token.slice(start, end)
    // hasOwn: "constructor" and friends are not emotes
    if (Object.hasOwn(glyphs, name)) return token.slice(0, start) + glyphs[name] + pad + token.slice(end)
    if (!TRAIL.includes(token[end - 1]!)) break
  }
  return token
}

/** Emote names standing as their own word become glyphs; code blocks and `code spans` are left alone. */
export function emotify(markdown: string, glyphs: Readonly<Record<string, string>>, pad: string): string {
  const lines = markdown.split('\n')
  let fence: string | undefined
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!
    const mark = /^\s*(`{3,}|~{3,})/.exec(line)?.[1]
    if (fence !== undefined) {
      if (mark !== undefined && mark[0] === fence[0] && mark.length >= fence.length) fence = undefined
      continue
    }
    if (mark !== undefined) {
      fence = mark
      continue
    }
    // Odd entries are inline code spans, then odd entries of each prose part are whitespace runs
    const parts = line.split(/(`+[^`]*`+)/)
    for (let j = 0; j < parts.length; j += 2) {
      const words = parts[j]!.split(/(\s+)/)
      for (let k = 0; k < words.length; k += 2) words[k] = swapToken(words[k]!, glyphs, pad)
      parts[j] = words.join('')
    }
    lines[i] = parts.join('')
  }
  return lines.join('\n')
}
