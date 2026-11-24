#!/usr/bin/env node
// SWE-style task mining from a GitHub repository. Node 22.18+ (runs TypeScript directly).
//
//   node swe/cli.ts mine https://github.com/OWNER/REPO [--max 10] [--scan 200] [--since 2021-01-01] [--out DIR] [--retry]
//   node swe/cli.ts funnel https://github.com/OWNER/REPO [--out DIR]
//   node swe/cli.ts export https://github.com/OWNER/REPO [--out DIR]
//
// --json: one JSON event per line on stdout ({"type":"progress"|"prompt"|"result"}); a "prompt"
// (the setup agent's question for a model) is answered with one line {"answer": "..."} on stdin.
// The terminal's /swe command uses this mode; without --json no model is asked.
import { createInterface } from "node:readline"
import { parseArgs } from "node:util"
import { parseRepo } from "./github.ts"
import { defaultOut, exportJsonl, funnel, mine } from "./pipeline.ts"

const USAGE = "usage: node swe/cli.ts mine|funnel|export <github-url> [--max N] [--scan N] [--since DATE] [--out DIR] [--retry] [--memory 2g] [--workers 1] [--json]"

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      max: { type: "string", default: "10" },
      scan: { type: "string", default: "200" },
      since: { type: "string" },
      out: { type: "string" },
      retry: { type: "boolean", default: false },
      memory: { type: "string", default: "2g" },
      workers: { type: "string", default: "1" },
      json: { type: "boolean", default: false },
      help: { type: "boolean", short: "h", default: false },
    },
  })
  const [command, url] = positionals
  if (values.help || !command || !url) {
    console.log(USAGE)
    return values.help ? 0 : 2
  }
  const json = values.json
  const emit = (event: object) => process.stdout.write(JSON.stringify(event) + "\n")
  const say = (line: string) => (json ? emit({ type: "progress", text: line }) : console.log(line))
  const lines = json ? createInterface({ input: process.stdin })[Symbol.asyncIterator]() : undefined
  const ask = lines
    ? async (prompt: string) => {
        emit({ type: "prompt", text: prompt })
        const next = await lines.next()
        if (next.done) throw new Error("no answer from the terminal")
        const msg = JSON.parse(next.value)
        if (msg.error) throw new Error(msg.error)
        return String(msg.answer ?? "")
      }
    : undefined
  try {
    const repo = parseRepo(url)
    const out = values.out ?? defaultOut(repo)
    let data: unknown
    if (command === "mine") {
      const max = Number(values.max), scan = Number(values.scan)
      if (!(max > 0) || !(scan > 0)) throw new Error("--max and --scan must be positive numbers")
      if (values.since && Number.isNaN(Date.parse(values.since))) throw new Error(`--since: not a date: ${values.since}`)
      if (!/^\d+[kmg]$/i.test(values.memory)) throw new Error(`--memory: like 2g or 4096m, not ${values.memory}`)
      const workers = Number(values.workers)
      if (!(workers >= 1 && workers <= 16)) throw new Error("--workers: 1 to 16")
      const r = await mine(url, { max, scan, since: values.since, out, retry: values.retry, memory: values.memory, workers, progress: say, ask })
      data = { out: r.out, made: r.made, funnel: r.funnel, store: `${r.out}/tasks` }
    } else if (command === "funnel") data = { out, funnel: funnel(out) }
    else if (command === "export") data = { out, rows: exportJsonl(out), file: `${out}/swe-bench.jsonl` }
    else throw new Error(USAGE)
    if (json) emit({ type: "result", ok: true, data })
    else console.log(JSON.stringify(data, null, 2))
    return 0
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e)
    if (json) emit({ type: "result", ok: false, error: message })
    else console.error(`error: ${message}`)
    return 1
  } finally {
    process.stdin.unref?.()
  }
}

main().then((code) => process.exit(code))
