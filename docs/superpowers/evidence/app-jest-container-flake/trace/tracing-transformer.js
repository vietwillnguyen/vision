// Wraps babel-jest; logs each transformed file with wall time and duration.
const fs = require("fs");
const path = require("path");
const babelJest = require(
  path.join(process.env.APP_DIR, "node_modules/babel-jest"),
).default;
const OUT = process.env.TRACE_OUT;
module.exports = {
  createTransformer(opts) {
    const t = babelJest.createTransformer(opts);
    return {
      ...t,
      process(src, file, o) {
        const s = Date.now();
        const r = t.process(src, file, o);
        fs.appendFileSync(
          OUT,
          `${s} ${Date.now() - s} ${process.pid} ${file}\n`,
        );
        return r;
      },
      processAsync: undefined,
    };
  },
};
