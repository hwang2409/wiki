import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const RUN_ID = "00000000-0000-4000-8000-000000000383";
const TICKET = "WIKI-383";
const BEFORE_SCREENSHOT = "/tmp/wiki-383-video-before.png";
const AFTER_SCREENSHOT = "/tmp/wiki-383-video-after.png";

function resolvePython() {
  const systemPython = spawnSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
  const pathPythonCandidates = (process.env.PATH ?? "")
    .split(path.delimiter)
    .filter(Boolean)
    .map((directory) => path.join(directory, "python3"));
  const candidates = [
    process.env.WIKI_PYTHON,
    path.join(ROOT, ".venv", "bin", "python"),
    path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
    ...pathPythonCandidates,
    "/opt/homebrew/Caskroom/miniforge/base/bin/python3",
    systemPython,
  ].filter(Boolean);
  const found = candidates.find((candidate) => {
    if (!existsSync(candidate)) return false;
    return spawnSync(candidate, ["-c", "import pydantic"], { stdio: "ignore" }).status === 0;
  });
  if (!found) throw new Error(`No Python runtime found: ${candidates.join(", ")}`);
  return found;
}

const PYTHON = resolvePython();
process.env.WIKI_PYTHON ??= PYTHON;
const { makeFixtureRoot, startBackend, writeQueue } = await import("../scripts/wiki32-harness.mjs");

function logStep(message) {
  console.error(`[wiki-383-video-controls] ${message}`);
}

