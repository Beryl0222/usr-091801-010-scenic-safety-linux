"use strict";

const { spawnSync } = require("node:child_process");

// 发现所有 test_*.py，交给 unittest discover（保持与旧版一致：失败即非零退出）。
const result = spawnSync(
  "python3",
  ["-m", "unittest", "discover", "-p", "test_*.py", "-v"],
  { stdio: "inherit" },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
