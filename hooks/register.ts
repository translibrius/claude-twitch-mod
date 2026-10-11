import type { EngineInterface, Register } from 'claude-code'

import { emotify, padFor } from './emotify'

const PACK = 'https://github.com/translibrius/claude-twitch-emotes/releases/latest/download'
const MARK = 'twitch-emotes'
const PROMPT_EMOTES = 150

type Pack = { cells: number; glyphs: Record<string, string> }

// wezterm.lua in the data dir. The user's config calls it once; everything that can change
// lives here so the config never needs a second edit. It watches `generation`, which
// emotefont bumps on every build while writing the font to a fresh font-<N> folder: a new
// font_dirs entry is what makes wezterm re-read fonts, so new emotes show up live.
const shim = (dir: string) => `-- Written by the twitch-emotes Claude Code mod on every session start. Edits do not survive.
local wezterm = require 'wezterm'
local dir = '${dir}'
return function(config)
  wezterm.add_to_config_reload_watch_list(dir .. '/generation')
  local f = io.open(dir .. '/generation')
  local gen = f and f:read('l') or '0'
  if f then f:close() end
  local dirs = config.font_dirs or {}
  table.insert(dirs, dir .. '/font-' .. gen)
  config.font_dirs = dirs
  -- config.font is a plain table of fallbacks; ours goes last so it only catches the emote codepoints
  local font = config.font or wezterm.font('JetBrains Mono')
  table.insert(font.font, wezterm.font('Twitch Emotes').font[1])
  config.font = font
end
`

const hookLine = (dir: string, config: string) =>
  `-- ${MARK}: Twitch emotes in Claude Code (github.com/translibrius/claude-twitch-emotes). Delete these two lines to undo.\n` +
  `do local ok, te = pcall(dofile, '${dir}/wezterm.lua'); if ok then te(${config}) end end\n`

// Module variables die on reload; everything here is re-read from disk, env or the store.
let dir: string | undefined
let pack: Pack | undefined
let hasFont: boolean | undefined
let isOn: boolean | undefined

async function dataDir($: EngineInterface): Promise<string> {
  if (dir === undefined) {
    const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '.'
    dir = ((await $.env.get('TWITCH_EMOTES_DIR')) ?? `${home}/.twitch-emotes`).replaceAll('\\', '/')
  }
  return dir
}

async function loadPack($: EngineInterface): Promise<Pack | undefined> {
  try {
    pack = JSON.parse(await $.fs.read(`${await dataDir($)}/emotes.json`)) as Pack
  } catch {
    pack = undefined
  }
  return pack
}

async function enabled($: EngineInterface): Promise<boolean> {
  // Only wezterm is known to scale and overflow the glyphs right; elsewhere it could be tofu
  hasFont ??= (await $.env.get('TERM_PROGRAM')) === 'WezTerm' || (await $.store.get('anyTerminal')) === true
  isOn ??= ((await $.store.get('isOn')) ?? true) === true
  return hasFont && isOn
}

/** First run: pull the prebuilt font pack. $.fs only writes text, so curl does the binary. */
async function downloadPack($: EngineInterface): Promise<string | undefined> {
  const d = await dataDir($)
  for (const [file, to] of [
    ['TwitchEmotes.ttf', 'font-1/TwitchEmotes.ttf'],
    ['registry.json', 'registry.json'],
    ['emotes.json', 'emotes.json'],
  ] as const) {
    try {
      const got = await $.process.run(['curl', '-fsSL', '--create-dirs', '-o', `${d}/${to}`, `${PACK}/${file}`], { timeoutMs: 120_000 })
      if (got.exitCode !== 0) return `could not download ${file}: ${got.stderr.trim()}`
    } catch (error) {
      return `could not run curl: ${String(error)}`
    }
  }
  // Last, so a wezterm already watching it reloads onto a font that is there
  await $.fs.write(`${d}/generation`, '1')
  return undefined
}

async function wezConfig($: EngineInterface): Promise<{ path: string; text: string } | undefined> {
  const path = await $.env.get('WEZTERM_CONFIG_FILE')
  if (path === undefined) return undefined
  try {
    return { path, text: await $.fs.read(path) }
  } catch {
    return undefined
  }
}

async function setup($: EngineInterface): Promise<string> {
  const d = await dataDir($)
  const manual = `Add this to your wezterm config, just before it returns:\n\n${hookLine(d, 'config')}`
  const cfg = await wezConfig($)
  if (cfg === undefined) return `Could not find your wezterm config (WEZTERM_CONFIG_FILE is not set: is this wezterm?).\n${manual}`
  if (cfg.text.includes(MARK)) return `Already set up in ${cfg.path}.`
  const returns = [...cfg.text.matchAll(/^[ \t]*return[ \t]+([A-Za-z_]\w*)[ \t]*;?[ \t]*$/gm)]
  const last = returns.at(-1)
  if (last === undefined) return `${cfg.path} does not end in a plain \`return config\`, so I am not touching it.\n${manual}`
  const eol = cfg.text.includes('\r\n') ? '\r\n' : '\n'
  const patched = cfg.text.slice(0, last.index) + hookLine(d, last[1]!).replaceAll('\n', eol) + cfg.text.slice(last.index)
  // A dotfiles symlink must stay a symlink: write to what it points at
  const real = (await $.fs.stat(cfg.path, { resolve: true })).realPath ?? cfg.path
  await $.fs.write(`${real}.twitch-emotes.bak`, cfg.text)
  await $.fs.write(real, patched)
  return `Added two lines to ${real} (backup beside it, .twitch-emotes.bak). wezterm reloads by itself: try saying KEKW.`
}