function invokeFixtureWorker(fixtures, input) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-383-fixture", version: "1" } },
    },
    {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: { name: "render_artifact", arguments: input },
    },
  ];
  const result = spawnSync(PYTHON, ["-m", "backend.app.wiki_artifacts"], {
    cwd: ROOT,
    env: { ...process.env, WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir, WIKI_RUN_ID: RUN_ID },
    input: `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error(`fixture worker failed: ${result.stderr || result.stdout}`);
  const responses = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
  const response = responses[1];
  if (response.result?.isError) throw new Error(response.result.content?.[0]?.text || "artifact rejected");
  const sentinel = response.result.content[0].text;
  const event = JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length));
  return { artifactId: event.id, sentinel };
}

async function writeFixture(fixtures, result, input) {
  const toolId = "toolu_wiki_383_video";
  const rows = [
    { type: "mode", mode: "normal", sessionId: "wiki-383-video" },
    {
      type: "assistant",
      timestamp: "2026-08-25T14:00:00Z",
      message: {
        role: "assistant",
        content: [{ type: "tool_use", id: toolId, name: "mcp__wiki-artifacts__render_artifact", input: { kind: input.kind, title: input.title, caption: input.caption, payload: { data_base64: "<omitted-from-transcript>", mime: input.payload.mime } } }],
      },
    },
    {
      type: "user",
      timestamp: "2026-08-25T14:00:01Z",
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: toolId, content: result.sentinel }] },
    },
  ];
  const transcript = path.join(fixtures.root, "wiki-383-video.jsonl");
  await fs.writeFile(transcript, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
  await fs.writeFile(
    fixtures.registryPath,
    JSON.stringify({ _orchestrators: { [TICKET]: { window: "@9999", spawned_at: "2026-08-25T14:00:00Z", kind: "cc", transcript } } }, null, 2),
  );
  writeQueue(fixtures.queuePath, TICKET, []);
  const live = path.join(fixtures.runtimeDir, "runs", RUN_ID, "artifacts", `${result.artifactId}.mp4`);
  const archive = path.join(fixtures.root, "archive", TICKET, "20260825-140000", "artifacts");
  await fs.mkdir(archive, { recursive: true });
  await fs.copyFile(live, path.join(archive, `${result.artifactId}.mp4`));
  const archiveSession = path.dirname(archive);
  await fs.writeFile(path.join(archiveSession, "raw.jsonl"), "\n");
  await fs.writeFile(path.join(archiveSession, "events.jsonl"), "\n");
  await fs.writeFile(path.join(archiveSession, "run.json"), JSON.stringify({ run_id: RUN_ID }));
  const commit = spawnSync(PYTHON, [
    "-c",
    "import sys; from pathlib import Path; from backend.app.agent_runtime.archive_protocol import commit_archive; commit_archive(Path(sys.argv[1]), run_id=sys.argv[2], completed_at='2026-08-25T14:00:00Z')",
    archiveSession,
    RUN_ID,
  ], { cwd: ROOT, encoding: "utf8" });
  if (commit.status !== 0) throw new Error(`archive commit failed: ${commit.stderr || commit.stdout}`);
}

async function main() {
  const videoPath = path.join(ROOT, "backend", "tests", "fixtures", "media", "tiny.mp4");
  const bytes = await fs.readFile(videoPath);
  const input = {
    kind: "video",
    title: "WIKI-383 video controls fixture",
    caption: "custom controls fixture",
    payload: { data_base64: bytes.toString("base64"), mime: "video/mp4" },
  };
  const fixtures = makeFixtureRoot("wiki-383-video-");
  const result = invokeFixtureWorker(fixtures, input);
  await writeFixture(fixtures, result, input);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => logStep(`page error: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() >= 400) logStep(`response ${response.status()}: ${response.url()}`);
  });
  try {
    await page.addInitScript(() => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    const block = page.locator(`[data-artifact-kind="video"]`).first();
    await block.waitFor({ state: "visible" });
    const video = block.locator("video");
    const bar = block.locator(".artifact-video-controls");
    if (await video.getAttribute("controls") !== null) throw new Error("native video controls are present");
    if (await bar.count() !== 1) throw new Error("custom video control bar is missing");
    const positions = await block.evaluate((node) => {
      const body = node.querySelector(".artifact-body").getBoundingClientRect();
      const frame = node.querySelector(".artifact-video-frame").getBoundingClientRect();
      return {
        bodyCenter: body.left + body.width / 2,
        frameCenter: frame.left + frame.width / 2,
      };
    });
    if (Math.abs(positions.bodyCenter - positions.frameCenter) > 1) throw new Error(`video frame is not centered: ${JSON.stringify(positions)}`);
    await block.screenshot({ path: BEFORE_SCREENSHOT });
    await video.evaluate((node) => new Promise((resolve, reject) => {
      if (node.readyState >= 1) {
        resolve();
        return;
      }
      const onLoaded = () => {
        cleanup();
        resolve();
      };
      const onError = () => {
        cleanup();
        reject(new Error("video metadata did not load"));
      };
      const cleanup = () => {
        node.removeEventListener("loadedmetadata", onLoaded);
        node.removeEventListener("error", onError);
      };
      node.addEventListener("loadedmetadata", onLoaded, { once: true });
      node.addEventListener("error", onError, { once: true });
      node.load();
      setTimeout(() => {
        cleanup();
        reject(new Error("video metadata timed out"));
      }, 15000);
    }));
    const renderedDimensions = await block.evaluate((node) => {
      const frame = node.querySelector(".artifact-video-frame").getBoundingClientRect();
      const video = node.querySelector("video");
      return {
        frameWidth: frame.width,
        intrinsicWidth: video.videoWidth || Number(video.getAttribute("width") || 0),
      };
    });
    if (!renderedDimensions.intrinsicWidth) throw new Error(`video intrinsic width is unavailable: ${JSON.stringify(renderedDimensions)}`);
    if (renderedDimensions.frameWidth > renderedDimensions.intrinsicWidth + 1) {
      throw new Error(`video frame upscales the intrinsic width: ${JSON.stringify(renderedDimensions)}`);
    }

    await bar.getByRole("button", { name: "Play" }).click();
    await page.waitForFunction(() => {
      const videoElement = document.querySelector("[data-artifact-kind=video] video");
      return videoElement instanceof HTMLVideoElement && !videoElement.paused;
    });
    await bar.getByRole("button", { name: "Pause" }).click();
    await page.waitForFunction(() => {
      const videoElement = document.querySelector("[data-artifact-kind=video] video");
      return videoElement instanceof HTMLVideoElement && videoElement.paused;
    });
    await bar.getByRole("button", { name: "Mute" }).click();
    if (!(await video.evaluate((node) => node.muted))) throw new Error("mute button did not mute video");
    const volumeSlider = bar.getByRole("slider", { name: "Volume" });
    await volumeSlider.press("Home");
    for (let step = 0; step < 50; step += 1) await volumeSlider.press("ArrowRight");
    const volume = await video.evaluate((node) => ({ volume: node.volume, muted: node.muted }));
    if (Math.abs(volume.volume - 0.5) > 0.01 || volume.muted) throw new Error(`volume control did not sync: ${JSON.stringify(volume)}`);
    const seekSlider = bar.getByRole("slider", { name: "Seek" });
    await seekSlider.press("Home");
    for (let step = 0; step < 50; step += 1) await seekSlider.press("ArrowRight");
    const currentTime = await video.evaluate((node) => node.currentTime);
    if (Math.abs(currentTime - 0.5) > 0.1) throw new Error(`seek control did not update currentTime: ${currentTime}`);
    await page.evaluate(() => {
      Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: false });
    });
    await bar.getByRole("button", { name: "Enter fullscreen" }).click();
    await page.waitForFunction(() => document.querySelector(".artifact-media-player")?.classList.contains("is-media-expanded"));
    if (await bar.isVisible() !== true) throw new Error("custom bar disappeared in fullscreen");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".artifact-media-player")?.classList.contains("is-media-expanded"));
    await block.screenshot({ path: AFTER_SCREENSHOT });
    logStep(`screenshots: ${BEFORE_SCREENSHOT}, ${AFTER_SCREENSHOT}`);
  } finally {
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
