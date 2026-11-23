// Task Factory commands inside the terminal:
//   /setup          pin this project and build its test environment (once)
//   /generate-task  draft a task with the selected model and validate it
//   /tasks          list tasks; show, run, approve or delete one
//   /run-task       solve tasks with a model in Docker (live progress)
//   /results        runs -> attempts -> verdict, agent session, diff, test output
// The work happens in Python (`python -m factory.terminal_bridge`, one process per command).
import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { TextAttributes, type ScrollBoxRenderable } from "@opentui/core"
import { useTerminalDimensions } from "@opentui/solid"
import { createSignal, For, Show } from "solid-js"
import { Plugin } from "@task-factory/plugin/tui"

type Section = { heading: string; text: string }
type View = { title: string; sections: Section[]; log: string[]; running: boolean; footer?: string }
type Result = { ok: boolean; text: string; error?: string; data?: any }

const PAGE = "task-factory"

function callBridge(
  python: string,
  project: string,
  request: Record<string, unknown>,
  hooks: { progress?: (line: string) => void; prompt?: (text: string) => Promise<string> } = {},
): Promise<Result> {
  return new Promise((resolveCall, rejectCall) => {
    const child = spawn(python, ["-u", "-m", "factory.terminal_bridge"], { cwd: project, stdio: ["pipe", "pipe", "pipe"] })
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
      else rejectCall(new Error(diagnostics.trim() || `Task Factory exited with status ${code}`))
    })
    send(request)
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
  props.context.keymap.layer(() => ({
    commands: [
      { bind: "escape", title: "back", run: props.onClose },
      { bind: "up", title: "Scroll up", run: () => scroll?.scrollBy(-1) },
      { bind: "down", title: "Scroll down", run: () => scroll?.scrollBy(1) },
      { bind: "pageup", title: "Previous page", run: () => scroll?.scrollBy(-page()) },
      { bind: "pagedown", title: "Next page", run: () => scroll?.scrollBy(page()) },
      { bind: "home", title: "Top", run: () => scroll?.scrollTo(0) },
      { bind: "end", title: "Bottom", run: () => scroll?.scrollTo(Infinity) },
    ],
  }))
  return (
    <box width="100%" height="100%" backgroundColor={theme.background.base} padding={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
        <text fg={theme.text.base} attributes={TextAttributes.BOLD}>
          Task Factory · {props.view().title}
        </text>
        <text fg={theme.text.muted}>{props.view().running ? "working…  " : ""}esc back · ↑↓ pgup pgdn scroll</text>
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
                <text fg={theme.text.base} attributes={TextAttributes.BOLD}>
                  {section.heading}
                </text>
                <text fg={theme.text.base} wrapMode="word">
                  {section.text}
                </text>
              </box>
            )}
          </For>
          <Show when={props.view().log.length > 0}>
            <text fg={theme.text.muted} wrapMode="word">
              {props.view().log.join("\n")}
            </text>
          </Show>
          <Show when={props.view().footer}>
            <text fg={theme.text.base} wrapMode="word">
              {props.view().footer}
            </text>
          </Show>
        </box>
      </scrollbox>
    </box>
  )
}

export default Plugin.define({
  id: "task-factory",
  setup(context) {
    const python = context.options.python
    const [view, setView] = createSignal<View>({ title: "", sections: [], log: [], running: false })
    const [previous, setPrevious] = createSignal<any>({ type: "home" })
    let busy: string | undefined // the command running in the live view, if any

    context.ui.router.register({
      name: PAGE,
      render: () => <Page context={context} view={view} onClose={() => context.ui.router.navigate(previous())} />,
    })

    const project = () => {
      const route = context.ui.router.current()
      const session = route.type === "session" ? context.data.session.get(route.sessionID) : undefined
      return (session?.location ?? context.location ?? context.data.location.default()).directory
    }
    const open = (next: View) => {
      setView(next)
      const current = context.ui.router.current()
      if (!(current.type === "plugin" && current.name === PAGE)) setPrevious({ ...current })
      context.ui.dialog.clear()
      context.ui.router.navigate({ type: "plugin", name: PAGE })
    }
    const toastError = (error: unknown) =>
      context.ui.toast.show({ message: error instanceof Error ? error.message : String(error), variant: "error" })
    const call = (request: Record<string, unknown>) => callBridge(python, project(), request)
    const data = async (request: Record<string, unknown>) => {
      const result = await call(request)
      if (!result.ok) throw new Error(result.text)
      return result.data
    }

    // A long command shown live: progress lines stream into the page.
    const live = async (title: string, request: Record<string, unknown>, extra: Parameters<typeof callBridge>[3] = {}) => {
      if (busy) {
        open(view())
        context.ui.toast.show({ message: `Still working on: ${busy}`, variant: "warning" })
        return undefined
      }
      busy = title
      open({ title, sections: [], log: [], running: true })
      try {
        const result = await callBridge(python, project(), request, {
          ...extra,
          progress: (line) => setView((v) => ({ ...v, log: [...v.log, line] })),
        })
        setView((v) => ({ ...v, running: false, footer: result.ok ? "Done." : result.error ?? result.text }))
        context.attention?.notify?.({ title: "Task Factory", message: `${title}: ${result.ok ? "done" : "failed"}` })
        return result
      } catch (error) {
        setView((v) => ({ ...v, running: false, footer: String(error instanceof Error ? error.message : error) }))
        return undefined
      } finally {
        busy = undefined
      }
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

    const showDoc = (doc: { title: string; sections: Section[] }) => open({ ...doc, log: [], running: false })

    const runTasks = async (taskIDs: string[]) => {
      const model = await pickModel()
      if (!model) return
      const result = await live(`solving ${taskIDs.length} task(s) with ${model}`, { op: "run", task_ids: taskIDs, model })
      if (result?.ok && result.data?.run_id) await showRun(result.data.run_id)
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

    const commands = [
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
          await live(
            "generate a task",
            { request: request.trim(), model: model.providerID + "/" + model.id },
            {
              prompt: async (prompt) => {
                setView((v) => ({ ...v, log: [...v.log, `… waiting for ${model.providerID}/${model.id}`] }))
                const answer = await context.client.experimental.generate.text({ model, prompt })
                return answer.text
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
            const tasks = await data({ op: "tasks" })
            if (!tasks.length)
              return context.ui.toast.show({ message: "No tasks yet. Create one with /generate-task.", variant: "warning" })
            const task = await context.ui.dialog.select({
              title: "Tasks",
              placeholder: "Search tasks",
              options: tasks.map((t: any) => ({
                title: t.title,
                description: `${t.task_id} · ${t.author}${t.last_result ? " · " + t.last_result : ""}`,
                footer: t.status,
                category: t.status,
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
    ]

    context.keymap.layer(() => ({
      mode: "global",
      commands: commands.map((command) => ({ ...command, group: "Task Factory", palette: true as const })),
    }))
  },
})
