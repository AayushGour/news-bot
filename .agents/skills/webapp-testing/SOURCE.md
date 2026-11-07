Vendored from https://github.com/anthropics/skills (skills/webapp-testing), commit 0a64e39, Apache-2.0 (LICENSE.txt).
Content unmodified. scripts/with_server.py manages server lifecycle (start, wait-on-port, run command, clean up); examples/ are Playwright patterns (console capture, element discovery, file:// automation). Requires playwright installed in the target project.
Primary user: tester (FE/blackbox layer — screenshots, console logs, DOM state as pasteable evidence).
