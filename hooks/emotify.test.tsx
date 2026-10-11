import { expect, mock, test } from 'claude-code/testing'

import { emotify, padFor } from './emotify'

const GLYPHS = { KEKW: 'K', Pog: 'P', Pepega: 'G', Clap: 'C', EZ: 'E', forsenE: 'F', 'D:': 'D' }
const PAD = padFor(3)
const swap = (text: string) => emotify(text, GLYPHS, PAD)
const g = (name: keyof typeof GLYPHS) => GLYPHS[name] + PAD

test('the pad is a real space, then blanks no trim eats, one cell each', async () => {
  expect(PAD).toBe(' \u2800')
  expect(padFor(4)).toBe(' \u2800\u2800')
  expect(swap('gg EZ').trimEnd()).toBe(`gg ${g('EZ')}`)
})

test('swaps a name standing alone, neighbours separately', async () => {
  expect(swap('it works Pog')).toBe(`it works ${g('Pog')}`)
  expect(swap('Pepega Clap')).toBe(`${g('Pepega')} ${g('Clap')}`)
})

test('keeps punctuation around the name', async () => {
  expect(swap('lol KEKW.')).toBe(`lol ${g('KEKW')}.`)
  expect(swap('(forsenE)')).toBe(`(${g('forsenE')})`)
  expect(swap('**Pog**')).toBe(`**${g('Pog')}**`)
})

test('D: is an emote, a drive letter is not', async () => {
  expect(swap('you deleted prod D:')).toBe(`you deleted prod ${g('D:')}`)
  expect(swap('D:, really')).toBe(`${g('D:')}, really`)
  expect(swap('D:\games and D:/games')).toBe('D:\games and D:/games')
})

test('leaves partial words, other casing and object keys alone', async () => {
  const text = 'Pogchamp KEKWait kekw Pog.png xPepega constructor toString __proto__'
  expect(swap(text)).toBe(text)
})

test('leaves code alone', async () => {
  const text = ['`KEKW` and `a KEKW b`', '```c', 'int KEKW = 1; // Pepega', '```', '~~~', 'Pog', '~~~'].join('\n')
  expect(swap(text)).toBe(text)
  expect(swap('`x` KEKW `y`')).toBe(`\`x\` ${g('KEKW')} \`y\``)
  expect(swap('```\nKEKW\n```\nKEKW')).toBe(`\`\`\`\nKEKW\n\`\`\`\n${g('KEKW')}`)
})

test('keeps whitespace and newlines as written', async () => {
  expect(swap('a  KEKW\n\n- Pog\tb')).toBe(`a  ${g('KEKW')}\n\n- ${g('Pog')}\tb`)
})

const reply = { text: 'it works Pog', isFirstOfReply: true }
const packFile = JSON.stringify({ cells: 3, glyphs: GLYPHS })

for (const [term, want] of [['WezTerm', `it works ${g('Pog')}`], ['vscode', 'it works Pog']] as const) {
  test(`draws ${term === 'WezTerm' ? 'glyphs' : 'plain names'} under TERM_PROGRAM=${term}`, async ($, on) => {
    mock.env(on, { TERM_PROGRAM: term, HOME: '/home/baj' })
    mock.store(on)
    on('fs.read', (_, e) => {
      // The engine resolves the path against the host's drive, so only the tail is portable
      if (!e.path.replaceAll('\\', '/').endsWith('/home/baj/.twitch-emotes/emotes.json')) throw new Error(`unexpected read of ${e.path}`)
      return { value: packFile }
    })
    let drawn: string | undefined
    on('ui.render', { component: 'AssistantMessage' }, ($$, e) => {
      drawn = e.props.text
      const { Text } = $$.ui.resolve(e)
      return <Text>{e.props.text}</Text>
    })
    await $.ui.mount({ plugin: 'twitch-emotes', surface: 'terminal', component: 'AssistantMessage', props: reply })
    expect(drawn).toBe(want)
  })
}

test('tells the model which emotes exist, only where they draw', async ($, on) => {
  mock.env(on, { TERM_PROGRAM: 'WezTerm', HOME: '/home/baj' })
  mock.store(on)
  on('fs.read', () => ({ value: packFile }))
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'hi', scope: 'shared' }] }))
  const { sections } = await $.prompt.compose({ model: 'claude-opus-5-5', promptModel: 'claude-opus-5-5', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] })
  expect(sections.map(s => s.id)).toEqual(['intro', 'twitch-emotes:emotes'])
  expect(sections[1]!.text).toContain('KEKW Pog Pepega')
})
