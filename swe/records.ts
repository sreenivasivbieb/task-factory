// Dual write to MongoDB (factory/records.py): when TASK_FACTORY_MONGO_URI is set, a work folder's
// records are synced through the Python side (`python -m factory.records sync <folder>`), so the
// TypeScript code needs no MongoDB driver. Files stay the source; a failure only warns.
import { run, type Runner } from "./util.ts"

export async function syncRecords(folder: string, exec: Runner = run): Promise<string | undefined> {
  if (!process.env.TASK_FACTORY_MONGO_URI) return undefined
  const python = process.env.TASK_FACTORY_PYTHON || "python3"
  const r = await exec(python, ["-m", "factory.records", "sync", folder], { timeoutMs: 120_000 })
  if (r.code !== 0) return `records not written to MongoDB: ${(r.stderr || r.stdout).trim().split("\n").pop()}`
  return /"enabled": false/.test(r.stdout) ? "records not written to MongoDB: it is not reachable" : undefined
}
