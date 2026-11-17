const fs = require("fs");
beforeEach(() =>
  fs.appendFileSync(
    process.env.TRACE_OUT,
    `${Date.now()} 0 ${process.pid} TEST_START ${expect.getState().currentTestName}\n`,
  ),
);
afterEach(() =>
  fs.appendFileSync(
    process.env.TRACE_OUT,
    `${Date.now()} 0 ${process.pid} TEST_END ${expect.getState().currentTestName}\n`,
  ),
);
