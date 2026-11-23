"""Build the Task Factory terminal engine from its pinned upstream source."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

UPSTREAM = "c0ad18b402eb2827fc1ee71c171b1177b3fc02d1"
UPSTREAM_REPO = "https://github.com/anomalyco/opencode.git"
UPSTREAM_TAG = "v2.3.2"
PACKAGE = "task-factory"
ENGINE = "task-factory-engine"
BUN = ["npm", "exec", "--yes", "--package=bun@1.4.4", "--", "bun"]
LOGO = "import { TextAttributes } from \"@opentui/core\"\nimport { useTerminalDimensions } from \"@opentui/solid\"\nimport { useTheme } from \"../context/theme\"\n\nexport function Logo() {\n  const theme = useTheme()\n  const dimensions = useTerminalDimensions()\n  return (\n    <box>\n      {dimensions().height >= 12 && (\n        <text fg={theme.text.base} attributes={TextAttributes.BOLD}>\n          {dimensions().width < 24 ? \"TASK FACTORY\" : \"T A S K   F A C T O R Y\"}\n        </text>\n      )}\n    </box>\n  )\n}\n"

CHANGES = {
    "packages/util/src/global.ts": [
        ('const app = "opencode"', 'const app = "task-factory"'),
    ],
    "packages/cli/script/build.ts": [
        ('OPENCODE_CLI_NAME: "\'opencode\'"', 'OPENCODE_CLI_NAME: "\'task-factory\'"'),
        ("OPENCODE_ARTIFACT: `'cli'`", "OPENCODE_ARTIFACT: `'task-factory'`"),
    ],
    "packages/cli/src/commands/commands.ts": [
        ("OpenCode command line interface", "Task Factory command line interface"),
    ],
    "packages/tui/src/app.tsx": [
        ('setTerminalTitle(renderer, "OpenCode")', 'setTerminalTitle(renderer, "Task Factory")'),
        ('`OC | ${', '`TF | ${'),
        ('title: "Update OpenCode"', 'title: "Update Task Factory"'),
    ],
    "packages/tui/src/attention.ts": [
        ('const DEFAULT_TITLE = "OpenCode"', 'const DEFAULT_TITLE = "Task Factory"'),
    ],
    "packages/tui/src/routes/session/permission.tsx": [
        ("Tell OpenCode what to do differently", "Tell Task Factory what to do differently"),
    ],
    "packages/tui/src/mini/footer.permission.tsx": [
        ("Tell OpenCode what to do differently", "Tell Task Factory what to do differently"),
    ],
    "packages/tui/src/mini/footer.prompt.tsx": [("close OpenCode", "close Task Factory")],
    "packages/tui/src/mini/runtime.lifecycle.ts": [
        ('setTerminalTitle(renderer, "OpenCode")', 'setTerminalTitle(renderer, "Task Factory")'),
    ],
    "packages/tui/src/component/terminal-pane.tsx": [
        ("restart OpenCode", "restart Task Factory"),
    ],
    "packages/tui/src/component/error-component.tsx": [
        ("OpenCode crashed", "Task Factory crashed"),
        ("OpenCode {app.version}", "Task Factory {app.version}"),
    ],
    "packages/tui/src/feature-plugins/sidebar/footer.tsx": [
        ("OpenCode includes free models so you can start immediately.",
         "Choose a free model to start without a provider key."),
    ],
    "packages/tui/src/config/index.tsx": [
        ("once after OpenCode updates", "once after Task Factory updates"),
    ],
    "packages/cli/src/services/updater.ts": [
        ('if (OPENCODE_LOCAL)\n      return {', 'if (OPENCODE_LOCAL || OPENCODE_ARTIFACT === "task-factory")\n      return {'),
        ('This build runs from a source checkout. Use an installed OpenCode release to check for updates.',
         'This terminal is built from source. Rebuild the Task Factory engine to update it.'),
        # Never detect an OpenCode install as ours: `uninstall` would run its package
        # manager removal and strip `.opencode/bin` from the user's shell profiles.
        ('  const method = Effect.fnUntraced(function* () {\n    if (executable === curlBinary) return "curl"',
         '  const method = Effect.fnUntraced(function* () {\n    if (OPENCODE_ARTIFACT === "task-factory") return\n'
         '    if (executable === curlBinary) return "curl"'),
    ],
    "packages/cli/src/commands/handlers/uninstall.ts": [
        ('intro("Uninstall OpenCode")', 'intro("Uninstall Task Factory")'),
    ],
    # Isolation: never read or write OpenCode's project config, project ID or plan files.
    # Global directories are already separate through `app` in global.ts.
    "packages/core/src/config/discovery.ts": [
        ('export const names = ["opencode.json", "opencode.jsonc"]',
         'export const names = ["task-factory.json", "task-factory.jsonc"]'),
        ('".opencode"', '".task-factory"'),
        ('"/Library/Application Support/opencode"', '"/Library/Application Support/task-factory"'),
        ('ProgramData", "opencode")', 'ProgramData", "task-factory")'),
        ('AbsolutePath.make("/etc/opencode")', 'AbsolutePath.make("/etc/task-factory")'),
        ("ai.opencode.managed", "ai.task-factory.managed"),
    ],
    "packages/core/src/config.ts": [
        ('path.join(directory, "opencode.jsonc")', 'path.join(directory, "task-factory.jsonc")'),
    ],
    "packages/core/src/plugin/plan.ts": [
        ('path.join(global.home, ".opencode", "plan")', 'path.join(global.home, ".task-factory", "plan")'),
    ],
    "packages/core/src/project.ts": [
        ('path.join(dir, "opencode")', 'path.join(dir, "task-factory")'),
    ],
    "packages/core/src/git.ts": [
        ('path.join(commonDir, "opencode")', 'path.join(commonDir, "task-factory")'),
    ],
    "packages/tui/src/plugin/discovery.ts": [
        ('path.join(projectDirectory, ".opencode")', 'path.join(projectDirectory, ".task-factory")'),
    ],
    "packages/tui/src/util/config-directories.ts": [
        ('path.join(directory, ".opencode")', 'path.join(directory, ".task-factory")'),
    ],
    # The model catalog names the hosted provider; show it as "Zen" (IDs are unchanged).
    "packages/core/src/models-dev.ts": [
        ("      name: item.name,\n      activation:", '      name: item.name.replace(/^OpenCode\\b/, "Zen"),\n      activation:'),
    ],
    # Our plugin imports the TUI plugin API under the Task Factory name.
    "packages/tui/src/plugin/runtime-plugin-support.bun.ts": [
        ('"@opencode/plugin/tui": { Plugin, PluginContextProvider, usePlugin },\n  },',
         '"@opencode/plugin/tui": { Plugin, PluginContextProvider, usePlugin },\n'
         '    "@task-factory/plugin/tui": { Plugin, PluginContextProvider, usePlugin },\n  },'),
    ],
    # Data file names: task-factory.db and log/task-factory.log.
    "packages/cli/src/database-path.ts": [
        ("`opencode-${OPENCODE_CHANNEL.replace(", "`${OPENCODE_CHANNEL.replace("),
    ],
    "packages/util/src/observability/logging.ts": [
        ('path.join(Global.Path.log, "opencode.log")', 'path.join(Global.Path.log, "task-factory.log")'),
        ("path.join(Global.Path.log, `opencode-${channel.replace(", "path.join(Global.Path.log, `${channel.replace("),
    ],
    # Theme IDs stay "opencode*" so saved settings keep working; only the label changes.
    "packages/tui/src/component/dialog-theme-list.tsx": [
        ("      title: value,\n", '      title: value.replace(/^opencode/, "task-factory"),\n'),
    ],
    "packages/cli/src/commands/handlers/mcp/add.ts": [
        ('"opencode.json"', '"task-factory.json"'),
        ('"opencode.jsonc"', '"task-factory.jsonc"'),
        ('".opencode"', '".task-factory"'),
    ],
}


def brand(source: Path) -> None:
    # HEAD may be the pin itself or local commits on top of it (e.g. committed branding).
    pinned = subprocess.run(["git", "merge-base", "--is-ancestor", UPSTREAM, "HEAD"],
                            cwd=source, capture_output=True, check=False)
    if pinned.returncode != 0:
        raise SystemExit(f"Expected the OpenCode v2.3.2 source checkout at {UPSTREAM} (or a branch on it)")
    for name, replacements in CHANGES.items():
        path = source / name
        current = path.read_text(encoding="utf-8")
        text = current
        for old, new in replacements:
            if old in new:
                raise SystemExit(f"Replacement would repeat on every build: {name}")
            if old not in text and new not in text:
                raise SystemExit(f"Branding target changed: {name}")
            text = text.replace(old, new)
        if current != text:
            path.write_text(text, encoding="utf-8")
    (source / "packages/tui/src/component/logo.tsx").write_text(LOGO, encoding="utf-8")
    # User-facing text everywhere else: string literals only, never identifiers.
    subprocess.run(["node", str(Path(__file__).with_name("rebrand_strings.mjs")), str(source)], check=True)


def install(source: Path) -> None:
    # Before branding: rebrand_strings.mjs parses the source with the checkout's own typescript.
    subprocess.run([*BUN, "install", "--frozen-lockfile"], cwd=source, check=True)


def build(source: Path, output: Path, terminal_only: bool) -> None:
    env = {**os.environ, "OPENCODE_CHANNEL": "task-factory", "OPENCODE_VERSION": "2.3.2"}
    flags = ["--single", "--skip-install"]
    if terminal_only:
        flags.append("--skip-web-ui")
    subprocess.run(
        [*BUN, "run", "script/build.ts", *flags],
        cwd=source / "packages/cli", env=env, check=True,
    )
    name = "opencode.exe" if os.name == "nt" else "opencode"
    artifact, = (source / "packages/cli/dist").glob(f"*/bin/{name}")
    output.mkdir(parents=True, exist_ok=True)
    installed = output / (ENGINE + (".exe" if os.name == "nt" else ""))
    temporary = installed.with_suffix(".new")
    shutil.copy2(artifact, temporary)
    temporary.replace(installed)
    shutil.copy2(source / "LICENSE", output / "THIRD-PARTY-LICENSE")  # MIT: keep the notice
    (output / "LICENSE-OpenCode").unlink(missing_ok=True)  # name used by earlier builds
    print(installed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="engine source folder (cloned at the pinned release if missing)")
    parser.add_argument("--brand-only", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--with-web-ui", action="store_true",
                        help="also bundle the browser UI (its text is not rebranded)")
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    parser.add_argument("--output", type=Path, default=data / PACKAGE / "bin")
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.exists():
        subprocess.run(["git", "clone", "--depth", "1", "--branch", UPSTREAM_TAG, UPSTREAM_REPO, str(source)],
                       check=True)
    if not args.skip_install:
        install(source)
    brand(source)
    if not args.brand_only:
        build(source, args.output, terminal_only=not args.with_web_ui)


if __name__ == "__main__":
    main()
