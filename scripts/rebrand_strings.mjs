// Replace the upstream product name in user-facing text of the terminal engine source.
//
// Only string literals, template text and JSX text change, never identifiers, imports
// or URLs, so code such as `OpenCode.make()` keeps compiling. Internal IDs written in
// lowercase (provider "opencode", "opencode.settings", env vars) are left as they are.
//
//   node rebrand_strings.mjs <engine checkout> [--check]
//
// --check prints what would change and writes nothing.
import fs from "node:fs"
import path from "node:path"
import { createRequire } from "node:module"

const source = path.resolve(process.argv[2] ?? ".")
const check = process.argv.includes("--check")
const ts = createRequire(path.join(source, "package.json"))("typescript")

// Packages bundled into the terminal. `app` (browser UI) and `desktop` are not built.
// httpapi-codegen emits code (`export * as OpenCode`), so its strings are identifiers.
const SKIP_PACKAGES = new Set(["app", "desktop", "web", "console", "docs", "storybook", "httpapi-codegen"])
const SUBCOMMANDS = [
  "acp", "agent", "auth", "debug", "export", "import", "issue", "login", "logout", "mcp", "mini",
  "models", "pair", "plugin", "reload", "run", "serve", "service", "session", "stats", "uninstall",
  "upgrade", "version", "web",
]

const RULES = [
  // Words joined to "-", "/" or ".name" are headers, tags or span names, not prose.
  [/(?<![\w/-])OpenCode Zen\b/g, "Zen"],
  [/(?<![\w/-])OpenCode (Go|Console|Web Search)\b/g, "Zen $1"],
  [/(?<![\w/.-])OpenCode(?![\w-]|\.\w)/g, "Task Factory"],
  [new RegExp(`(?<![\\w./@-])opencode (?=(?:${SUBCOMMANDS.join("|")})\\b)`, "g"), "task-factory "],
  [/(?<![\w./@-])opencode (?=(?:can|cannot|drops|will|is|uses|needs|starts|runs)\b)/g, "Task Factory "],
  [/(?<![\w./@-])opencode\.json(c?)\b/g, "task-factory.json$1"],
  [/(?<![\w.:/@-])\.opencode\//g, ".task-factory/"],
  [/(?<=run |Run |then run )opencode\b(?![\w.-])/g, "task-factory"],
  [/(?<=\.task-factory\/)opencode\.json(c?)\b/g, "task-factory.json$1"],
  [/\b([Aa])n (?=(?:Task Factory|task-factory|Zen)\b)/g, "$1 "],
]

function moduleSpecifier(node) {
  const parent = node.parent
  if (!parent) return false
  if (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent)) return parent.moduleSpecifier === node
  if (ts.isExternalModuleReference(parent) || ts.isLiteralTypeNode(parent)) return true
  if (ts.isCallExpression(parent)) {
    const callee = parent.expression
    return callee.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(callee) && callee.text === "require")
  }
  return false
}

// Header values (e.g. a provider's billing origin) are matched by the provider, not read.
function headerValue(node) {
  for (let current = node.parent, depth = 0; current && depth < 4; current = current.parent, depth++) {
    if (ts.isPropertyAssignment(current)) {
      const name = current.name
      const key = ts.isStringLiteral(name) || ts.isIdentifier(name) ? name.text : ""
      return /^(x-|http-|user-agent$|originator$)/i.test(key)
    }
  }
  return false
}

function isText(node) {
  return (
    ts.isStringLiteral(node) ||
    ts.isNoSubstitutionTemplateLiteral(node) ||
    ts.isTemplateHead(node) ||
    ts.isTemplateMiddle(node) ||
    ts.isTemplateTail(node) ||
    node.kind === ts.SyntaxKind.JsxText
  )
}

function rewrite(text) {
  let out = text
  for (const [pattern, replacement] of RULES) out = out.replace(pattern, replacement)
  return out
}

function rebrand(file) {
  const text = fs.readFileSync(file, "utf8")
  if (!/opencode/i.test(text)) return []
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind)
  const edits = []
  const visit = (node) => {
    if (isText(node) && !moduleSpecifier(node) && !headerValue(node)) {
      const start = node.kind === ts.SyntaxKind.JsxText ? node.pos : node.getStart(sf)
      const original = text.slice(start, node.end)
      const updated = rewrite(original)
      if (updated !== original) edits.push({ start, end: node.end, original, updated })
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  if (edits.length && !check) {
    let out = text
    for (const edit of edits.toReversed()) out = out.slice(0, edit.start) + edit.updated + out.slice(edit.end)
    fs.writeFileSync(file, out)
  }
  return edits
}

// Prompt and skill text bundled as files. The model catalog snapshot is data, not prose.
const TEXT_SKIP = new Set(["snapshot.txt", "README.md", "CHANGELOG.md"])

function rebrandText(file) {
  const text = fs.readFileSync(file, "utf8")
  const updated = rewrite(text)
  if (updated === text) return []
  if (!check) fs.writeFileSync(file, updated)
  const lines = updated.split("\n").filter((line, i) => line !== text.split("\n")[i])
  return lines.map((line) => ({ updated: line }))
}

function* sources(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const full = path.join(directory, entry.name)
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules" && entry.name !== "test" && entry.name !== "__tests__") yield* sources(full)
    } else if (/\.(txt|md)$/.test(entry.name) && !TEXT_SKIP.has(entry.name)) {
      yield full
    } else if (/\.tsx?$/.test(entry.name) && !/\.(test|spec|stories)\.tsx?$/.test(entry.name) && !entry.name.endsWith(".d.ts")) {
      yield full
    }
  }
}

let files = 0
let changes = 0
for (const pkg of fs.readdirSync(path.join(source, "packages"))) {
  const src = path.join(source, "packages", pkg, "src")
  if (SKIP_PACKAGES.has(pkg) || !fs.existsSync(src)) continue
  for (const file of sources(src)) {
    const edits = /\.(txt|md)$/.test(file) ? rebrandText(file) : rebrand(file)
    if (!edits.length) continue
    files += 1
    changes += edits.length
    if (check) {
      for (const edit of edits) {
        const line = edit.updated.replace(/\s+/g, " ").slice(0, 140)
        console.log(`${path.relative(source, file)}: ${line}`)
      }
    }
  }
}
console.log(`${check ? "Would rewrite" : "Rewrote"} ${changes} strings in ${files} files`)
