/* eslint-disable @typescript-eslint/no-require-imports */
// Compile test imports with the project's existing TypeScript; no runtime dependency added.
const fs = require("node:fs");
const ts = require("typescript");

function installTsHook() {
  const previous = [require.extensions[".ts"], require.extensions[".tsx"]];
  const load = (module, filename) => {
    const result = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      fileName: filename,
      compilerOptions: {
        target: ts.ScriptTarget.ES2020,
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
      },
    });
    module._compile(result.outputText, filename);
  };
  require.extensions[".ts"] = load;
  require.extensions[".tsx"] = load;
  return () => {
    for (const [index, extension] of [".ts", ".tsx"].entries()) {
      if (previous[index]) require.extensions[extension] = previous[index];
      else delete require.extensions[extension];
    }
  };
}

module.exports = { installTsHook };
