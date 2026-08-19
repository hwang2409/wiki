import fs from "node:fs/promises";

import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-373";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-373-instant-send-");
  const transcript = fixtures.root + "/codex-empty.jsonl";
  await fs.writeFile(
    transcript,
    JSON.stringify({
      type: "event_msg",
      timestamp: new Date().toISOString(),
      payload: { type: "thread_settings_applied" },
    }) + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const sessionEvents = [{
    id: 0,
    kind: "claude_init",
    ts: new Date().toISOString(),
    text: "",
    disposition: "rendered",
    claude_init: { model: "claude-sonnet", cwd: "/tmp/wiki" },
  }, {
    id: 1,
    kind: "tool",
    ts: new Date().toISOString(),
    text: "",
    disposition: "rendered",
    tool: {
      name: "Bash",
      input: "echo streamed",
      output: null,
      ok: null,
      archetype: "bash",
      summary: "echo streamed",
    },
  }];
  let sendCount = 0;
  let patches = [];
  let releaseFirstSend;
  const firstSendReleased = new Promise((resolve) => { releaseFirstSend = resolve; });
  let firstRequestBody;
  let firstFailedRequestId;
  let editedRequestId;
  let ambiguousRequestId;
  let uncertainEditRequestId;
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.setDefaultTimeout(5_000);

  try {
    await page.route("**/api/agents/" + TICKET + "/session?**", async (route) => {
      const cursor = Number(new URL(route.request().url()).searchParams.get("cursor") ?? 0);
      const tailFrom = cursor >= 0 && cursor <= sessionEvents.length ? cursor : 0;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          version: 2,
          format: "claude",
          path: transcript,
          tokens: null,
          model: "claude-sonnet",
          kind: "worker",
          provider: "claude",
          tasks: [],
          session_meta: {},
          dispositions: { rendered: sessionEvents.length, summarized: 0, ignored: 0, unknown: 0 },
          base: 0,
          cursor: sessionEvents.length,
          tail_from: tailFrom,
          events: sessionEvents.slice(tailFrom),
          patches,
          subagents: [],
          queue: [],
          working: true,
        }),
      });
    });

    await page.route("**/api/agents/" + TICKET + "/message", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON();
      if (sendCount === 1) {
        firstRequestBody = body;
        await firstSendReleased;
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: body.text,
          pending_id: body.pending_id,
          disposition: "rendered",
        });
        patches = [{
          id: 1,
          output: "streamed output",
          ok: true,
          completed_at: new Date().toISOString(),
          duration_ms: 12,
          status: "completed",
          partial: false,
        }];
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id }),
        });
        return;
      }
      if (sendCount === 2 || sendCount === 4) {
        if (sendCount === 2) firstFailedRequestId = body.request_id;
        if (sendCount === 4) editedRequestId = body.request_id;
        await route.fulfill({
          status: 400,
          contentType: "application/json",
          body: JSON.stringify({ detail: "fixture send failure" }),
        });
        return;
      }
      if (sendCount === 3) {
        if (body.request_id !== firstFailedRequestId) {
          throw new Error("retry changed the logical message request id");
        }
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: body.text,
          pending_id: body.pending_id,
          disposition: "rendered",
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id }),
        });
        return;
      }
      if (sendCount === 5) {
        if (body.request_id === editedRequestId) {
          throw new Error("editing reused the old logical message request id");
        }
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: body.text,
          pending_id: body.pending_id,
          disposition: "rendered",
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id }),
        });
        return;
      }
      if (sendCount === 6) {
        ambiguousRequestId = body.request_id;
        await delay(16_000);
        try {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ status: "uncertain", pending_id: body.pending_id }),
          });
        } catch {
          // The browser timeout intentionally aborts this ambiguous request.
        }
        return;
      }
      if (sendCount === 7) {
        if (body.request_id !== ambiguousRequestId) {
          throw new Error("ambiguous delivery retry changed the request id");
        }
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: body.text,
          pending_id: body.pending_id,
          disposition: "rendered",
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id }),
        });
        return;
      }
      if (sendCount === 8) {
        uncertainEditRequestId = body.request_id;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "uncertain", pending_id: body.pending_id }),
        });
        return;
      }
      if (sendCount === 9) {
        if (body.request_id === uncertainEditRequestId) {
          throw new Error("editing an uncertain message reused its request id");
        }
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: body.text,
          pending_id: body.pending_id,
          disposition: "rendered",
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id }),
        });
        return;
      }
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "fixture send failure" }),
      });
    });

    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [{
            id: "window-0",
            focusedPaneId: "pane-1",
            layout: { kind: "pane", id: "pane-1", path: "agent://" + ticket },
          }],
        }),
      );
    }, { ticket: TICKET });
    await page.goto(backend.baseUrl + "/#/agent/" + TICKET, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-composer textarea", { timeout: 5_000 });
    if (await page.getByText(/^session started:/).count() !== 0) {
      throw new Error("session started was rendered in the transcript");
    }

    const composer = page.locator(".session-composer textarea");
    await composer.fill("instant hello");
    await composer.press("Enter");
    const pending = page.locator(".session-scroll .session-pending-user", { hasText: "instant hello" });
    await pending.waitFor({ state: "visible", timeout: 5_000 });
    if (!firstRequestBody?.request_id) throw new Error("send did not include a request id");
    if (firstRequestBody.pending_id !== firstRequestBody.request_id) {
      throw new Error("initial send used separate pending and request identities");
    }
    const pendingRow = pending.locator("..");
    const pendingKey = await pendingRow.getAttribute("data-row-key");
    releaseFirstSend();

    const authoritative = page.locator(".session-scroll .session-user:not(.session-pending-user)", { hasText: "instant hello" });
    await authoritative.waitFor({ state: "visible", timeout: 8_000 });
    await pending.waitFor({ state: "detached", timeout: 5_000 });
    if (await authoritative.count() !== 1) throw new Error("authoritative message was duplicated");
    if (await authoritative.locator("..").getAttribute("data-row-key") !== pendingKey) {
      throw new Error("authoritative message changed its transcript row position");
    }
    await page.getByText("streamed output", { exact: true }).waitFor({ state: "visible", timeout: 5_000 });

    await composer.fill("failed hello");
    await composer.press("Enter");
    const failed = page.locator(".session-scroll .session-pending-user", { hasText: "failed hello" });
    await failed.getByText("send failed: fixture send failure").waitFor({ state: "visible", timeout: 5_000 });
    if (!firstFailedRequestId) throw new Error("fixture did not capture failed request id");
    await failed.getByRole("button", { name: "Retry send" }).click();
    await page.getByText("failed hello", { exact: true }).last().waitFor({ state: "visible", timeout: 5_000 });
    if (await page.locator(".session-scroll .session-user", { hasText: "failed hello" }).count() !== 1) {
      throw new Error("retry duplicated the user message");
    }

    await composer.fill("edit me");
    await composer.press("Enter");
    const editable = page.locator(".session-scroll .session-pending-user", { hasText: "edit me" });
    await editable.getByText("send failed: fixture send failure").waitFor({ state: "visible", timeout: 5_000 });
    await editable.getByRole("button", { name: "Edit message" }).click();
    await composer.fill("edited hello");
    await composer.press("Enter");
    await page.getByText("edited hello", { exact: true }).last().waitFor({ state: "visible", timeout: 5_000 });
    if (!editedRequestId || editedRequestId === firstFailedRequestId) {
      throw new Error("fixture did not capture a new request id for the edit");
    }
    if (await page.locator(".session-scroll .session-user", { hasText: "edit me" }).count() !== 0) {
      throw new Error("edited message left the old logical row visible");
    }

    await composer.fill("ambiguous hello");
    await composer.press("Enter");
    const ambiguous = page.locator(".session-scroll .session-pending-user", { hasText: "ambiguous hello" });
    await ambiguous.waitFor({ state: "visible", timeout: 5_000 });
    await ambiguous.getByText("delivery uncertain").waitFor({ state: "visible", timeout: 20_000 });
    const verify = ambiguous.getByRole("button", { name: "Verify delivery" });
    await Promise.all([verify.click(), verify.click()]);
    await page.locator(".session-scroll .session-user:not(.session-pending-user)", { hasText: "ambiguous hello" })
      .waitFor({ state: "visible", timeout: 8_000 });
    if (sendCount !== 7) throw new Error(`expected one ambiguous retry POST, got ${sendCount - 6}`);
    if (await page.locator(".session-scroll .session-user", { hasText: "ambiguous hello" }).count() !== 1) {
      throw new Error("concurrent ambiguous retries duplicated the user message");
    }

    await composer.fill("uncertain edit me");
    await composer.press("Enter");
    const uncertainEdit = page.locator(".session-scroll .session-pending-user", { hasText: "uncertain edit me" });
    await uncertainEdit.getByText("delivery uncertain").waitFor({ state: "visible", timeout: 5_000 });
    await uncertainEdit.getByRole("button", { name: "Edit message" }).click();
    await composer.fill("uncertain edited");
    await composer.press("Enter");
    await page.getByText("uncertain edited", { exact: true }).last().waitFor({ state: "visible", timeout: 5_000 });
    if (!uncertainEditRequestId || uncertainEditRequestId === editedRequestId) {
      throw new Error("fixture did not capture the uncertain edit request id");
    }
    if (await page.locator(".session-scroll .session-user", { hasText: "uncertain edit me" }).count() !== 0) {
      throw new Error("uncertain edit left the old logical row visible");
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
