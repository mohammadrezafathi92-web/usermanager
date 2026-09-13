#!/usr/bin/env node
/**
 * Every translation key must appear once per language, and in both.
 *
 * 2026-09-13: a new key was inserted twice into the Persian table - the
 * Persian text first, then the English - and JavaScript object literals let
 * the later one win, so a Persian screen quietly showed an English label.
 * Nothing catches that: the file is valid, the build succeeds, and the only
 * symptom is a sentence in the wrong language on one panel in one place.
 *
 * Same reasoning as check-hooks.mjs beside it - a small scanner for a
 * mistake that has actually happened, rather than a linting framework.
 */
import { readFileSync } from "node:fs";

const src = readFileSync("src/i18n/translations.js", "utf8");
const lines = src.split("\n");

// The file is one object per language, each opened by `<lang>: {`.
const tables = [];
let current = null;
lines.forEach((line, i) => {
  const opener = line.match(/^\s{2}(\w+):\s*\{\s*$/);
  if (opener) {
    current = { lang: opener[1], keys: new Map(), line: i + 1 };
    tables.push(current);
    return;
  }
  if (!current) return;
  if (/^\s{2}\},?\s*$/.test(line)) {
    current = null;
    return;
  }
  // Any indentation: a mis-indented entry is still a live key, and
  // treating it as absent reports a missing translation that is not.
  const entry = line.match(/^\s+"([^"]+)"\s*:/);
  if (entry) {
    const key = entry[1];
    current.keys.set(key, (current.keys.get(key) || 0) + 1);
  }
});

const problems = [];
for (const table of tables) {
  for (const [key, count] of table.keys) {
    if (count > 1) {
      problems.push(`"${key}" appears ${count} times in \`${table.lang}\` - the last one silently wins`);
    }
  }
}

// ...and no language may be missing a key another language has.
if (tables.length >= 2) {
  const [first, ...rest] = tables;
  for (const other of rest) {
    for (const key of first.keys.keys()) {
      if (!other.keys.has(key)) problems.push(`"${key}" is in \`${first.lang}\` but not \`${other.lang}\``);
    }
    for (const key of other.keys.keys()) {
      if (!first.keys.has(key)) problems.push(`"${key}" is in \`${other.lang}\` but not \`${first.lang}\``);
    }
  }
}

if (problems.length) {
  console.error("\nTranslation tables disagree:\n");
  for (const problem of problems) console.error("  " + problem);
  console.error("");
  process.exit(1);
}
console.log(`i18n ok - ${tables.map((t) => `${t.lang}: ${t.keys.size}`).join(", ")} keys, no duplicates`);
