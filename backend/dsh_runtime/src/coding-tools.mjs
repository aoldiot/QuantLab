import { exec as execCallback } from 'node:child_process'
import { promisify } from 'node:util'
import { readFile, writeFile, readdir, mkdir } from 'node:fs/promises'
import path from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

const exec = promisify(execCallback)
const CWD = path.resolve(process.env.DSH_CWD || process.cwd())
const resolvePath = value => path.resolve(CWD, value || '.')
const render = (_args, value) => [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]

export const name = 'quantlab-coding-tools'
export const inject = ['tools']

const isProtectedStrategyPath = filePath => {
  const normalized = filePath.replace(/\\/g, '/')
  return normalized.includes('/app/strategies/') || normalized.includes('backend/app/strategies/')
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'read_file',
    description: 'Read a UTF-8 project file with line numbers. Use start_line/end_line for focused reads.',
    parameters: {
      path: { type: 'string', required: true },
      start_line: { type: 'number' },
      end_line: { type: 'number' },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const lines = (await readFile(resolvePath(args.path), 'utf8')).split('\n')
      const start = Math.max(1, Number(args.start_line || 1))
      const end = Math.min(lines.length, Number(args.end_line || lines.length))
      return lines.slice(start - 1, end).map((line, index) => `${start + index}\t${line}`).join('\n')
    },
  }))

  ctx.tools.register(defineTool({
    name: 'list_files',
    description: 'List project files recursively, optionally filtered by a case-insensitive substring.',
    parameters: {
      path: { type: 'string', description: 'Directory relative to project root; default .' },
      contains: { type: 'string', description: 'Optional path substring filter' },
      limit: { type: 'number', description: 'Maximum results; default 500' },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const base = resolvePath(args.path || '.')
      const entries = await readdir(base, { recursive: true })
      const needle = String(args.contains || '').toLowerCase()
      return entries.filter(item => !needle || String(item).toLowerCase().includes(needle)).slice(0, Number(args.limit || 500)).join('\n')
    },
  }))

  ctx.tools.register(defineTool({
    name: 'search_code',
    description: 'Search text in project files and return file, line and matching content.',
    parameters: {
      query: { type: 'string', required: true },
      path: { type: 'string', description: 'Directory relative to project root; default .' },
      limit: { type: 'number', description: 'Maximum matches; default 200' },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const base = resolvePath(args.path || '.')
      const files = await readdir(base, { recursive: true })
      const results = []
      for (const relative of files) {
        if (results.length >= Number(args.limit || 200)) break
        if (!/\.(py|js|mjs|ts|tsx|json|ya?ml|md)$/.test(String(relative))) continue
        try {
          const lines = (await readFile(path.join(base, String(relative)), 'utf8')).split('\n')
          lines.forEach((line, index) => {
            if (results.length < Number(args.limit || 200) && line.includes(args.query)) results.push(`${path.join(args.path || '.', String(relative))}:${index + 1}:${line}`)
          })
        } catch { /* skip directories and non-readable files */ }
      }
      return results.join('\n')
    },
  }))

  ctx.tools.register(defineTool({
    name: 'write_file',
    description: 'Write complete UTF-8 content to a project file. Prefer replace_in_file for repairs.',
    parameters: {
      path: { type: 'string', required: true },
      content: { type: 'string', required: true },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const target = resolvePath(args.path)
      if (isProtectedStrategyPath(target)) {
        return JSON.stringify({
          ok: false,
          error: '禁止通过通用文件工具直接修改已发布的正式策略目录 (backend/app/strategies/)。请使用 stage_strategy_candidate / patch_strategy_candidate 走候选区隔离和 4 级 Pre-Flight 校验流程。',
        })
      }
      await mkdir(path.dirname(target), { recursive: true })
      await writeFile(target, args.content, 'utf8')
      return JSON.stringify({ ok: true, path: target, chars: args.content.length })
    },
  }))

  ctx.tools.register(defineTool({
    name: 'replace_in_file',
    description: 'Apply one exact local replacement. The old text must occur exactly once, preventing accidental broad rewrites.',
    parameters: {
      path: { type: 'string', required: true },
      old: { type: 'string', required: true },
      new: { type: 'string', required: true },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const target = resolvePath(args.path)
      if (isProtectedStrategyPath(target)) {
        return JSON.stringify({
          ok: false,
          error: '禁止通过通用文件工具直接修改已发布的正式策略目录 (backend/app/strategies/)。请使用 patch_strategy_candidate 进行候选区定点修补并重新触发 4 级 Pre-Flight 校验。',
        })
      }
      const original = await readFile(target, 'utf8')
      const count = args.old ? original.split(args.old).length - 1 : 0
      if (count !== 1) return JSON.stringify({ ok: false, error: `old text matched ${count} times; file unchanged` })
      await writeFile(target, original.replace(args.old, args.new), 'utf8')
      return JSON.stringify({ ok: true, path: target })
    },
  }))

  ctx.tools.register(defineTool({
    name: 'run_command',
    description: 'Run a shell command in the full QuantLab repository and return complete stdout/stderr. Use for Pre-Flight, pytest, ruff, git diff and smoke backtests.',
    parameters: {
      command: { type: 'string', required: true },
      timeout_seconds: { type: 'number', description: 'Default 120, maximum 900' },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const timeout = Math.min(900, Math.max(1, Number(args.timeout_seconds || 120))) * 1000
      try {
        const { stdout, stderr } = await exec(args.command, { cwd: CWD, timeout, maxBuffer: 10 * 1024 * 1024 })
        return JSON.stringify({ ok: true, exit_code: 0, stdout, stderr })
      } catch (error) {
        return JSON.stringify({ ok: false, exit_code: error.code ?? 1, stdout: error.stdout || '', stderr: error.stderr || error.message })
      }
    },
  }))
}
