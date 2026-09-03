/**
 * Mechanical detectors for the bug classes this repo has actually shipped.
 *
 * Every one of these was first found by hand, at the cost of a broken open or a
 * silent button. They are all cheap to detect with a real parser, so they are
 * detected here instead of being found again.
 *
 *   D1  two functions of one name in one scope — the later silently wins, and
 *       every call site above it changes behaviour. Shipped: `isVisible`.
 *   D3  a flag that is read but never assigned anything truthy, so the branch
 *       guarding on it cannot run. Shipped: `seatState.awaitingPayment`.
 *   D5  an async function called without await, void or .catch — its rejection
 *       becomes an unhandled promise rejection and nothing else. Shipped:
 *       `runArmScheduler`, which left `armState.running` stuck true and
 *       disabled 대기 시작 for the life of the page.
 *
 * D1/D3/D5 gate: a finding exits non-zero. D6/D8/D9 are advisory (--all),
 * because "unused" and "ignored" have legitimate instances here.
 *
 * Needs an ES parser. acorn is not a dependency of this repo, so it is resolved
 * from wherever it happens to exist; without it this exits 2 and the test that
 * runs it skips rather than pretending to pass.
 */
import { readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { execSync } from "node:child_process";

const CANDIDATES = [];
try {
  const root = execSync("npm root -g", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  for (const pkg of readdirSync(root)) {
    CANDIDATES.push(`${root}/${pkg}/node_modules/acorn/`);
    if (pkg.startsWith("@")) {
      for (const scoped of readdirSync(`${root}/${pkg}`)) {
        CANDIDATES.push(`${root}/${pkg}/${scoped}/node_modules/acorn/`);
      }
    }
  }
} catch { /* no global npm; fall through to the local candidates */ }
CANDIDATES.push(`${process.cwd()}/node_modules/acorn/`, import.meta.url);

let acorn = null;
for (const base of CANDIDATES) {
  try { acorn = createRequire(base)("acorn"); break; } catch { /* try the next */ }
}
if (!acorn) {
  console.error("audit_js: no ES parser available (looked for acorn). Skipping.");
  process.exit(2);
}

const FILE = process.argv[2] ?? "browser/nolsniper_autopilot.js";
const ALL = process.argv.includes("--all");
const src = readFileSync(FILE, "utf8");
const ast = acorn.parse(src, { ecmaVersion: 2023, locations: true, sourceType: "script" });

const nodes = [];
(function walk(node, parent) {
  if (!node || typeof node.type !== "string") return;
  node.__parent = parent;
  nodes.push(node);
  for (const key of Object.keys(node)) {
    if (key === "__parent" || key === "loc") continue;
    const value = node[key];
    if (Array.isArray(value)) value.forEach((c) => c && typeof c.type === "string" && walk(c, node));
    else if (value && typeof value.type === "string") walk(value, node);
  }
})(ast, null);

const of = (type) => nodes.filter((n) => n.type === type);
const lineOf = (n) => n.loc.start.line;
const FN = ["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"];
const scopeOf = (n) => {
  let p = n.__parent;
  while (p && p.type !== "Program" && !FN.includes(p.type)) p = p.__parent;
  return p;
};

// References inside the debug-export object are not uses.
const exportAssign = of("AssignmentExpression").find(
  (n) => src.slice(n.left.start, n.left.end) === "window.NOLSniper",
);
const EX = exportAssign ? [exportAssign.right.start, exportAssign.right.end] : [-1, -1];
const inExports = (n) => n.start >= EX[0] && n.end <= EX[1];

const gating = [];
const advisory = [];
const emit = (list, detector, message) => list.push(`${detector}  ${message}`);

// ---- D1 ----
const byScope = new Map();
for (const fn of of("FunctionDeclaration")) {
  const scope = scopeOf(fn);
  const key = `${scope ? scope.start : "root"}::${fn.id.name}`;
  if (!byScope.has(key)) byScope.set(key, []);
  byScope.get(key).push(fn);
}
for (const [key, group] of byScope) {
  if (group.length > 1) {
    emit(gating, "D1", `${key.split("::")[1]} declared ${group.length}x in one scope (lines ${group.map(lineOf).join(", ")}) — the last one wins everywhere`);
  }
}

// ---- D3 ----
for (const obj of ["seatState", "armState", "clockState"]) {
  const reads = new Map();
  const truthyWrite = new Set();
  const sawWrite = new Set();
  for (const n of of("MemberExpression")) {
    if (n.object?.type !== "Identifier" || n.object.name !== obj) continue;
    const field = n.property?.name;
    if (!field) continue;
    const p = n.__parent;
    const isWrite =
      (p?.type === "AssignmentExpression" && p.left === n) ||
      (p?.type === "UpdateExpression" && p.argument === n);
    if (!isWrite) { reads.set(field, (reads.get(field) ?? 0) + 1); continue; }
    sawWrite.add(field);
    if (p.type === "UpdateExpression" || p.operator !== "=") { truthyWrite.add(field); continue; }
    const v = p.right;
    const falsy = (v.type === "Literal" && !v.value) || (v.type === "Identifier" && v.name === "undefined");
    if (!falsy) truthyWrite.add(field);
  }
  for (const field of sawWrite) {
    if (!truthyWrite.has(field) && reads.get(field)) {
      emit(gating, "D3", `${obj}.${field} is read ${reads.get(field)}x but never assigned anything truthy — the branch guarding on it cannot run`);
    }
  }
}

// ---- D5 ----
const asyncNames = new Set(of("FunctionDeclaration").filter((f) => f.async).map((f) => f.id.name));
for (const call of of("CallExpression")) {
  if (call.callee?.type !== "Identifier" || !asyncNames.has(call.callee.name)) continue;
  if (inExports(call)) continue;
  const p = call.__parent;
  if (p?.type !== "ExpressionStatement") continue;   // awaited, returned, chained
  emit(gating, "D5", `${call.callee.name}() at line ${lineOf(call)} is async and its promise is dropped — a rejection goes nowhere`);
}

// ---- D6 (advisory): referenced only by the export, and not by the panel ----
let pythonSource = "";
for (const dir of ["mac", "core"]) {
  try {
    for (const f of readdirSync(dir)) if (f.endsWith(".py")) pythonSource += readFileSync(`${dir}/${f}`, "utf8");
  } catch { /* directory absent */ }
}
for (const fn of of("FunctionDeclaration")) {
  const name = fn.id.name;
  const used = of("Identifier").some((id) => id.name === name && id !== fn.id && !inExports(id));
  if (used) continue;
  if (pythonSource.includes(name)) continue;         // called across the bridge
  emit(advisory, "D6", `${name} (line ${lineOf(fn)}) is referenced only by the debug export and never by the panel`);
}

// ---- D8 (advisory): a return value dropped at every call site ----
for (const fn of of("FunctionDeclaration")) {
  const returnsValue = nodes.some((n) => n.type === "ReturnStatement" && n.argument && scopeOf(n) === fn);
  if (!returnsValue) continue;
  const calls = of("CallExpression").filter(
    (n) => n.callee?.type === "Identifier" && n.callee.name === fn.id.name && !inExports(n));
  if (!calls.length) continue;
  const anyUsed = calls.some((n) => {
    if (n.__parent?.type === "AwaitExpression") return true;   // a deliberate wait
    return n.__parent && n.__parent.type !== "ExpressionStatement";
  });
  if (!anyUsed) emit(advisory, "D8", `${fn.id.name} (line ${lineOf(fn)}) returns a value that all ${calls.length} call site(s) discard`);
}

// ---- D9 (advisory): a catch that only logs ----
for (const clause of of("CatchClause")) {
  const body = clause.body.body;
  if (!body.length) { emit(advisory, "D9", `empty catch at line ${lineOf(clause)}`); continue; }
  const onlyLogs = body.every((s) =>
    s.type === "ExpressionStatement" && s.expression.type === "CallExpression" &&
    ["log", "console"].includes(
      s.expression.callee.type === "Identifier"
        ? s.expression.callee.name
        : s.expression.callee.object?.name ?? ""));
  if (onlyLogs) emit(advisory, "D9", `catch at line ${lineOf(clause)} only logs`);
}

for (const row of gating) console.log(row);
if (ALL) for (const row of advisory) console.log(row);
console.log(`\n${FILE}: ${gating.length} gating finding(s), ${advisory.length} advisory`);
process.exit(gating.length ? 1 : 0);
