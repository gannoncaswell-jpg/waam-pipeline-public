import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ESLint, Linter } from "eslint";
import { browserLanguage, correctnessRules } from "../eslint.config.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const eslint = new ESLint({ cwd: ROOT, overrideConfigFile: path.join(ROOT, "eslint.config.mjs") });

function lintScriptBlocks(page) {
  const checker = new Linter();
  const preceding = {};
  return page.scripts.map((block, index) => {


    const messages = checker.verify(block.code, {
      languageOptions: {
        ...browserLanguage, globals: { ...browserLanguage.globals, ...preceding },
      },
      linterOptions: { reportUnusedDisableDirectives: "error" },
      rules: correctnessRules,
    });
    const source = checker.getSourceCode();
    if (source) {
      for (const variable of source.scopeManager.globalScope.variables) {
        if (variable.defs.length) preceding[variable.name] = "writable";
      }
    }
    return {
      filePath: path.join(ROOT, "generated", `${page.name}.script-${index + 1}.js`),
      messages, suppressedMessages: [],
      errorCount: messages.filter(item => item.severity === 2).length,
      warningCount: messages.filter(item => item.severity === 1).length,
      fatalErrorCount: messages.filter(item => item.fatal).length,
      fixableErrorCount: 0, fixableWarningCount: 0,
    };
  });
}

async function selfTest() {
  const expected = JSON.parse(await readFile(path.join(ROOT, "package.json"), "utf8"));
  assert.equal(ESLint.version, expected.devDependencies.eslint,
    "ESLint version differs from package.json; run npm ci");
  const installedGlobals = JSON.parse(await readFile(path.join(ROOT, "node_modules", "globals", "package.json"), "utf8"));
  assert.equal(installedGlobals.version, expected.devDependencies.globals,
    "Browser globals version differs from package.json; run npm ci");
  const cases = [
    ["missingWaamIdentifier();", "no-undef"],
    ["console.log(tooSoon); const tooSoon = 1;", "no-use-before-define"],
    ["function broken() { return 1; console.log('unreachable'); }", "no-unreachable"],
    ["const broken = ;", null],
  ];
  for (const [code, expected] of cases) {
    const [result] = await eslint.lintText(code, { filePath: "generated/negative-control.js" });
    assert(result.errorCount > 0 && result.messages.some(item => item.ruleId === expected),
      `JavaScript lint failed to detect negative control: ${expected ?? "syntax"}`);
  }
  const [valid] = await eslint.lintText("const ready = 1; console.log(ready);", {
    filePath: "generated/positive-control.js",
  });
  assert.equal(valid.errorCount + valid.warningCount, 0, "Valid JavaScript control failed lint");
  const syntax = new Linter();
  assert(syntax.verify("const cutOff =", { languageOptions: browserLanguage }).some(item => item.fatal),
    "An incomplete script block must fail even when a later block could finish it");
  const page = { name: "cross-script-control", scripts: [
    { code: "later();" }, { code: "function later() {}" },
  ] };
  assert(lintScriptBlocks(page)[0].messages.some(item => item.ruleId === "no-undef"),
    "A later script declaration must not hide an earlier script's missing function");
  page.scripts.reverse();
  assert(lintScriptBlocks(page).every(result => result.errorCount === 0),
    "A preceding script's declarations must remain available to later scripts");
}

async function sourceFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name === "_vendor" || entry.name === "node_modules") continue;
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await sourceFiles(full));
    else if (/\.(?:js|mjs|cjs)$/.test(entry.name)) files.push(full);
  }
  return files;
}

function parseArguments() {
  let python = process.env.PYTHON || "python";
  let onlySelfTest = false;
  for (let index = 2; index < process.argv.length; index++) {
    const argument = process.argv[index];
    if (argument === "--python" && process.argv[index + 1]) python = process.argv[++index];
    else if (argument === "--self-test") onlySelfTest = true;
    else throw new Error(`Unknown or incomplete argument: ${argument}`);
  }
  return { python, onlySelfTest };
}

async function main() {
  const { python, onlySelfTest } = parseArguments();
  await selfTest();
  if (onlySelfTest) {
    console.log("ESLint: negative and positive controls passed.");
    return;
  }
  const rendered = spawnSync(python, [path.join(ROOT, "tools", "lint_pages.py")], {
    cwd: ROOT, encoding: "utf8", maxBuffer: 12 * 1024 * 1024, timeout: 120000,
  });
  if (rendered.error) throw rendered.error;
  if (rendered.status !== 0) throw new Error(`Page rendering failed:\n${rendered.stderr || rendered.stdout}`);
  const manifest = JSON.parse(rendered.stdout);
  assert(Array.isArray(manifest.pages) && manifest.pages.length > 0, "No generated pages to lint");
  assert.deepEqual(manifest.embedded_only, ["src/waam_pipeline/studio_controls.js"]);
  const embedded = new Set(manifest.embedded_only.map(item => path.resolve(ROOT, item)));
  const files = [
    ...await sourceFiles(path.join(ROOT, "src", "waam_pipeline")),
    ...await sourceFiles(path.join(ROOT, "tools")),
    path.join(ROOT, "eslint.config.mjs"),
  ].filter(file => !embedded.has(file));
  assert(files.length > 0, "No first-party JavaScript files to lint");
  const results = await eslint.lintFiles(files);
  let blockCount = 0;
  for (const page of manifest.pages) {
    assert(page.scripts.length > 0, `${page.name}: no executable scripts`);
    results.push(...lintScriptBlocks(page));
    blockCount += page.scripts.length;


    const scripts = page.scripts.map((block, index) =>
      `// ${page.name}: script ${index + 1}, rendered HTML line ${block.line}\n${block.code}\n;`);
    for (const [index, handler] of page.handlers.entries()) {
      scripts.push(`function __waamInlineHandler${index}(event) {\n${handler}\n}`);
    }
    results.push(...await eslint.lintText(scripts.join("\n"), {
      filePath: `generated/${page.name}.js`,
    }));
  }
  const failed = results.some(result => result.errorCount || result.warningCount);
  if (failed) {
    console.error(await (await eslint.loadFormatter("stylish")).format(results));
    process.exitCode = 1;
  } else {
    console.log(`ESLint: ${files.length} files and ${blockCount} script blocks across ${manifest.pages.length} rendered pages passed.`);
  }
}

main().catch(error => {
  console.error(`JavaScript checks failed: ${error.message}`);
  process.exitCode = 2;
});
