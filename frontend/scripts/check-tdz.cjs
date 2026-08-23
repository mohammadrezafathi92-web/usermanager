/**
 * Finds `const`/`let` bindings that are READ before the line that declares
 * them, inside the same function.
 *
 * JavaScript calls this the temporal dead zone: the syntax is valid, so a
 * parser is happy, but the read throws at runtime. In a React component
 * that means the render throws and the page displays nothing at all - no
 * error, no partial UI, just a blank section. Exactly how the Packages page
 * stopped opening after a helper was added above the useState it read.
 *
 * A plain parse check cannot see this, which is why it shipped. This does.
 *
 *   node scripts/check-tdz.cjs            # every page/component
 *   node scripts/check-tdz.cjs src/x.jsx  # one file
 */
const fs = require("fs");
const path = require("path");
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (/\.(jsx?|tsx?)$/.test(entry.name)) out.push(full);
  }
  return out;
}

function check(file) {
  const code = fs.readFileSync(file, "utf8");
  let ast;
  try {
    ast = parser.parse(code, { sourceType: "module", plugins: ["jsx"] });
  } catch (err) {
    return [{ file, line: err.loc ? err.loc.line : 0, name: "(parse error)", detail: err.message }];
  }

  const problems = [];
  traverse(ast, {
    ReferencedIdentifier(refPath) {
      const { node, scope } = refPath;
      const binding = scope.getBinding(node.name);
      if (!binding) return;
      if (binding.kind !== "const" && binding.kind !== "let") return;

      // Only compare positions when both live in the SAME function body -
      // a closure that runs later (an event handler, a useEffect, a
      // callback) legitimately reads a binding declared below it.
      const declFn = binding.path.getFunctionParent();
      const refFn = refPath.getFunctionParent();
      if (declFn !== refFn) return;

      const declLine = binding.path.node.loc && binding.path.node.loc.start.line;
      const refLine = node.loc && node.loc.start.line;
      if (!declLine || !refLine || refLine >= declLine) return;

      problems.push({
        file,
        line: refLine,
        name: node.name,
        detail: `read on line ${refLine}, declared on line ${declLine}`,
      });
    },
  });
  return problems;
}

const targets = process.argv.slice(2);
const files = targets.length ? targets : walk(path.join(__dirname, "..", "src"));
const all = files.flatMap(check);

if (all.length === 0) {
  console.log(`TDZ check: no use-before-declaration in ${files.length} files`);
  process.exit(0);
}
for (const p of all) {
  console.log(`FAIL ${path.relative(process.cwd(), p.file)}:${p.line}  ${p.name} - ${p.detail}`);
}
console.log(`\n${all.length} problem(s) - each of these throws at runtime and blanks the page.`);
process.exit(1);
