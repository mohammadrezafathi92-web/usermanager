#!/usr/bin/env node
/**
 * Refuses to let a component be USED without being imported.
 *
 * Reported 2026-09-17, the third white page of this project: «دوباره صفحه
 * کاربران لود نمیشه». An icon was added to a page - `icon: ShieldAlert` in
 * a lookup table and `<ShieldAlert size={16} />` in a button - and the
 * import line was never updated. Vite builds that happily: JSX compiles to
 * `React.createElement(ShieldAlert, ...)`, which is valid JavaScript right
 * up until the moment it runs, when it throws ReferenceError and React
 * unmounts the whole tree. A blank page, a green build, and a stack trace
 * only visible in a console nobody had open.
 *
 * What makes it worth a check rather than more care: the failure is
 * invisible to every other gate. The build passes, the file is valid, the
 * grep for the name succeeds (it IS in the file - as the usage), and the
 * page that breaks may not be the page anyone was testing.
 *
 * Deliberately narrow, for the same reason check-hooks.mjs is: this
 * project has no eslint setup, and adding one is a much bigger change than
 * the one rule that has now cost three blank pages.
 *
 * Run:  npm run check:imports   (also part of `npm run build`)
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const roots = ["src/pages", "src/components"];
const problems = [];

// Names that are in scope without being imported or declared.
const GLOBALS = new Set([
  "React", "Fragment", "window", "document", "console", "Math", "Object",
  "Array", "String", "Number", "Boolean", "Date", "JSON", "Promise", "Set",
  "Map", "URL", "URLSearchParams", "FormData", "Intl", "Error", "RegExp",
  "Infinity", "NaN", "File", "Blob", "Image", "Audio", "AbortController",
  "IntersectionObserver", "ResizeObserver", "Notification", "WebSocket",
]);

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path);
    else if (entry.endsWith(".jsx")) scan(path);
  }
}

/** Every name this file brings into scope: imports, and its own declarations. */
function declaredNames(source) {
  const names = new Set();

  // import X, { A as B, C } from "..."  /  import * as NS from "..."
  for (const match of source.matchAll(/import\s+([^;]+?)\s+from\s+["'][^"']+["']/g)) {
    const clause = match[1];
    const braces = clause.match(/\{([^}]*)\}/);
    if (braces) {
      for (const part of braces[1].split(",")) {
        const name = part.trim().split(/\s+as\s+/).pop().trim();
        if (name) names.add(name);
      }
    }
    // The default and namespace bindings, i.e. everything outside the braces.
    const outside = clause.replace(/\{[^}]*\}/, "").replace(/\*\s+as\s+/, "");
    for (const part of outside.split(",")) {
      const name = part.trim();
      if (name && /^[A-Za-z_$][\w$]*$/.test(name)) names.add(name);
    }
  }

  // Anything the file declares itself.
  for (const match of source.matchAll(/(?:^|\n)\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_$][\w$]*)/g)) {
    names.add(match[1]);
  }
  for (const match of source.matchAll(/(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) {
    names.add(match[1]);
  }
  // Destructured consts: const { A, B } = ... and const [A, B] = ...
  for (const match of source.matchAll(/(?:const|let|var)\s*[{[]([^}\]]*)[}\]]\s*=/g)) {
    for (const part of match[1].split(",")) {
      const name = part.trim().split(":").pop().trim().replace(/^\.\.\./, "");
      if (/^[A-Za-z_$][\w$]*$/.test(name)) names.add(name);
    }
  }
  // Function parameters, which is how a component receives an icon:
  //   function IconTile({ icon: Icon }) { ... <Icon /> ... }
  for (const match of source.matchAll(/\(([^)]*)\)\s*(?:=>|\{)/g)) {
    for (const part of match[1].split(",")) {
      const name = part.trim().split(":").pop().trim().replace(/^\.\.\./, "").split("=")[0].trim();
      if (/^[A-Za-z_$][\w$]*$/.test(name)) names.add(name);
    }
  }
  return names;
}

/**
 * Comments, blanked out - keeping the line count, so reported line numbers
 * still point at the real line.
 *
 * Found immediately: StatCard.jsx documents its own two call styles with
 * `icon={<Wallet size={18} />}` in a JSX comment, and the first version of
 * this check reported it as a missing import. A checker that cries wolf on
 * a comment gets switched off, which is worse than not having it.
 *
 * `//` is left alone when it follows a colon, so a URL inside a string is
 * not mistaken for a comment. Over-blanking could only ever HIDE a usage,
 * never invent one, so erring that way is the safe direction.
 */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/(^|[^:])\/\/[^\n]*/g, (m, before) => before + " ".repeat(m.length - before.length));
}


function scan(path) {
  const raw = readFileSync(path, "utf8");
  const source = stripComments(raw);
  const known = declaredNames(raw);
  const seen = new Set();

  const report = (name, line, how) => {
    if (seen.has(name) || known.has(name) || GLOBALS.has(name)) return;
    seen.add(name);
    problems.push(`${path}:${line}  <${name}> is used but never imported (${how})`);
  };

  const lineOf = (index) => source.slice(0, index).split("\n").length;

  // Used as a JSX element: <Foo ...>. Capitalised only - a lowercase tag is
  // an HTML element, not a component.
  for (const match of source.matchAll(/<([A-Z][\w$]*)[\s/>]/g)) {
    report(match[1], lineOf(match.index), "JSX element");
  }
  // Passed as a component value, which is how every icon in this codebase
  // travels: `icon: ShieldAlert` in a lookup table, rendered elsewhere.
  for (const match of source.matchAll(/\bicon:\s*([A-Z][\w$]*)/g)) {
    report(match[1], lineOf(match.index), "icon reference");
  }
}

for (const root of roots) walk(root);

if (problems.length) {
  console.error("\nA component used without being imported builds fine and throws at runtime,");
  console.error("which renders a blank page. Add it to the import, or remove the usage.\n");
  for (const problem of problems) console.error("  " + problem + "\n");
  process.exit(1);
}
console.log(`imports ok - every component used in ${roots.join(", ")} is in scope`);
