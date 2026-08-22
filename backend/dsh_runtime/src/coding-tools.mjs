import { readFile, writeFile, readdir, mkdir, realpath, lstat } from 'node:fs/promises'
import path from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

const CWD = path.resolve(process.env.DSH_CWD || process.cwd())

const DEFAULT_IGNORED_DIRS = [
  'node_modules',
  '.venv',
  'venv',
  '__pycache__',
  '.ruff_cache',
  '.pytest_cache',
  '.git',
  '.idea',
  '.vscode',
  '.sessions',
  'frontend',
]

const DEFAULT_READABLE_PATHS = [
  'backend/app/strategies',
  'app/strategies',
  'backend/app/quant',
  'app/quant',
  'backend/app/strategy_contract.py',
  'app/strategy_contract.py',
  'backend/app/strategy_base.py',
  'app/strategy_base.py',
  'backend/app/schemas.py',
  'app/schemas.py',
  '.agents/skills',
  'docs',
]

const DEFAULT_WRITABLE_PATHS = [
  'backend/app/strategies/candidates',
  'app/strategies/candidates',
]

export const name = 'quantlab-coding-tools'
export const inject = ['tools']

const render = (_args, value) => [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]

function resolvePath(value) {
  return path.resolve(CWD, value || '.')
}

function normalizeRel(filePath) {
  const rel = path.relative(CWD, path.resolve(CWD, filePath || '.'))
  return rel.replace(/\\/g, '/').replace(/^\.\//, '')
}

function isPathAllowed(targetPath, allowedList, ignoredSet) {
  const rel = normalizeRel(targetPath)
  if (rel === '..' || rel.startsWith('../') || path.isAbsolute(rel)) {
    return false
  }

  // Blacklist check
  const segments = rel.split('/')
  for (const s of segments) {
    if (ignoredSet.has(s) || s.startsWith('.env') || s.endsWith('.pem') || s.endsWith('.key')) {
      return false
    }
  }

  if (!Array.isArray(allowedList) || allowedList.length === 0) {
    return false
  }

  for (const item of allowedList) {
    const norm = item.replace(/\\/g, '/').replace(/^\.\//, '').replace(/\/+$/, '')
    if (norm === '.' || norm === '') {
      return true
    }
    if (rel === norm || rel.startsWith(norm + '/')) {
      return true
    }
  }

  return false
}

async function realPathAllowed(targetPath, allowedList, ignoredSet, { forWrite = false } = {}) {
  if (!isPathAllowed(targetPath, allowedList, ignoredSet)) return false

  async function nearestExisting(candidate) {
    let current = candidate
    while (true) {
      try {
        await lstat(current)
        return { lexical: current, real: await realpath(current) }
      } catch (error) {
        if (error?.code !== 'ENOENT') return null
        const parent = path.dirname(current)
        if (parent === current) return null
        current = parent
      }
    }
  }

  const targetExisting = await nearestExisting(targetPath)
  if (!targetExisting || (!forWrite && targetExisting.lexical !== targetPath)) return false

  for (const item of allowedList) {
    const configuredRoot = resolvePath(item)
    if (!isPathAllowed(targetPath, [item], ignoredSet)) continue
    const rootExisting = await nearestExisting(configuredRoot)
    if (!rootExisting) continue

    // If the configured root does not exist yet, both it and the target must
    // share the same nearest real parent. This permits creation of candidates/
    // while still rejecting a symlinked parent that escapes the repository.
    if (rootExisting.lexical !== configuredRoot) {
      if (targetExisting.real === rootExisting.real) return true
      continue
    }

    const rel = path.relative(rootExisting.real, targetExisting.real)
    if (rel === '' || (rel !== '..' && !rel.startsWith('../') && !path.isAbsolute(rel))) return true
  }
  return false
}

function allowedRoots(allowedList) {
  return [...new Set(allowedList.map(resolvePath))]
}

export const __testing = { resolvePath, isPathAllowed, realPathAllowed, allowedRoots }

const isProtectedStrategyPath = filePath => {
  const normalized = filePath.replace(/\\/g, '/')
  const isStrategyDir = normalized.includes('/app/strategies/') || normalized.includes('backend/app/strategies/')
  const isCandidate = normalized.includes('/strategies/candidates/')
  return isStrategyDir && !isCandidate
}

async function* safeWalk(baseDir, ignoredSet, maxEntries = 10000) {
  let count = 0
  const queue = [baseDir]

  while (queue.length > 0) {
    if (count >= maxEntries) break
    const currentDir = queue.shift()

    let entries
    try {
      entries = await readdir(currentDir, { withFileTypes: true })
    } catch {
      continue
    }

    for (const entry of entries) {
      if (count >= maxEntries) break
      const name = entry.name

      // Skip blacklisted and hidden sensitive files/dirs
      if (ignoredSet.has(name) || name.startsWith('.env') || name.endsWith('.key') || name.endsWith('.pem')) {
        continue
      }

      const fullPath = path.join(currentDir, name)

      if (entry.isDirectory()) {
        queue.push(fullPath)
      } else if (entry.isFile()) {
        count++
        yield fullPath
      }
    }
  }
}

export function apply(ctx, config = {}) {
  const readablePaths = Array.isArray(config.readablePaths) ? config.readablePaths : DEFAULT_READABLE_PATHS
  const writablePaths = Array.isArray(config.writablePaths) ? config.writablePaths : DEFAULT_WRITABLE_PATHS
  const ignoredDirs = new Set(Array.isArray(config.ignoredDirs) ? config.ignoredDirs : DEFAULT_IGNORED_DIRS)
  const isReadOnly = writablePaths.length === 0

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
      const target = resolvePath(args.path)
      if (!await realPathAllowed(target, [...readablePaths, ...writablePaths], ignoredDirs)) {
        return `Access Denied: Path "${args.path}" is outside the allowed readable directories for this agent.`
      }

      try {
        const lines = (await readFile(target, 'utf8')).split('\n')
        const start = Math.max(1, Number(args.start_line || 1))
        const end = Math.min(lines.length, Number(args.end_line || lines.length))
        return lines.slice(start - 1, end).map((line, index) => `${start + index}\t${line}`).join('\n')
      } catch (err) {
        return `Error: ${err.message}`
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'list_files',
    description: 'List allowed project files recursively, optionally filtered by a case-insensitive substring.',
    parameters: {
      path: { type: 'string', description: 'Directory relative to project root; default .' },
      contains: { type: 'string', description: 'Optional path substring filter' },
      limit: { type: 'number', description: 'Maximum results; default 500' },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const effectiveAllowed = [...readablePaths, ...writablePaths]
      const requested = String(args.path || '.').trim()
      const bases = requested === '.' || requested === ''
        ? allowedRoots(effectiveAllowed)
        : [resolvePath(requested)]

      const needle = String(args.contains || '').toLowerCase()
      const limit = Math.min(500, Math.max(1, Number(args.limit || 500)))
      const matched = []

      for (const base of bases) {
        if (!await realPathAllowed(base, effectiveAllowed, ignoredDirs)) continue
        for await (const fullPath of safeWalk(base, ignoredDirs)) {
          if (!await realPathAllowed(fullPath, effectiveAllowed, ignoredDirs)) continue
          const rel = normalizeRel(fullPath)
          if (!needle || rel.toLowerCase().includes(needle) || path.basename(rel).toLowerCase().includes(needle)) {
            matched.push(rel)
            if (matched.length >= limit) break
          }
        }
        if (matched.length >= limit) break
      }

      return matched.length ? matched.join('\n') : 'No matching files found.'
    },
  }))

  ctx.tools.register(defineTool({
    name: 'search_code',
    description: 'Search text in allowed project files and return file, line and matching content.',
    parameters: {
      query: { type: 'string', required: true },
      path: { type: 'string', description: 'Directory relative to project root; default .' },
      limit: { type: 'number', description: 'Maximum matches; default 200' },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      const effectiveAllowed = [...readablePaths, ...writablePaths]
      const requested = String(args.path || '.').trim()
      const bases = requested === '.' || requested === ''
        ? allowedRoots(effectiveAllowed)
        : [resolvePath(requested)]

      const limit = Math.min(200, Math.max(1, Number(args.limit || 200)))
      const results = []

      for (const base of bases) {
        if (!await realPathAllowed(base, effectiveAllowed, ignoredDirs)) continue
        for await (const fullPath of safeWalk(base, ignoredDirs)) {
          if (results.length >= limit) break
          if (!/\.(py|js|mjs|ts|tsx|json|ya?ml|md)$/.test(fullPath)) continue
          if (!await realPathAllowed(fullPath, effectiveAllowed, ignoredDirs)) continue

          try {
            const content = await readFile(fullPath, 'utf8')
            const lines = content.split('\n')
            const rel = normalizeRel(fullPath)
            for (let index = 0; index < lines.length; index++) {
              if (results.length >= limit) break
              if (lines[index].includes(args.query)) {
                results.push(`${rel}:${index + 1}:${lines[index]}`)
              }
            }
          } catch {
            // ignore unreadable files
          }
        }
        if (results.length >= limit) break
      }

      return results.length ? results.join('\n') : 'No matching code found.'
    },
  }))

  if (!isReadOnly) ctx.tools.register(defineTool({
    name: 'write_file',
    description: 'Write complete UTF-8 content to a project file within writable directories. Prefer replace_in_file for repairs.',
    parameters: {
      path: { type: 'string', required: true },
      content: { type: 'string', required: true },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      if (isReadOnly) {
        return JSON.stringify({
          ok: false,
          error: 'Access Denied: This agent is strictly read-only and is not permitted to write files.',
        })
      }

      const target = resolvePath(args.path)
      if (!await realPathAllowed(target, writablePaths, ignoredDirs, { forWrite: true })) {
        return JSON.stringify({
          ok: false,
          error: `Access Denied: Path "${args.path}" is outside the writable directory whitelist for this agent.`,
        })
      }

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

  if (!isReadOnly) ctx.tools.register(defineTool({
    name: 'replace_in_file',
    description: 'Apply one exact local replacement in writable directories. The old text must occur exactly once, preventing accidental broad rewrites.',
    parameters: {
      path: { type: 'string', required: true },
      old: { type: 'string', required: true },
      new: { type: 'string', required: true },
    },
    output: { schema: { type: 'string' }, render },
    async execute(args) {
      if (isReadOnly) {
        return JSON.stringify({
          ok: false,
          error: 'Access Denied: This agent is strictly read-only and is not permitted to modify files.',
        })
      }

      const target = resolvePath(args.path)
      if (!await realPathAllowed(target, writablePaths, ignoredDirs, { forWrite: true })) {
        return JSON.stringify({
          ok: false,
          error: `Access Denied: Path "${args.path}" is outside the writable directory whitelist for this agent.`,
        })
      }

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

}
