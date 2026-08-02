import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import {
  startBackend,
  startBackendBrowserFixture,
} from "../scripts/wiki32-harness.mjs";

class FakeChild extends EventEmitter {
  constructor({ exitDelayMs = 0 } = {}) {
    super();
    this.exitCode = null;
    this.signalCode = null;
    this.stderr = new EventEmitter();
    this.exitDelayMs = exitDelayMs;
    this.exitEvents = 0;
    this.killCalls = [];
  }

  kill(signal) {
    this.killCalls.push(signal);
    setTimeout(() => this.exit(null, signal), this.exitDelayMs);
    return true;
  }

  exit(code, signal = null) {
    if (this.exitCode !== null || this.signalCode !== null) return;
    this.exitCode = code;
    this.signalCode = signal;
    this.exitEvents += 1;
    this.emit("exit", code, signal);
  }
}

function fixtures() {
  return {
    registryPath: "/tmp/wiki-240-registry.json",
    statusDir: "/tmp/wiki-240-status",
    queuePath: "/tmp/wiki-240-queue.json",
    sessionsDir: "/tmp/wiki-240-sessions",
    supervisorSocketPath: "/tmp/wiki-240-supervisor.sock",
  };
}

function within(promise, timeoutMs = 500) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("fixture cleanup timed out")), timeoutMs);
    }),
  ]);
}

function readyBackend(child) {
  return startBackend(fixtures(), {
    chooseBackendPort: async () => 43103,
    spawnProcess: () => child,
    waitForReady: async () => {},
  });
}

function assertReaped(child) {
  assert.deepEqual(child.killCalls, ["SIGTERM"]);
  assert.equal(child.signalCode, "SIGTERM");
  assert.equal(child.exitEvents, 1);
}

test("health failure terminates and awaits the spawned backend", async () => {
  const child = new FakeChild({ exitDelayMs: 20 });
  await assert.rejects(
    within(startBackend(fixtures(), {
      chooseBackendPort: async () => 43101,
      spawnProcess: () => child,
      waitForReady: async () => {
        throw new Error("fixture health failed");
      },
    })),
    /fixture health failed/,
  );

  assertReaped(child);
});

test("stop is idempotent when the backend exited before cleanup", async () => {
  const child = new FakeChild();
  const backend = await startBackend(fixtures(), {
    chooseBackendPort: async () => 43102,
    spawnProcess: () => child,
    waitForReady: async () => {},
  });
  child.stderr.emit("data", "fixture backend exited early");
  child.exit(17);

  const firstStop = backend.stop();
  const secondStop = backend.stop();
  assert.strictEqual(secondStop, firstStop);
  await assert.rejects(within(firstStop), /fixture backend exited early/);
  assert.deepEqual(child.killCalls, []);
  assert.equal(child.exitEvents, 1);
});

test("browser launch failure still stops and reaps the backend", async () => {
  const child = new FakeChild({ exitDelayMs: 10 });
  await assert.rejects(
    within(startBackendBrowserFixture({
      startBackendProcess: () => readyBackend(child),
      launchBrowser: async () => {
        throw new Error("browser launch failed");
      },
      createPage: async () => {
        throw new Error("page creation must not run");
      },
    })),
    /browser launch failed/,
  );
  assertReaped(child);
});

test("newPage failure closes the browser and reaps the backend", async () => {
  const child = new FakeChild({ exitDelayMs: 10 });
  let browserCloseCalls = 0;
  const browser = {
    async close() {
      browserCloseCalls += 1;
    },
  };
  await assert.rejects(
    within(startBackendBrowserFixture({
      startBackendProcess: () => readyBackend(child),
      launchBrowser: async () => browser,
      createPage: async () => {
        throw new Error("newPage failed");
      },
    })),
    /newPage failed/,
  );
  assert.equal(browserCloseCalls, 1);
  assertReaped(child);
});

test("page cleanup failure still closes the browser and reaps the backend", async () => {
  const child = new FakeChild({ exitDelayMs: 10 });
  let pageCloseCalls = 0;
  let browserCloseCalls = 0;
  const page = {
    isClosed: () => false,
    async unrouteAll() {
      throw new Error("page cleanup failed");
    },
    async close() {
      pageCloseCalls += 1;
    },
  };
  const browser = {
    async close() {
      browserCloseCalls += 1;
    },
  };
  const fixture = await startBackendBrowserFixture({
    startBackendProcess: () => readyBackend(child),
    launchBrowser: async () => browser,
    createPage: async () => page,
  });
  await assert.rejects(within(fixture.stop()), /Backend browser fixture cleanup failed/);
  assert.equal(pageCloseCalls, 1);
  assert.equal(browserCloseCalls, 1);
  assertReaped(child);
});

test("browser cleanup failure still reaps the backend", async () => {
  const child = new FakeChild({ exitDelayMs: 10 });
  let pageClosed = false;
  const page = {
    isClosed: () => pageClosed,
    async unrouteAll() {},
    async close() {
      pageClosed = true;
    },
  };
  const browser = {
    async close() {
      throw new Error("browser cleanup failed");
    },
  };
  const fixture = await startBackendBrowserFixture({
    startBackendProcess: () => readyBackend(child),
    launchBrowser: async () => browser,
    createPage: async () => page,
  });
  await assert.rejects(within(fixture.stop()), /Backend browser fixture cleanup failed/);
  assert.equal(pageClosed, true);
  assertReaped(child);
});
