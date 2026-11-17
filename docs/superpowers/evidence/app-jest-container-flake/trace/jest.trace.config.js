// Jest config for measuring where Babel transform time lands.
// Same as app/jest.config.js, but the JS transformer is wrapped so every
// transform is logged, and each test's start and end are marked.
// Env: APP_DIR (absolute app/ path), TRACE_OUT (log file), NO_PRELOAD=1 to
// drop app/jest.preload.js and measure the pre-fix behaviour.
const path = require("path");
const APP = process.env.APP_DIR;
const base = require(path.join(APP, "node_modules/jest-expo/jest-preset"));
const cfg = require(path.join(APP, "jest.config.js"));
const abs = (p) => p.replace("<rootDir>", APP);
const JS = "\\.[jt]sx?$";

module.exports = {
  ...cfg,
  rootDir: APP,
  setupFiles: process.env.NO_PRELOAD ? [] : (cfg.setupFiles || []).map(abs),
  setupFilesAfterEnv: [
    ...cfg.setupFilesAfterEnv.map(abs),
    path.join(__dirname, "marks.js"),
  ],
  transform: Object.fromEntries(
    Object.entries(base.transform).map(([k, v]) =>
      k === JS
        ? [
            k,
            [
              path.join(__dirname, "tracing-transformer.js"),
              Array.isArray(v) ? v[1] : {},
            ],
          ]
        : [k, v],
    ),
  ),
};
