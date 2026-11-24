// Task Factory commands inside the terminal:
//   /setup          pin this project and build its test environment (once)
//   /generate-task  draft a task with the selected model and validate it
//   /tasks          list tasks; show, run, approve or delete one
//   /run-task       solve tasks with a model in Docker (live progress)
//   /results        runs -> attempts -> verdict, agent session, diff, test output
//   /benchmark      tasks x models: who solved what, difficulty, failures, time and tokens
//   /solutions      this session's AI solutions as files changed (graded diff; r = raw agent diff)
//   /rerun          run this session's tasks on another model, here or in a copy of the session
//   /swe            mine SWE-bench style tasks from a GitHub repository (swe/cli.ts, TypeScript)
//   /swe-tasks      the mined tasks: issue, reference patch, hidden tests; run, approve, delete
// The session sidebar shows the project's state: tasks left, AI performance, per model,
// what this session generated, and the next step.
// The work happens in Python (`python -m factory.terminal_bridge`, one process per command);
// /solutions and the session links are read and written here (solutions.ts), from the workdir.
import { spawn } from "node:child_process"
import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { createInterface } from "node:readline"
import { fileURLToPath } from "node:url"
import { TextAttributes, type ScrollBoxRenderable } from "@opentui/core"
import { useTerminalDimensions } from "@opentui/solid"
import { createEffect, createSignal, For, onCleanup, Show } from "solid-js"
import { Plugin } from "@task-factory/plugin/tui"
import {
  type Attempt,
  attemptDiff,
  copyTasks,
  filesChanged,
  linkRuns,
  newRuns,
  readAttempts,
  runIDs,
  sessionAttempts,
  sessionTaskIDs,
  taskInstruction,
  taskTitle,
  verdictOf,
  webUse,
} from "./solutions.ts"
import { listOf, readPart, sweRoot, sweStores, type SweTask, sweTasks } from "./swe_store.ts"

const SWE_CLI = fileURLToPath(new URL("../../swe/cli.ts", import.meta.url))

type Tone = "base" | "muted" | "faint" | "ok" | "bad" | "warn" | "info" | "add" | "del" | "hunk"
type Seg = { text: string; tone?: Tone; bold?: boolean }
type Section = { heading: string; text?: string; lines?: Seg[][] }
type Action = { key: string; title: string; run: () => void }
type View = {
  title: string
  sections: Section[]
  log: string[]
  running: boolean
  footer?: string
  ok?: boolean // colours the footer and the title once the command has finished
  started?: number
  actions?: Action[]
}

const color = (theme: any, tone: Tone = "base") =>
  ({
    base: theme.text.base,
    muted: theme.text.muted,
    faint: theme.text.faint ?? theme.text.muted,
    ok: theme.text.feedback.success.base,
    bad: theme.text.feedback.error.base,
    warn: theme.text.feedback.warning.base,
    info: theme.text.feedback.info.base,
    add: theme.diff.text.added,
    del: theme.diff.text.removed,
    hunk: theme.diff.text.hunkHeader,
  })[tone]

// One tone per line, from what the line says. `kind` comes from the section heading.
function toneOf(line: string, kind: "diff" | "agent" | "plain" | "text"): Seg {
  if (kind === "text") return { text: line, tone: line.startsWith("#") ? "base" : "muted", bold: line.startsWith("#") }
  if (kind === "diff") {
    if (/^(\+\+\+|---) /.test(line)) return { text: line, tone: "faint" }
    if (line.startsWith("@@")) return { text: line, tone: "hunk" }
    if (line.startsWith("+")) return { text: line, tone: "add" }
    if (line.startsWith("-")) return { text: line, tone: "del" }
    return { text: line, tone: "muted" }
  }
  if (kind === "agent") {
    if (line.startsWith("● ")) return { text: line, tone: "base", bold: true }
    if (line.startsWith("✓ ")) return { text: line, tone: "ok" }
    if (line.startsWith("✗ ") || line.startsWith("ERROR")) return { text: line, tone: "bad" }
    if (line.startsWith("    ")) return { text: line, tone: "faint" }
    return { text: line, tone: "muted" }
  }
  if (/^\s*G\d\s+fail|✗|\bERROR\b|\berror:|rejected|NOT added|REJECT|\bFAILED\b|[1-9]\d* failed|timed out|stopped early|verdict: (?!RESOLVED)/.test(line))
    return { text: line, tone: "bad" }
  if (/⚠|WARNING|warning|kept open|not attempted|HEAD moved|still working/i.test(line)) return { text: line, tone: "warn" }
  if (/^\s*G\d\s+pass|✓|RESOLVED|\bresolved\b|\bvalid\b|^ready:|new branch|environment ok|\d+ passed|saved as pending|^Done\.?$/.test(line))
    return { text: line, tone: "ok" }
  return { text: line, tone: "muted" }
}

function sectionKind(heading: string): "diff" | "agent" | "plain" | "text" {
  if (/instruction|history|how to read/i.test(heading)) return "text"
  if (/diff|change/i.test(heading)) return "diff"
  if (/session/i.test(heading)) return "agent"
  return "plain"
}

function Line(props: { theme: any; segs: Seg[] }) {
  return (
    <text fg={props.theme.text.muted} wrapMode="word">
      <For each={props.segs}>
        {(seg) => (
          <span style={{ fg: color(props.theme, seg.tone), attributes: seg.bold ? TextAttributes.BOLD : undefined }}>
            {seg.text}
          </span>
        )}
      </For>
    </text>
  )
}
type Result = { ok: boolean; text: string; error?: string; data?: any }

const PAGE = "task-factory"

type Hooks = { progress?: (line: string) => void; prompt?: (text: string) => Promise<string> }

function callBridge(python: string, project: string, request: Record<string, unknown>, hooks: Hooks = {}): Promise<Result> {
  return callProcess(python, ["-u", "-m", "factory.terminal_bridge"], project, request, hooks)
}

// A child process speaking JSON lines: {"type": "progress" | "prompt" | "result"}; other lines are
// progress. `request` (if any) is its first stdin line. With `plain`, the exit code is the result.
function callProcess(
  cmd: string,
  args: string[],
  cwd: string,
  request: Record<string, unknown> | undefined,
  hooks: Hooks = {},
  plain = false,
): Promise<Result> {
  return new Promise((resolveCall, rejectCall) => {
    const child = spawn(cmd, args, {
      cwd,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, NO_COLOR: "1", TERM: "dumb", COLUMNS: "110" },
    })
    let diagnostics = ""
    let result: Result | undefined
    child.stderr.setEncoding("utf8")
    child.stderr.on("data", (data) => (diagnostics = (diagnostics + data).slice(-3500)))
    child.stdin.on("error", () => {})
    const send = (value: unknown) => child.stdin.write(JSON.stringify(value) + "\n")
    const lines = createInterface({ input: child.stdout })
    lines.on("line", async (line) => {
      let event: any
      try {
        event = JSON.parse(line)
      } catch {
        hooks.progress?.(line)
        return
      }
      if (event.type === "progress") hooks.progress?.(event.text)
      else if (event.type === "prompt") {
        try {
          send({ answer: hooks.prompt ? await hooks.prompt(event.text) : "" })
        } catch (error) {
          send({ error: error instanceof Error ? error.message : JSON.stringify(error) })
        }
      } else if (event.type === "result") {
        result = { ok: event.ok !== false, text: event.text ?? "", error: event.error, data: event.data }
        child.stdin.end()
      }
    })
    child.on("error", (error) => rejectCall(error))
    child.on("close", (code) => {
      lines.close()
      if (result) resolveCall(result)
      else if (plain) resolveCall({ ok: code === 0, text: "", error: code === 0 ? undefined : diagnostics.trim().split("\n").pop() || `exit ${code}` })
      else rejectCall(new Error(diagnostics.trim() || `Task Factory exited with status ${code}`))
    })
    if (request) send(request)
    else if (plain) child.stdin.end()
  })
}

