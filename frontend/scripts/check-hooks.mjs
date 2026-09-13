#!/usr/bin/env node
/**
 * Refuses to let a React hook be declared after an early return.
 *
 * Reported 2026-09-13: every customer's page went white. The cause was one
 * `const [resetTarget, setResetTarget] = useState(null)` placed next to the
 * handler that used it - which happened to be below this component's
 * `if (!user) return <loading/>`. React counts hooks per render, so on the
 * first render (user still loading) it saw N, and the moment the user
 * arrived it saw N+1 and threw. Nothing catches that: the build succeeds,
 * the file is valid JavaScript, and the only symptom is a blank screen.
 *
 * That is the second white page of this kind in this project (the first was
 * a component invoked with a built element instead of a component), so it
 * gets a check rather than a resolution to be careful.
 *
 * Deliberately a small scanner rather than eslint-plugin-react-hooks: the
 * project has no eslint setup at all, and adding one - config, plugins,
 * a lockfile's worth of dependencies, and a first run that reports hundreds
 * of pre-existing warnings nobody will read - is a much bigger change than
 * the one rule that has actually cost us anything twice.
 *
 * Run:  npm run check:hooks   (also part of `npm run build`)
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const HOOK = /(?:^|[^.\w])(useState|useEffect|useMemo|useCallback|useRef|useReducer|useContext|useLayoutEffect)\s*\(/;
// An early return at the top level of a component body. Indentation is the
// signal: two spaces means "directly inside the function", which is where a
// guard clause lives. A `return` nested deeper belongs to a callback and
// says nothing about hook order.
const EARLY_RETURN = /^ {2}(?:if \([^)]*\)\s*(?:return|\{)|return )/;
const COMPONENT_START = /^export default function [A-Z]/;

const roots = ["src/pages", "src/components"];
const problems = [];

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path);
    else if (entry.endsWith(".jsx")) scan(path);
  }
}

function scan(path) {
  const lines = readFileSync(path, "utf8").split("\n");
  let inComponent = false;
  let returnedAt = 0;

  lines.forEach((line, i) => {
    if (COMPONENT_START.test(line)) {
      inComponent = true;
      returnedAt = 0;
      return;
    }
    if (!inComponent) return;
    // End of the top-level function body.
    if (/^\}/.test(line)) {
      inComponent = false;
      return;
    }
    if (!returnedAt && EARLY_RETURN.test(line)) {
      returnedAt = i + 1;
      return;
    }
    // The component's own final `return (` is not an early return - but by
    // then we have already recorded the first one, and any hook after THAT
    // is the bug regardless.
    if (returnedAt && HOOK.test(line) && /^\s{2}const|^\s{2}use/.test(line)) {
      problems.push(
        `${path}:${i + 1}  hook declared after the early return on line ${returnedAt}\n` +
          `    ${line.trim()}`
      );
    }
  });
}

for (const root of roots) walk(root);

if (problems.length) {
  console.error("\nReact hooks must run on every render, so none may sit below an early return.");
  console.error("A component that breaks this renders a blank page the moment the guard stops firing.\n");
  for (const problem of problems) console.error("  " + problem + "\n");
  process.exit(1);
}
console.log(`hooks ok - no hook sits below an early return in ${roots.join(", ")}`);
