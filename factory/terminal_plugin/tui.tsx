import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { Plugin } from "@task-factory/plugin/tui"

async function generateTask(python, project, request, model, generate) {
  const process = spawn(python, ["-u", "-m", "factory.terminal_bridge"], {
    cwd: project,
    stdio: ["pipe", "pipe", "pipe"],
  })
  const completed = new Promise((resolve) => {
    process.on("error", (error) => resolve({ error }))
    process.on("close", (code) => resolve({ code }))
  })
  let diagnostics = ""
  process.stderr.setEncoding("utf8")
  process.stderr.on("data", (data) => {
    diagnostics = (diagnostics + data).slice(-3500)
  })
  process.stdin.on("error", () => {})
  const send = (value) => process.stdin.write(JSON.stringify(value) + "\n")
  const lines = createInterface({ input: process.stdout })
  send({ request, model: model.providerID + "/" + model.id })
  try {
    let result
    for await (const line of lines) {
      const event = JSON.parse(line)
      if (event.type === "prompt") {
        const answer = await generate({ model, prompt: event.text })
        send({ answer: answer.text })
      } else if (event.type === "result") {
        result = event.text
        process.stdin.end()
      }
    }
    const status = await completed
    if (!result) {
      throw new Error(status.error?.message ?? (diagnostics.trim() || "Task Factory exited with status " + status.code))
    }
    return result
  } finally {
    lines.close()
    process.stdin.destroy()
    if (process.exitCode === null) process.kill()
  }
}

export default Plugin.define({
  id: "task-factory",
  setup(context) {
    context.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: "task-factory.generate-task",
        title: "Generate a benchmark task",
        description: "Generate a task with the selected model and validate it",
        group: "Task Factory",
        palette: true,
        slash: { name: "generate-task", arguments: true },
        async run(input) {
          const request = input?.trim() || await context.ui.dialog.prompt({
            title: "Generate a benchmark task",
            placeholder: "Describe the exercise",
          })
          if (!request?.trim()) return
          const selection = context.ui.model.current()
          if (!selection) {
            context.ui.toast.show({ message: "Choose a model with /models first.", variant: "warning" })
            return
          }
          const route = context.ui.router.current()
          const session = route.type === "session" ? context.data.session.get(route.sessionID) : undefined
          const project = (session?.location ?? context.location ?? context.data.location.default()).directory
          const model = { providerID: selection.providerID, id: selection.modelID, variant: selection.variant }
          context.ui.toast.show({ message: "Task Factory is generating and validating the task." })
          try {
            const result = await generateTask(context.options.python, project, request.trim(), model,
              (payload) => context.client.experimental.generate.text(payload))
            context.ui.dialog.show(() => (
              <box padding={1} gap={1}>
                <text>Task Factory</text>
                <text wrapMode="word">{result}</text>
              </box>
            ))
          } catch (error) {
            context.ui.toast.show({ message: error instanceof Error ? error.message : String(error), variant: "error" })
          }
        },
      }],
    }))
  },
})