function when(runID: string) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(runID)
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : runID
}

function Page(props: { context: any; view: () => View; onClose: () => void }) {
  const theme = props.context.theme
  const dimensions = useTerminalDimensions()
  let scroll: ScrollBoxRenderable | undefined
  const page = () => Math.max(3, dimensions().height - 6)
  const [now, setNow] = createSignal(Date.now())
  const tick = setInterval(() => props.view().running && setNow(Date.now()), 1000)
  onCleanup(() => clearInterval(tick))
  const elapsed = () => {
    const start = props.view().started
    if (!start) return ""
    const s = Math.max(0, Math.round((now() - start) / 1000))
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
  }
  props.context.keymap.layer(() => ({
    commands: [
      { bind: "escape", title: "back", run: props.onClose },
      { bind: "up", title: "Scroll up", run: () => scroll?.scrollBy(-1) },
      { bind: "down", title: "Scroll down", run: () => scroll?.scrollBy(1) },
      { bind: "pageup", title: "Previous page", run: () => scroll?.scrollBy(-page()) },
      { bind: "pagedown", title: "Next page", run: () => scroll?.scrollBy(page()) },
      { bind: "home", title: "Top", run: () => scroll?.scrollTo(0) },
      { bind: "end", title: "Bottom", run: () => scroll?.scrollTo(Infinity) },
      ...(props.view().actions ?? []).map((a) => ({ bind: a.key, title: a.title, run: a.run })),
    ],
  }))
  const titleTone = (): Tone => {
    const v = props.view()
    if (v.running) return "warn"
    if (v.ok === false || / · (?!RESOLVED)[A-Z ]+$/.test(v.title)) return "bad"
    if (v.ok === true || / · RESOLVED$/.test(v.title)) return "ok"
    return "base"
  }
  return (
    <box width="100%" height="100%" backgroundColor={theme.background.base} padding={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
        <text fg={theme.text.base} attributes={TextAttributes.BOLD}>
          Task Factory · <span style={{ fg: color(theme, titleTone()) }}>{props.view().title}</span>
        </text>
        <text fg={theme.text.muted}>
          <Show when={props.view().running}>
            <span style={{ fg: color(theme, "warn") }}>● working {elapsed()}{"  "}</span>
          </Show>
          {(props.view().actions ?? []).map((a) => `${a.key} ${a.title} · `).join("")}esc back · ↑↓ pgup pgdn scroll
        </text>
      </box>
      <scrollbox
        ref={(value: ScrollBoxRenderable) => (scroll = value)}
        height={Math.max(3, dimensions().height - 4)}
        flexShrink={0}
        stickyScroll={props.view().running}
        stickyStart="bottom"
      >
        <box gap={1}>
          <For each={props.view().sections}>
            {(section) => (
              <box>
                <text fg={color(theme, "info")} attributes={TextAttributes.BOLD}>
                  {section.heading}
                </text>
                <For each={section.lines ?? (section.text ?? "").split("\n").map((l) => [toneOf(l, sectionKind(section.heading))])}>
                  {(segs) => <Line theme={theme} segs={segs} />}
                </For>
              </box>
            )}
          </For>
          <Show when={props.view().log.length > 0}>
            <box>
              <For each={props.view().log}>{(line) => <Line theme={theme} segs={[toneOf(line, "plain")]} />}</For>
            </box>
          </Show>
          <Show when={props.view().footer}>
            <text fg={color(theme, props.view().ok === false ? "bad" : "ok")} attributes={TextAttributes.BOLD} wrapMode="word">
              {props.view().footer}
            </text>
          </Show>
        </box>
      </scrollbox>
    </box>
  )
}

const pct = (n: number, d: number) => (d ? `${Math.round((100 * n) / d)}%` : "–")
const short = (model: string) => model.replace(/^[^/]+\//, "").replace(/-free$/, "").slice(0, 22)
const fit = (text: string, n = 34) => (text.length > n ? text.slice(0, n - 1) + "…" : text)

function Sidebar(props: {
  context: any
  overview: () => any
  running: () => string | undefined
  solutions?: () => Attempt[] | undefined
}) {
  const theme = props.context.theme
  const head = (text: string) => (
    <text fg={theme.text.base} attributes={TextAttributes.BOLD}>
      {text}
    </text>
  )
  const row = (segs: Seg[]) => <Line theme={theme} segs={segs} />
  const rateTone = (n: number, d: number): Tone => (!d ? "faint" : n / d >= 0.5 ? "ok" : n ? "warn" : "bad")
  return (
    <Show when={props.overview()}>
      {(o: () => any) => (
        <box>
          {head("Task Factory")}
          {row(
            !o().project.repo
              ? [{ text: "not set up · /setup", tone: "warn" }]
              : [
                  { text: `${o().project.repo} @ ${String(o().project.commit).slice(0, 7)} `, tone: "muted" },
                  o().project.ready
                    ? o().project.head_moved
                      ? { text: "⚠ HEAD moved", tone: "warn" }
                      : { text: "✓", tone: "ok" }
                    : { text: "not ready", tone: "bad" },
                ],
          )}
          <Show when={o().tasks.total > 0}>
            {head(`Tasks (${o().tasks.total})`)}
            {row([
              { text: `✓ ${o().tasks.approved} approved`, tone: o().tasks.approved ? "ok" : "faint" },
              { text: " · ", tone: "faint" },
              { text: `⏳ ${o().tasks.pending} review`, tone: o().tasks.pending ? "warn" : "faint" },
              { text: " · ", tone: "faint" },
              { text: `✗ ${o().tasks.deleted}`, tone: "faint" },
            ])}
            <Show when={o().tasks.not_run}>{row([{ text: `not run yet  ${o().tasks.not_run}`, tone: "warn" }])}</Show>
            <Show when={o().tasks.never_solved}>
              {row([{ text: `never solved ${o().tasks.never_solved} ⚠ check solvable`, tone: "bad" }])}
            </Show>
            <Show when={o().tasks.open_assignments}>
              {row([{ text: `assigned     ${o().tasks.open_assignments} open`, tone: "muted" }])}
            </Show>
          </Show>
          <Show when={o().performance.attempts + o().performance.agent_errors > 0}>
            {head("AI performance")}
            {row([
              { text: "resolved ", tone: "muted" },
              {
                text: `${pct(o().performance.resolved, o().performance.attempts)}`,
                tone: rateTone(o().performance.resolved, o().performance.attempts),
                bold: true,
              },
              { text: ` (${o().performance.resolved}/${o().performance.attempts} attempts)`, tone: "muted" },
            ])}
            {row([
              { text: "solved by ", tone: "muted" },
              { text: `all ${o().performance.difficulty.all}`, tone: o().performance.difficulty.all ? "ok" : "faint" },
              { text: " · ", tone: "faint" },
              { text: `some ${o().performance.difficulty.some}`, tone: o().performance.difficulty.some ? "warn" : "faint" },
              { text: " · ", tone: "faint" },
              { text: `none ${o().performance.difficulty.none}`, tone: o().performance.difficulty.none ? "bad" : "faint" },
            ])}
            <For each={Object.entries(o().performance.failures)}>
              {([k, v]: [string, any]) => row([{ text: `${k} ${v}`, tone: k === "agent_error" ? "warn" : "bad" }])}
            </For>
          </Show>
          <Show when={o().models.length}>
            {head("By model")}
            <For each={o().models}>
              {(m: any) =>
                row([
                  { text: fit(short(m.model), 20).padEnd(21), tone: "muted" },
                  { text: `${m.resolved}/${m.attempts}`, tone: rateTone(m.resolved, m.attempts), bold: true },
                  {
                    text:
                      (m.mean_time_s != null ? ` ${Math.round(m.mean_time_s)}s` : "") +
                      (m.mean_tokens != null ? ` ${Math.round(m.mean_tokens / 1000)}k` : ""),
                    tone: "faint",
                  },
                  ...(m.agent_errors ? [{ text: ` ⚠${m.agent_errors}`, tone: "warn" as Tone }] : []),
                ])
              }
            </For>
          </Show>
          <Show when={o().session.made}>
            {head("This session")}
            {row([{ text: `${o().session.made} generated · ${o().session.valid} valid`, tone: "muted" }])}
            <For each={o().session.items}>
              {(g: any) =>
                g.verdict === "valid"
                  ? row([{ text: "✓ " + fit(g.title ?? g.task_id ?? g.request, 32), tone: "ok" }])
                  : row([
                      { text: "✗ " + fit(String(g.request), 20), tone: "bad" },
                      { text: ` ${g.reject_code ?? g.verdict}`, tone: "faint" },
                    ])
              }
            </For>
          </Show>
          <Show when={props.solutions?.()?.length}>
            {head("AI solutions (this session)")}
            {row([
              { text: `✓ ${props.solutions!()!.filter((a) => a.resolved).length} resolved`, tone: "ok" },
              { text: " · ", tone: "faint" },
              { text: `✗ ${props.solutions!()!.filter((a) => !a.resolved && a.category !== "agent_error").length}`, tone: "bad" },
              ...(props.solutions!()!.some((a) => a.category === "agent_error")
                ? [{ text: ` · ⚠ ${props.solutions!()!.filter((a) => a.category === "agent_error").length} agent error`, tone: "warn" as Tone }]
                : []),
              { text: " · /solutions", tone: "info" },
            ])}
          </Show>
          <Show when={props.running()}>{row([{ text: `● ${fit(String(props.running()), 32)}`, tone: "warn" }])}</Show>
          {row([
            { text: "Next: ", tone: "info", bold: true },
            { text: o().next, tone: "info" },
          ])}
        </box>
      )}
    </Show>
  )
}

export default Plugin.define({
  id: "task-factory",
  setup(context) {
    const python = context.options.python
    process.env.TASK_FACTORY_PYTHON ??= python // the SWE miner writes MongoDB records through it
    // Session links are files here; with TASK_FACTORY_MONGO_URI they also go to MongoDB (dual write).
    const syncRecords = (folder: string) => {
      if (process.env.TASK_FACTORY_MONGO_URI)
        callProcess(python, ["-m", "factory.records", "sync", folder], folder, undefined, {}, true).catch(() => {})
    }
    const [view, setView] = createSignal<View>({ title: "", sections: [], log: [], running: false })
    const [previous, setPrevious] = createSignal<any>({ type: "home" })
    const back: View[] = [] // pages opened from another Task Factory page; esc returns to them
    let busy: string | undefined // the command running in the live view, if any
    const [running, setRunning] = createSignal<string | undefined>()
    const [version, setVersion] = createSignal(0) // bumped after every command, so the sidebar refreshes
    const refresh = () => setVersion((v) => v + 1)
    const sessionID = () => {
      const route = context.ui.router.current()
      return route.type === "session" ? route.sessionID : undefined
    }
    // The session a command belongs to, also while a Task Factory page is open on top of it.
    const sessionHere = (): string | undefined => {
      const route = context.ui.router.current()
      if (route.type === "session") return route.sessionID
      const before = previous()
      return route.type === "plugin" && route.name === PAGE && before?.type === "session" ? before.sessionID : undefined
    }
    const workdirs = new Map<string, string>()
    const workdirOf = async (dir: string): Promise<string> => {
      if (!workdirs.has(dir)) {
        const st = await callBridge(python, dir, { op: "status" })
        if (!st.ok || !st.data?.workdir) throw new Error(st.error ?? "cannot find this project's Task Factory folder")
        workdirs.set(dir, st.data.workdir)
      }
      return workdirs.get(dir)!
    }

    context.ui.router.register({
      name: PAGE,
      render: () => (
        <Page
          context={context}
          view={view}
          onClose={() => {
            const last = back.pop()
            if (last) setView(last)
            else context.ui.router.navigate(previous())
          }}
        />
      ),
    })

    const project = () => {
      const route = context.ui.router.current()
      const session = route.type === "session" ? context.data.session.get(route.sessionID) : undefined
      return (session?.location ?? context.location ?? context.data.location.default()).directory
    }
    const open = (next: View, keepBack = false) => {
      const current = context.ui.router.current()
      const onPage = current.type === "plugin" && current.name === PAGE
      if (!onPage) {
        setPrevious({ ...current })
        back.length = 0
      } else if (keepBack && !view().running) back.push(view())
      else if (!keepBack) back.length = 0
      setView(next)
      context.ui.dialog.clear()
      context.ui.router.navigate({ type: "plugin", name: PAGE })
    }
    const toastError = (error: unknown) =>
      context.ui.toast.show({ message: error instanceof Error ? error.message : String(error), variant: "error" })
    const call = (request: Record<string, unknown>) => callBridge(python, project(), request)
    const data = async (request: Record<string, unknown>) => {
      const result = await call(request)
      if (!result.ok) throw new Error(result.error ?? result.text)
      if (["approve", "delete"].includes(String(request.op))) refresh()
      return result.data
    }

    // The sidebar: one overview per session, reloaded after each command (and every 20 s while one runs).
    context.ui.slot({
      append: "sidebar.content",
      render: (props: { sessionID: string }) => {
        const [overview, setOverview] = createSignal<any>()
        const [solutions, setSolutions] = createSignal<Attempt[]>()
        const load = () => {
          const dir = project()
          if (dir) callBridge(python, dir, { op: "overview", session_id: props.sessionID })
            .then((r) => r.ok && setOverview(r.data))
            .catch(() => {})
          if (dir) sessionSolutions(props.sessionID)
            .then((list) => setSolutions(list.map((x) => x.a)))
            .catch(() => {})
        }
        createEffect(() => {
          version()
          load()
        })
        const timer = setInterval(() => running() && load(), 20_000)
        onCleanup(() => clearInterval(timer))
        return <Sidebar context={context} overview={overview} running={running} solutions={solutions} />
      },
    })

    // Ask the model inside a real session, so the exchange is visible and Zen's free models work.
    // The session may answer but not act: every tool is denied, so it cannot touch the repo.
    const askInSession = async (genSession: string, prompt: string): Promise<string> => {
      await context.client.session.prompt({ sessionID: genSession, text: prompt })
      for (let tries = 0; tries < 600; tries++) {
        await context.client.experimental.session.wait({ sessionID: genSession })
        const page = await context.client.message.list({ sessionID: genSession, limit: 30, order: "desc" })
        const newest: any[] = []
        for (const m of page.data as any[]) {
          if (m.type === "user") break
          if (m.type === "assistant") newest.unshift(m)
        }
        const last = newest[newest.length - 1]
        if (!last || !last.time?.completed) {
          await new Promise((r) => setTimeout(r, 500))
          continue
        }
        if (last.error) throw new Error(last.error.message ?? JSON.stringify(last.error))
        // The answer is the last message with text; earlier ones are the model reading files first.
        const texts = newest
          .map((m) => m.content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n"))
          .filter((t) => t.trim())
        return texts[texts.length - 1] ?? ""
      }
      throw new Error("the model did not answer")
    }

    // A long command shown live: progress lines stream into the page.
    const live = async (title: string, request: Record<string, unknown>, extra: Parameters<typeof callBridge>[3] = {}) => {
      if (busy) {
        open(view())
        context.ui.toast.show({ message: `Still working on: ${busy}`, variant: "warning" })
        return undefined
      }
      busy = title
      setRunning(title)
      open({ title, sections: [], log: [], running: true, started: Date.now() })
      try {
        const result = await callBridge(python, project(), request, {
          ...extra,
          progress: (line) => setView((v) => ({ ...v, log: [...v.log, line] })),
        })
        setView((v) => ({ ...v, running: false, ok: result.ok, footer: result.ok ? "Done." : result.error ?? result.text }))
        context.attention?.notify?.({ title: "Task Factory", message: `${title}: ${result.ok ? "done" : "failed"}` })
        return result
      } catch (error) {
        setView((v) => ({ ...v, running: false, ok: false, footer: String(error instanceof Error ? error.message : error) }))
        return undefined
      } finally {
        busy = undefined
        setRunning(undefined)
        refresh()
      }
    }

    // Like live(), for any work that reports progress lines (the SWE miner, the harness CLI).
    const liveRun = async (title: string, work: (progress: (line: string) => void) => Promise<Result>) => {
      if (busy) {
        open(view())
        context.ui.toast.show({ message: `Still working on: ${busy}`, variant: "warning" })
        return undefined
      }
      busy = title
      setRunning(title)
      open({ title, sections: [], log: [], running: true, started: Date.now() })
      try {
        const result = await work((line) => setView((v) => ({ ...v, log: [...v.log, line] })))
        setView((v) => ({ ...v, running: false, ok: result.ok, footer: result.ok ? "Done." : result.error ?? result.text }))
        context.attention?.notify?.({ title: "Task Factory", message: `${title}: ${result.ok ? "done" : "failed"}` })
        return result
      } catch (error) {
        setView((v) => ({ ...v, running: false, ok: false, footer: String(error instanceof Error ? error.message : error) }))
        return undefined
      } finally {
        busy = undefined
        setRunning(undefined)
        refresh()
      }
    }

    // Every attempt this session ran: the project's runs and runs of SWE tasks (each store is a workdir).
    const sessionSolutions = async (session: string): Promise<{ wd: string; a: Attempt }[]> => {
      const out: { wd: string; a: Attempt }[] = []
      const wd = await workdirOf(project()).catch(() => undefined)
      if (wd) out.push(...sessionAttempts(wd, session).map((a) => ({ wd, a })))
      for (const store of sweStores()) out.push(...sessionAttempts(store, session).map((a) => ({ wd: store, a })))
      return out.sort((x, y) => y.a.run_id.localeCompare(x.a.run_id))
    }

    const currentModel = () => {
      const selection = context.ui.model.current()
      return selection ? `${selection.providerID}/${selection.modelID}` : undefined
    }
    const pickModel = async (): Promise<string | undefined> => {
      const current = currentModel()
      const models = (context.data.location.model.list() ?? []).filter((m: any) => m.status !== "deprecated")
      const options = models.map((m: any) => ({
        title: `${m.providerID}/${m.id}`,
        description: m.name,
        value: `${m.providerID}/${m.id}`,
        footer: `${m.providerID}/${m.id}` === current ? "selected" : undefined,
      }))
      if (current && !options.some((o: any) => o.value === current)) options.unshift({ title: current, value: current, footer: "selected" })
      if (!options.length) {
        context.ui.toast.show({ message: "Connect a provider with /connect, then choose a model with /models.", variant: "warning" })
        return undefined
      }
      options.sort((a: any, b: any) => (a.value === current ? -1 : b.value === current ? 1 : 0))
      return context.ui.dialog.select({ title: "Solve with which model?", placeholder: "Search models", options, current })
    }

    const showDoc = (doc: { title: string; sections: Section[] }, keepBack = false) =>
      open({ ...doc, log: [], running: false }, keepBack)

    const mark = (c: any) =>
      !c ? "–" : c.attempts === 0 ? "err" : c.resolved === c.attempts ? `✓${c.attempts > 1 ? c.attempts : ""}` : c.resolved ? `${c.resolved}/${c.attempts}` : "✗"
    const showBenchmark = (b: any) => {
      const models: string[] = b.models.map((m: any) => m.model)
      const widths = models.map((m) => Math.max(6, short(m).length) + 2)
      const width = Math.min(44, Math.max(10, ...b.matrix.map((r: any) => r.task_id.length)))
      const cellTone = (c: any): Tone =>
        !c ? "faint" : c.attempts === 0 ? "warn" : c.resolved === c.attempts ? "ok" : c.resolved ? "warn" : "bad"
      const diffTone = (d?: string): Tone => (d === "all" ? "ok" : d === "some" ? "warn" : d === "none" ? "bad" : "faint")
      const header: Seg[] = [
        { text: "task".padEnd(width + 2), tone: "base", bold: true },
        ...models.map((m, i) => ({ text: short(m).padEnd(widths[i]), tone: "base" as Tone, bold: true })),
        { text: "solved by", tone: "base", bold: true },
      ]
      const rows: Seg[][] = b.matrix.map((r: any) => [
        { text: r.task_id.slice(0, width).padEnd(width + 2), tone: r.status === "deleted" ? "faint" : "base" },
        ...models.map((m, i) => ({ text: mark(r.cells[m]).padEnd(widths[i]), tone: cellTone(r.cells[m]) })),
        {
          text: r.difficulty ? (r.difficulty === "none" ? "none ⚠" : r.difficulty) : "not run",
          tone: diffTone(r.difficulty),
        },
      ])
      const perModel: Seg[][] = b.models.map((m: any) => [
        { text: `${short(m.model).padEnd(22)}`, tone: "base", bold: true },
        {
          text: `${m.resolved}/${m.attempts} resolved (${pct(m.resolved, m.attempts)})`,
          tone: m.attempts === 0 ? "faint" : m.resolved / m.attempts >= 0.5 ? "ok" : m.resolved ? "warn" : "bad",
        },
        {
          text:
            ` on ${m.tasks} task(s)` +
            (m.mean_time_s != null ? ` · ${m.mean_time_s}s avg` : "") +
            (m.mean_tokens != null ? ` · ${m.mean_tokens.toLocaleString()} tokens avg` : ""),
          tone: "muted",
        },
        ...(m.agent_errors ? [{ text: ` · ${m.agent_errors} agent error(s), not counted`, tone: "warn" as Tone }] : []),
      ])
      const failures = Object.entries(b.failures).map(([k, v]) => `${k}: ${v}`)
      const cells = b.matrix.flatMap((r: any) =>
        models.filter((m) => r.cells[m]).map((m) => ({ task: r.task_id, model: m, cell: r.cells[m] })),
      )
      showDoc({
        title: "benchmark",
        sections: [
          { heading: "Who solved what (✓ all attempts · n/m some · ✗ none · – not run · err agent error)", lines: [header, ...rows] },
          {
            heading: "By model (agent errors such as quota are not counted as failures)",
            lines: perModel.length ? perModel : [[{ text: "(no runs yet)", tone: "faint" }]],
          },
          {
            heading: "Why attempts failed",
            lines: failures.length ? failures.map((f) => [{ text: f, tone: "bad" as Tone }]) : [[{ text: "(no failures)", tone: "ok" }]],
          },
          {
            heading: "How to read it",
            text:
              "solved by: the design doc's difficulty tag over all graded attempts — all, some or none. " +
              "A task no attempt solves (none ⚠) needs a human check: it may be unsolvable. Press o to open an attempt.",
          },
        ],
      })
      setView((v) => ({
        ...v,
        actions: [
          {
            key: "o",
            title: "open attempt",
            run: async () => {
              const pick = await context.ui.dialog.select({
                title: "Open which attempt?",
                placeholder: "Search",
                options: cells.map((c: any) => ({
                  title: `${mark(c.cell)}  ${c.task}`,
                  description: `${c.model} · last: ${c.cell.category}`,
                  value: c,
                })),
              })
              if (pick)
                showDoc(
                  await data({ op: "attempt", run_id: pick.cell.run_id, task_id: pick.task, attempt: pick.cell.attempt }),
                  true,
                )
            },
          },
        ],
      }))
    }

    // `forSession`: the session the run's attempts belong to (/solutions shows them only there).
    const runTasks = async (taskIDs: string[], chosen?: string, forSession = sessionHere()) => {
      const dir = project()
      const model = chosen ?? (await pickModel())
      if (!model) return
      const wd = await workdirOf(dir).catch(() => undefined)
      const before = new Set(wd ? runIDs(wd) : [])
      const result = await live(`solving ${taskIDs.length} task(s) with ${model}`, { op: "run", task_ids: taskIDs, model })
      if (wd && forSession) {
        linkRuns(wd, forSession, newRuns(wd, before, taskIDs), model, taskIDs)
        syncRecords(wd)
        refresh()
      }
      if (result?.ok && result.data?.run_id) await showRun(result.data.run_id)
    }

    // One attempt as files changed: the graded diff, or with `raw` everything the agent changed.
    const showSolution = (wd: string, a: Attempt, raw = false, keepBack = false) => {
      const diff = attemptDiff(wd, a, raw)
      const files = filesChanged(diff)
      const added = files.reduce((n, f) => n + f.added, 0)
      const removed = files.reduce((n, f) => n + f.removed, 0)
      const title = taskTitle(taskInstruction(wd, a.task_id), a.task_id)
      const mark = { added: "A", deleted: "D", renamed: "R", binary: "B", modified: "M" }
      const summary: Seg[][] = [
        [{ text: `verdict: ${verdictOf(a)}`, tone: a.resolved ? "ok" : "bad", bold: true }],
        [{ text: `model: ${a.agent ?? "?"} · ${a.model ?? "?"} · attempt ${a.attempt} · run ${when(a.run_id)}`, tone: "muted" }],
        [{
          text: `hidden tests: ${a.f2p_passed}/${a.f2p_total} new tests pass · ` +
            (a.p2p_failed.length ? `${a.p2p_failed.length} existing test(s) broke` : "no existing test broke"),
          tone: a.f2p_passed === a.f2p_total && !a.p2p_failed.length ? "ok" : "bad",
        }],
        ...(a.tests_tampered.length
          ? [[{ text: `⚠ test/config files the agent changed (undone before grading): ${a.tests_tampered.join(", ")}`, tone: "warn" as Tone }]]
          : []),
        ...(a.branch ? [[{ text: `branch: ${a.branch}`, tone: "muted" as Tone }]] : []),
        // Runs with the egress allowlist record what was refused: the agent reached only its model.
        ...(a.egress_blocked
          ? [[{ text: a.egress_blocked.length ? `🛡 network: only the model API; refused ${a.egress_blocked.join(", ")}` : "🛡 network: only the model API", tone: "ok" as Tone }]]
          : webUse(wd, a).map((w, i) => [{ text: `${i ? "  " : "⚠ used the internet (may have looked up the fix; result not trustworthy): "}${w}`, tone: "warn" as Tone }])),
        [{ text: raw ? "showing: raw agent change (agent.diff) · r = graded change" : "showing: graded change (solution.diff) · r = raw agent change", tone: "info" }],
      ]
      const list: Seg[][] = files.length
        ? files.map((f) => [
            { text: `${mark[f.status]} ${f.path}  `, tone: "base" },
            { text: `+${f.added}`, tone: "add" },
            { text: " ", tone: "faint" },
            { text: `−${f.removed}`, tone: "del" },
          ])
        : [[{ text: "(no change)", tone: "faint" }]]
      const doc: View = {
        title: `${fit(title, 40)} · ${short(String(a.model ?? a.agent ?? "?"))} · ${verdictOf(a)}`,
        sections: [
          { heading: "Summary", lines: summary },
          { heading: `Files changed (${files.length})  +${added} −${removed}`, lines: list },
          ...files.map((f) => ({
            heading: `${mark[f.status]} ${f.path}  (+${f.added} −${f.removed})`,
            lines: f.lines.length ? f.lines.map((l) => [toneOf(l, "diff")]) : [[{ text: "(no text change)", tone: "faint" as Tone }]],
          })),
        ],
        log: [],
        running: false,
        actions: [{ key: "r", title: raw ? "graded diff" : "raw diff", run: () => showSolution(wd, a, !raw, false) }],
      }
      if (context.ui.router.current().type === "plugin" && !keepBack) setView(doc)
      else open(doc, keepBack)
    }

    const showRun = async (runID: string) => {
      const attempts = await data({ op: "attempts", run_id: runID })
      if (!attempts.length) return context.ui.toast.show({ message: "This run has no attempts yet.", variant: "warning" })
      const choice = await context.ui.dialog.select({
        title: `Run ${when(runID)}`,
        options: attempts.map((a: any) => ({
          title: `${a.resolved ? "✓" : "✗"} ${a.task_id}`,
          description: `attempt ${a.attempt} · ${a.category} · ${a.agent_duration_s ?? "?"}s`,
          footer: a.branch ? "branch" : undefined,
          value: a,
        })),
      })
      if (choice) showDoc(await data({ op: "attempt", run_id: runID, task_id: choice.task_id, attempt: choice.attempt }))
    }

    const pickTasks = async (title: string, tasks: any[]) => {
      const approved = tasks.filter((t) => t.status === "approved").map((t) => t.task_id)
      const pending = tasks.filter((t) => t.status === "pending").map((t) => t.task_id)
      const options: any[] = []
      if (approved.length > 1) options.push({ title: `All approved tasks (${approved.length})`, value: approved, category: "Groups" })
      if (pending.length > 1) options.push({ title: `All pending tasks (${pending.length})`, value: pending, category: "Groups" })
      for (const t of tasks)
        options.push({ title: t.title, description: t.task_id, footer: t.status, value: [t.task_id], category: "Tasks" })
      return context.ui.dialog.select<string[]>({ title, placeholder: "Search tasks", options })
    }

    // The engine's stored logins (/connect): copy the model provider's one into the sandbox.
    const hasLogin = (model: string) => {
      try {
        const data = JSON.parse(readFileSync(join(homedir(), ".local/share/task-factory/auth.json"), "utf8"))
        return model.split("/")[0] in data
      } catch {
        return false
      }
    }

    // Solve SWE tasks with the existing coding harness (the Python CLI), in the task's own store.
    const runSwe = async (tasks: SweTask[], model: string, forSession = sessionHere()) => {
      const store = tasks[0].store
      const before = new Set(runIDs(store))
      const args = ["-u", "-m", "factory.cli", "harness", "run", "--agent", "task-factory", "--model", model, "--workdir", store,
        "--tasks", join(store, "tasks"), ...tasks.flatMap((t) => ["--task", t.task_id]), ...(hasLogin(model) ? ["--use-host-auth"] : [])]
      const result = await liveRun(`solving ${tasks.length} SWE task(s) with ${model}`, (progress) =>
        callProcess(python, args, store, undefined, { progress }, true),
      )
      const fresh = newRuns(store, before, tasks.map((t) => t.task_id))
      if (forSession) linkRuns(store, forSession, fresh, model, tasks.map((t) => t.task_id))
      syncRecords(store)
      refresh()
      const attempts = fresh.flatMap((id) => readAttempts(store, id))
      if (result && attempts.length) {
        setView((v) => ({
          ...v,
          log: [...v.log, "", ...attempts.map((a) => `${a.resolved ? "✓ resolved" : "✗ " + a.category}  ${a.task_id}` +
            (a.egress_blocked?.length ? `  🛡 refused: ${a.egress_blocked.join(", ")}` : !a.egress_blocked && webUse(store, a).length ? "  ⚠ used the web (may have looked up the fix)" : "")), "see the change: /solutions"],
        }))
      }
    }

    const showSweTask = (t: SweTask) => {
      const f2p = listOf(t.meta.FAIL_TO_PASS)
      const p2p = listOf(t.meta.PASS_TO_PASS)
      const patch = filesChanged(readPart(t, "solution/solution.diff"))
      const tests = filesChanged(JSON.parse(readPart(t, "swe.json") || "{}").test_patch ?? "")
      const mark = { added: "A", deleted: "D", renamed: "R", binary: "B", modified: "M" }
      const fileRows = (files: ReturnType<typeof filesChanged>): Seg[][] =>
        files.flatMap((f) => [
          [{ text: `${mark[f.status]} ${f.path}  `, tone: "base" as Tone, bold: true }, { text: `+${f.added} −${f.removed}`, tone: "faint" as Tone }],
          ...f.lines.map((l) => [toneOf(l, "diff")]),
        ])
      showDoc({
        title: `${t.meta.repo} #${t.meta.pull_request.split("/").pop()} · ${t.status}`,
        sections: [
          {
            heading: "Summary",
            lines: [
              [{ text: `task: ${t.task_id}`, tone: "base" }],
              [{ text: `pull request: ${t.meta.pull_request}`, tone: "muted" }],
              [{ text: `issue(s): ${t.meta.issues.join(", ")}`, tone: "muted" }],
              [{ text: `base commit: ${t.meta.base_commit}  (the exact commit before the change)`, tone: "muted" }],
              [{ text: `hidden tests: ${f2p.length} FAIL_TO_PASS · ${p2p.length} PASS_TO_PASS`, tone: "ok" }],
              ...(t.meta.quality?.flags.length
                ? [[{
                    text: `⚠ check: ${t.meta.quality.flags.join(", ")}` +
                      (t.meta.quality.unmentioned_names.length ? ` — the tests call ${t.meta.quality.unmentioned_names.join(", ")}, the issue never names it` : ""),
                    tone: "warn" as Tone,
                  }]]
                : [[{ text: "✓ quality checks: no flags", tone: "ok" as Tone }]]),
              [{ text: `status: ${t.status}   x run with a model · a approve · d delete`, tone: "info" }],
            ],
          },
          { heading: "Problem statement (instruction.md, the only thing the solver sees)", text: readPart(t, "instruction.md") },
          { heading: `Reference patch (the PR's code change) · ${patch.length} file(s)`, lines: fileRows(patch) },
          { heading: `Hidden tests 🔒 (the PR's test change) · ${tests.length} file(s)`, lines: fileRows(tests) },
          { heading: "FAIL_TO_PASS (fail before the change, pass after)", lines: f2p.map((id) => [{ text: id, tone: "ok" as Tone }]) },
        ],
      })
      const cli = (action: string, ...extra: string[]) =>
        callProcess(python, ["-u", "-m", "factory.cli", "tasks", action, t.task_id, "--workdir", t.store, ...extra], t.store, undefined, {}, true)
      setView((v) => ({
        ...v,
        actions: [
          {
            key: "x",
            title: "run",
            run: async () => {
              const model = await pickModel()
              if (model) await runSwe([t], model)
            },
          },
          {
            key: "a",
            title: "approve",
            run: async () => {
              const r = await cli("approve")
              context.ui.toast.show({ message: r.ok ? `Approved ${t.task_id}` : r.error ?? "failed", variant: r.ok ? "success" : "error" })
            },
          },
          {
            key: "d",
            title: "delete",
            run: async () => {
              const reason = await context.ui.dialog.prompt({ title: `Delete ${t.task_id}`, placeholder: "Why? (kept in the review log)" })
              if (!reason?.trim()) return
              const r = await cli("delete", "--reason", reason.trim())
              context.ui.toast.show({ message: r.ok ? `Deleted ${t.task_id}` : r.error ?? "failed", variant: r.ok ? "success" : "error" })
            },
          },
        ],
      }))
    }

    const commands = [
      {
        id: "task-factory.swe",
        title: "Mine SWE tasks from a GitHub repository",
        description: "Merged PRs that close an issue -> tasks at the exact base commit with hidden tests (SWE-bench style)",
        slash: { name: "swe", arguments: true as const },
        async run(input?: string) {
          const text =
            input?.trim() ||
            (await context.ui.dialog.prompt({ title: "Mine SWE tasks", placeholder: "https://github.com/OWNER/REPO  [how many tasks, default 5]" }))
          if (!text?.trim()) return
          const [url, count] = text.trim().split(/\s+/)
          const max = Number(count) > 0 ? String(Math.floor(Number(count))) : "5"
          const selection = context.ui.model.current()
          let setupSession: string | undefined
          const result = await liveRun(`SWE tasks from ${url}`, (progress) =>
            callProcess("node", [SWE_CLI, "mine", url, "--max", max, "--json"], project(), undefined, {
              progress,
              // the setup agent: asked only when the plain environment cannot run the tests
              prompt: async (prompt) => {
                if (!selection) throw new Error("the environment needs the setup agent: choose a model with /models")
                if (!setupSession) {
                  const created: any = await context.client.session.create({
                    title: `SWE setup: ${url.replace(/^https?:\/\/github\.com\//, "")}`,
                    model: { providerID: selection.providerID, id: selection.modelID, variant: selection.variant },
                    location: { directory: project() },
                    metadata: { taskFactory: "swe-setup" },
                    permissions: [{ action: "*", resource: "*", effect: "deny" }],
                  })
                  setupSession = created.id
                }
                progress(`… ${selection.providerID}/${selection.modelID} proposes setup commands (session "SWE setup", no tools)`)
                return askInSession(setupSession!, prompt)
              },
            }),
          )
          if (result?.ok && result.data) {
            const f = result.data.funnel
            setView((v) => ({
              ...v,
              log: [...v.log, "", `funnel: ${f.scanned} PRs scanned → ${f.processed} tried → ${f.tasks} task(s)`,
                `dropped: ${JSON.stringify(f.dropped)} · not candidates: ${JSON.stringify(f.mining_skipped)}`,
                `${result.data.made.length} new task(s), pending review: /swe-tasks`],
            }))
          }
        },
      },
      {
        id: "task-factory.swe-tasks",
        title: "SWE tasks",
        description: "Mined tasks: problem statement, reference patch, hidden tests; run, approve or delete",
        slash: { name: "swe-tasks" },
        async run() {
          try {
            const all = sweTasks()
            if (!all.length) return context.ui.toast.show({ message: "No SWE tasks yet. Mine some with /swe https://github.com/OWNER/REPO", variant: "warning" })
            const pick = await context.ui.dialog.select<SweTask | string>({
              title: "SWE tasks",
              placeholder: "Search",
              options: [
                ...[...new Set(all.map((t) => t.store))].map((store) => ({
                  title: `Run every task of ${all.find((t) => t.store === store)!.meta.repo} with a model`,
                  value: store,
                  category: "Groups",
                })),
                ...all.map((t) => ({
                  title: `#${t.meta.pull_request.split("/").pop()} ${t.task_id}`,
                  description: `${listOf(t.meta.FAIL_TO_PASS).length} FAIL_TO_PASS · ${listOf(t.meta.PASS_TO_PASS).length} PASS_TO_PASS` +
                    (t.meta.quality?.flags.length ? ` · ⚠ ${t.meta.quality.flags.join(", ")}` : ""),
                  footer: t.status,
                  category: t.meta.repo,
                  value: t,
                })),
              ],
            })
            if (!pick) return
            if (typeof pick === "string") {
              const model = await pickModel()
              if (model) await runSwe(all.filter((t) => t.store === pick), model)
            } else showSweTask(pick)
          } catch (error) {
            toastError(error)
          }
        },
      },
      {
        id: "task-factory.setup",
        title: "Set up this project for Task Factory",
        description: "Pin the committed HEAD and build its Docker test environment",
        slash: { name: "setup" },
        async run() {
          await live("setup", { op: "setup" })
        },
      },
      {
        id: "task-factory.generate-task",
        title: "Generate a benchmark task",
        description: "Generate a task with the selected model and validate it",
        slash: { name: "generate-task", arguments: true as const },
        async run(input?: string) {
          const request =
            input?.trim() ||
            (await context.ui.dialog.prompt({ title: "Generate a benchmark task", placeholder: "Describe the exercise" }))
          if (!request?.trim()) return
          const selection = context.ui.model.current()
          if (!selection) {
            context.ui.toast.show({ message: "Choose a model with /models first.", variant: "warning" })
            return
          }
          const model = { providerID: selection.providerID, id: selection.modelID, variant: selection.variant }
          const parent = sessionID()
          let genSession: string
          try {
            // A separate top-level session, never a child: the engine hands a child's result
            // (or failure) to the parent session's agent, which then acted on it and edited the
            // repo. The link to the user's session is Task Factory's own session_id record.
            // Read-only: it may read the repo for context but not edit, run commands or browse.
            const created: any = await context.client.session.create({
              title: `Generate task: ${request.trim().slice(0, 50)}`,
              model,
              location: { directory: project() },
              metadata: { taskFactory: "generate-task", ...(parent ? { forSession: parent } : {}) },
              permissions: [
                { action: "*", resource: "*", effect: "deny" },
                { action: "read", resource: "*", effect: "allow" },
                { action: "grep", resource: "*", effect: "allow" },
                { action: "glob", resource: "*", effect: "allow" },
              ],
            })
            genSession = created.id
          } catch (error) {
            return toastError(error)
          }
          await live(
            "generate a task",
            {
              request: request.trim(),
              model: model.providerID + "/" + model.id,
              session_id: parent ?? genSession,
            },
            {
              prompt: async (prompt) => {
                setView((v) => ({
                  ...v,
                  log: [...v.log, `… ${model.providerID}/${model.id} is writing in session "Generate task: …" (read-only; see /sessions)`],
                }))
                return askInSession(genSession, prompt)
              },
            },
          )
        },
      },
      {
        id: "task-factory.tasks",
        title: "Task Factory tasks",
        description: "List tasks; show, run, approve or delete one",
        slash: { name: "tasks" },
        async run() {
          try {
            const here = sessionID()
            const tasks = (await data({ op: "tasks" })).sort(
              (a: any, b: any) => Number(b.session_id === here && !!here) - Number(a.session_id === here && !!here),
            )
            if (!tasks.length)
              return context.ui.toast.show({ message: "No tasks yet. Create one with /generate-task.", variant: "warning" })
            const task = await context.ui.dialog.select({
              title: "Tasks",
              placeholder: "Search tasks",
              options: tasks.map((t: any) => ({
                title: t.title,
                description: `${t.task_id} · ${t.author}${t.last_result ? " · " + t.last_result : ""}`,
                footer: t.status,
                category: here && t.session_id === here ? "This session" : t.status,
                value: t,
              })),
            })
            if (!task) return
            const actions = [
              { title: "Show details", value: "show" },
              ...(task.status !== "deleted" ? [{ title: "Run with a model", value: "run" }] : []),
              ...(task.status !== "approved" && task.status !== "deleted" ? [{ title: "Approve", value: "approve" }] : []),
              ...(task.status !== "deleted" ? [{ title: "Delete…", value: "delete" }] : []),
            ]
            const action = await context.ui.dialog.select({ title: task.title, options: actions })
            if (action === "show") showDoc(await data({ op: "task", task_id: task.task_id }))
            else if (action === "run") await runTasks([task.task_id])
            else if (action === "approve") {
              await data({ op: "approve", task_id: task.task_id })
              context.ui.toast.show({ message: `Approved ${task.task_id}`, variant: "success" })
            } else if (action === "delete") {
              const reason = await context.ui.dialog.prompt({ title: `Delete ${task.task_id}`, placeholder: "Why? (kept in the review log)" })
              if (!reason?.trim()) return
              await data({ op: "delete", task_id: task.task_id, reason })
              context.ui.toast.show({ message: `Deleted ${task.task_id}` })
            }
          } catch (error) {
            toastError(error)
          }
        },
      },
      {
        id: "task-factory.run-task",
        title: "Run tasks with a model",
        description: "Solve tasks in Docker with the selected model and grade them with the hidden tests",
        slash: { name: "run-task" },
        async run() {
          try {
            if (busy) return void open(view())
            const tasks = (await data({ op: "tasks" })).filter((t: any) => t.status !== "deleted")
            if (!tasks.length)
              return context.ui.toast.show({ message: "No tasks yet. Create one with /generate-task.", variant: "warning" })
            const ids = await pickTasks("Which tasks?", tasks)
            if (ids?.length) await runTasks(ids)
          } catch (error) {
            toastError(error)
          }
        },
      },
      {
        id: "task-factory.results",
        title: "Task Factory results",
        description: "Runs, attempts, the agent session, its diff and the hidden test output",
        slash: { name: "results" },
        async run() {
          try {
            const runs = await data({ op: "runs" })
            if (!runs.length) return context.ui.toast.show({ message: "No runs yet. Start one with /run-task.", variant: "warning" })
            const run = await context.ui.dialog.select({
              title: "Runs",
              options: runs.map((r: any) => ({
                title: `${when(r.run_id)}  ${r.model ?? r.agent}`,
                description: `${r.resolved}/${r.attempts} resolved${r.finished ? "" : " · running or stopped"}${r.stopped_early ? " · stopped early" : ""}`,
                value: r.run_id,
              })),
            })
            if (run) await showRun(run)
          } catch (error) {
            toastError(error)
          }
        },
      },
      {
        id: "task-factory.solutions",
        title: "AI solutions in this session",
        description: "What the models changed for this session's tasks, file by file (r = raw diff)",
        slash: { name: "solutions" },
        async run() {
          try {
            const session = sessionHere()
            if (!session) return context.ui.toast.show({ message: "Open a session first: /solutions shows the tasks run in it.", variant: "warning" })
            const list = await sessionSolutions(session)
            if (!list.length)
              return context.ui.toast.show({ message: "No task was run in this session yet. Use /run-task or /rerun.", variant: "warning" })
            const pick = await context.ui.dialog.select({
              title: "AI solutions in this session",
              placeholder: "Search",
              options: list.map(({ wd, a }) => {
                const files = filesChanged(attemptDiff(wd, a))
                return {
                  title: `${a.resolved ? "✓" : a.category === "agent_error" ? "⚠" : "✗"} ${a.task_id}`,
                  description: `${short(String(a.model ?? a.agent))} · attempt ${a.attempt} · ${a.category} · ${files.length} file(s) +${files.reduce((n, f) => n + f.added, 0)} −${files.reduce((n, f) => n + f.removed, 0)}` +
                    (!a.egress_blocked && webUse(wd, a).length ? " · ⚠ used the web" : ""),
                  category: `run ${when(a.run_id)}${wd.startsWith(sweRoot()) ? " · SWE" : ""}`,
                  value: { wd, a },
                }
              }),
            })
            if (pick) showSolution(pick.wd, pick.a, false, true)
          } catch (error) {
            toastError(error)
          }
        },
      },
      {
        id: "task-factory.rerun",
        title: "Run this session's tasks on another model",
        description: "Same tasks, another model: in this session or in a copy of it",
        slash: { name: "rerun" },
        async run() {
          try {
            const session = sessionHere()
            if (!session) return context.ui.toast.show({ message: "Open a session first: /rerun uses the tasks of that session.", variant: "warning" })
            const dir = project()
            const wd = await workdirOf(dir)
            const ids = sessionTaskIDs(wd, session)
            if (!ids.length)
              return context.ui.toast.show({ message: "This session has no tasks yet: /generate-task or /run-task first.", variant: "warning" })
            const all = (await data({ op: "tasks" })).filter((t: any) => ids.includes(t.task_id) && t.status !== "deleted")
            if (!all.length) return context.ui.toast.show({ message: "This session's tasks were deleted.", variant: "warning" })
            const chosen = await context.ui.dialog.select<string[]>({
              title: "Which of this session's tasks?",
              placeholder: "Search tasks",
              options: [
                ...(all.length > 1 ? [{ title: `All tasks of this session (${all.length})`, value: all.map((t: any) => t.task_id), category: "Groups" }] : []),
                ...all.map((t: any) => ({ title: t.title, description: t.task_id, footer: t.status, value: [t.task_id], category: "Tasks" })),
              ],
            })
            if (!chosen?.length) return
            const model = await pickModel()
            if (!model) return
            const where = await context.ui.dialog.select({
              title: `Run ${chosen.length} task(s) on ${model}`,
              options: [
                { title: "In this session", description: "the new attempts sit next to the earlier ones in /solutions", value: "here" },
                { title: "In a copy of this session", description: "a new session with the same tasks; its /solutions shows only this model", value: "copy" },
              ],
            })
            if (!where) return
            let target = session
            if (where === "copy") {
              const [providerID, ...rest] = model.split("/")
              const created: any = await context.client.session.create({
                title: `Rerun: ${chosen.length} task(s) on ${rest.join("/") || model}`,
                model: { providerID, id: rest.join("/") },
                location: { directory: dir },
                metadata: { taskFactory: "rerun", fromSession: session },
              })
              target = created.id
              copyTasks(wd, target, chosen, session)
              syncRecords(wd)
              context.ui.router.navigate({ type: "session", sessionID: target })
            }
            await runTasks(chosen, model, target)
          } catch (error) {
            toastError(error)
          }
        },
      },
      {
        id: "task-factory.benchmark",
        title: "Task Factory benchmark",
        description: "Tasks x models: who solved what, difficulty, failures, time and tokens",
        slash: { name: "benchmark" },
        async run() {
          try {
            const b = await data({ op: "benchmark" })
            if (!b.matrix.length) return context.ui.toast.show({ message: "No tasks yet.", variant: "warning" })
            showBenchmark(b)
          } catch (error) {
            toastError(error)
          }
        },
      },
    ]

    context.keymap.layer(() => ({
      mode: "global",
      commands: commands.map((command) => ({ ...command, group: "Task Factory", palette: true as const })),
    }))
  },
})