async function addEmote($: EngineInterface, args: readonly string[]): Promise<string> {
  const d = await dataDir($)
  const tool = `${$.plugin.root}/tool/emotefont.py`.replaceAll('\\', '/')
  const attempts = [['uv', 'run', '--quiet', '--script'], ['python'], ['python3']]
  let said = ''
  for (const runner of attempts) {
    try {
      const got = await $.process.run([...runner, tool, 'add', ...args], { timeoutMs: 120_000, env: { TWITCH_EMOTES_DIR: d } })
      said = (got.stdout + got.stderr).trim()
      if (got.exitCode !== 0) {
        // Missing imports: maybe the next runner has them. Anything else is the real answer
        if (said.includes('needs fonttools')) continue
        return said
      }
      await loadPack($)
      $.ui.invalidate('ui.render')
      return said
    } catch {
      // that runner is not installed
    }
  }
  return said || 'Adding emotes builds the font locally, which needs Python: install uv (https://docs.astral.sh/uv), or Python with `pip install fonttools pillow`.'
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'emotes',
      description: 'Twitch emotes: setup, on/off, list, add NAME [URL], prompt N',
      argumentHint: '[setup|on|off|list|add NAME|prompt N|any-terminal on|off]',
    })
    const d = await dataDir($)
    await $.fs.write(`${d}/wezterm.lua`, shim(d))
    if ((await loadPack($)) === undefined) {
      const failed = await downloadPack($)
      if (failed !== undefined) $.ui.log(failed)
      else await loadPack($)
    }
    if ((await $.env.get('TERM_PROGRAM')) === 'WezTerm') {
      const cfg = await wezConfig($)
      if (cfg !== undefined && !cfg.text.includes(MARK)) $.ui.log('one step left: run /emotes setup (adds two lines to your wezterm config)')
    }
    return next(e)
  })

  on('command.run', { command: 'emotes' }, async ($, e) => {
    const [verb = '', ...rest] = e.args.trim().split(/\s+/)
    switch (verb) {
      case 'setup':
        return { text: await setup($) }
      case 'add':
        if (rest.length === 0) return { text: 'Usage: /emotes add NAME [URL] [@FRAME]   (NAME as it is spelled on 7TV, case and all)' }
        return { text: await addEmote($, rest) }
      case 'list': {
        const names = Object.keys((await loadPack($))?.glyphs ?? {})
        return { text: `${names.length} emotes: ${names.join(' ')}` }
      }
      case 'prompt': {
        const n = Number(rest[0])
        if (!Number.isInteger(n) || n < 0) return { text: `Claude is told about the first ${(await $.store.get('promptEmotes')) ?? PROMPT_EMOTES} emotes. /emotes prompt N changes that, 0 tells it nothing.` }
        await $.store.set('promptEmotes', n)
        return { text: `Claude will be told about ${n} emotes from the next session on.` }
      }
      case 'any-terminal':
        hasFont = undefined
        await $.store.set('anyTerminal', rest[0] !== 'off')
        $.ui.invalidate('ui.render')
        return { text: rest[0] !== 'off' ? 'Swapping emotes in every terminal. Yours needs the font as a fallback, or you get boxes.' : 'Back to wezterm only.' }
      case 'on':
      case 'off':
      case '':
        await enabled($)
        isOn = verb === '' ? !isOn : verb === 'on'
        await $.store.set('isOn', isOn)
        $.ui.invalidate('ui.render')
        return { text: `Twitch emotes ${isOn ? 'on' : 'off'}.` }
      default:
        return { text: 'Usage: /emotes [setup | on | off | list | add NAME [URL] | prompt N | any-terminal on|off]' }
    }
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!(await enabled($))) return composed
    const stored = await $.store.get('promptEmotes')
    const n = typeof stored === 'number' ? stored : PROMPT_EMOTES
    const names = Object.keys((pack ?? (await loadPack($)))?.glyphs ?? {}).slice(0, n)
    if (names.length === 0) return composed
    const text =
      'This terminal draws Twitch emotes as inline images: an emote name written as its own word, ' +
      'spelled and cased exactly, outside of code, shows as the emote. Where your instructions or the ' +
      'conversation call for Twitch-chat tone you can use them as a chatter would (one at the end of a ' +
      'line, not a wall). Do not start using them in conversations that are not already in that tone. ' +
      `The most used ones: ${names.join(' ')}`
    return { sections: [...composed.sections, { id: 'twitch-emotes:emotes', text, scope: 'session' }] }
  })

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (e.surface !== 'terminal' || !(await enabled($))) return next(e)
    const p = pack ?? (await loadPack($))
    if (p === undefined) return next(e)
    return next({ ...e, props: { ...e.props, text: emotify(e.props.text, p.glyphs, padFor(p.cells)) } })
  })
}
