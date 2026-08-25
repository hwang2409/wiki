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
  const pageErrors = [];
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
    logStep(`page error: ${error.message}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) logStep(`response ${response.status()}: ${response.url()}`);
  });
  try {
    await page.addInitScript(() => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
      window.__wiki383UnhandledRejection = null;
      window.addEventListener("unhandledrejection", (event) => {
        window.__wiki383UnhandledRejection = String(event.reason?.message ?? event.reason);
      });
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
    await bar.getByRole("combobox", { name: "Playback speed" }).selectOption("1.5");
    await page.waitForFunction(() => {
      const videoElement = document.querySelector("[data-artifact-kind=video] video");
      return videoElement instanceof HTMLVideoElement && Math.abs(videoElement.playbackRate - 1.5) < 0.01;
    });
    const videoHandle = await video.elementHandle();
    if (!videoHandle) throw new Error("video element handle is missing");
    const stateBeforeExpand = await video.evaluate((node) => ({ currentTime: node.currentTime, playbackRate: node.playbackRate }));
    await page.evaluate(() => {
      Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: false });
    });
    await bar.getByRole("button", { name: "Enter fullscreen" }).click();
    const expandedPlayer = block.locator("dialog.artifact-media-player.is-media-expanded");
    await expandedPlayer.waitFor({ state: "visible" });
    if (await expandedPlayer.evaluate((node) => node.tagName) !== "DIALOG") throw new Error("expanded player is not a native dialog");
    const expandedVideo = expandedPlayer.locator("video");
    const expandedVideoHandle = await expandedVideo.elementHandle();
    if (!expandedVideoHandle) throw new Error("expanded video element handle is missing");
    if (!(await videoHandle.evaluate((node, expanded) => node === expanded, expandedVideoHandle))) {
      throw new Error("expansion replaced the video element");
    }
    const stateAfterExpand = await expandedVideo.evaluate((node) => ({ currentTime: node.currentTime, playbackRate: node.playbackRate }));
    if (Math.abs(stateAfterExpand.currentTime - stateBeforeExpand.currentTime) > 0.05
      || Math.abs(stateAfterExpand.playbackRate - stateBeforeExpand.playbackRate) > 0.01) {
      throw new Error(`expansion changed media state: ${JSON.stringify({ stateBeforeExpand, stateAfterExpand })}`);
    }
    await expandedPlayer.getByRole("button", { name: "Play" }).click();
    await page.waitForFunction(() => {
      const videoElement = document.querySelector("[data-artifact-kind=video] video");
      return videoElement instanceof HTMLVideoElement && !videoElement.paused;
    });
    if (await expandedPlayer.getByRole("button", { name: "Pause" }).count() !== 1) {
      throw new Error("play/pause button is out of sync after expansion");
    }
    const viewport = await expandedPlayer.boundingBox();
    const expectedViewport = await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight }));
    if (!viewport || Math.abs(viewport.x) > 0.5 || Math.abs(viewport.y) > 0.5
      || Math.abs(viewport.width - expectedViewport.width) > 0.5
      || Math.abs(viewport.height - expectedViewport.height) > 0.5) {
      throw new Error(`expanded player does not fill viewport: ${JSON.stringify({ viewport, expectedViewport })}`);
    }
    const expandedBar = expandedPlayer.locator(".artifact-video-controls");
    if (await expandedBar.isVisible() !== true) throw new Error("custom bar disappeared in fullscreen");
    if (await expandedPlayer.getAttribute("aria-modal") !== "true") throw new Error("expanded player is not modal");
    if (!(await expandedPlayer.evaluate((node) => node.matches(":modal")))) throw new Error("expanded player is not in the top layer");

    await page.evaluate(() => {
      const frame = document.querySelector("[data-artifact-kind=video] .artifact-video-frame");
      const videoElement = document.querySelector("[data-artifact-kind=video] video");
      if (!(frame instanceof HTMLElement) || !(videoElement instanceof HTMLVideoElement)) throw new Error("portrait probe elements are missing");
      frame.style.aspectRatio = "1 / 1";
      frame.style.maxWidth = "500px";
      videoElement.setAttribute("width", "720");
      videoElement.setAttribute("height", "1280");
    });
    const portraitGeometry = await expandedPlayer.evaluate((player) => {
      const frame = player.querySelector(".artifact-video-frame").getBoundingClientRect();
      const videoElement = player.querySelector("video").getBoundingClientRect();
      return { frame, video: videoElement, objectFit: getComputedStyle(player.querySelector("video")).objectFit };
    });
    if (portraitGeometry.objectFit !== "contain"
      || portraitGeometry.video.left < portraitGeometry.frame.left - 1
      || portraitGeometry.video.right > portraitGeometry.frame.right + 1
      || portraitGeometry.video.top < portraitGeometry.frame.top - 1
      || portraitGeometry.video.bottom > portraitGeometry.frame.bottom + 1) {
      throw new Error(`portrait video is not contained: ${JSON.stringify(portraitGeometry)}`);
    }

    await expandedBar.getByRole("button", { name: "Pause" }).click();
    await page.waitForFunction(() => {
      const videoElement = document.querySelector("[data-artifact-kind=video] video");
      return videoElement instanceof HTMLVideoElement && videoElement.paused;
    });
    const stateBeforeCollapse = await expandedVideo.evaluate((node) => ({ currentTime: node.currentTime, playbackRate: node.playbackRate }));
    await expandedPlayer.focus();
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".artifact-media-player.is-media-expanded"));
    const collapsedVideo = block.locator("video");
    const collapsedVideoHandle = await collapsedVideo.elementHandle();
    if (!collapsedVideoHandle || !(await videoHandle.evaluate((node, collapsed) => node === collapsed, collapsedVideoHandle))) {
      throw new Error("collapse replaced the video element");
    }
    const stateAfterCollapse = await collapsedVideo.evaluate((node) => ({ currentTime: node.currentTime, playbackRate: node.playbackRate }));
    if (Math.abs(stateAfterCollapse.currentTime - stateBeforeCollapse.currentTime) > 0.05
      || Math.abs(stateAfterCollapse.playbackRate - stateBeforeCollapse.playbackRate) > 0.01) {
      throw new Error(`collapse changed media state: ${JSON.stringify({ stateBeforeCollapse, stateAfterCollapse })}`);
    }
    if (await page.evaluate(() => document.activeElement?.getAttribute("aria-label")) !== "Enter fullscreen") {
      throw new Error("focus was not restored to the expand button");
    }
    const cleanupState = await page.evaluate(() => ({
      bodyOverflow: document.body.style.overflow,
      inertCount: [...document.body.children].filter((element) => element.hasAttribute("inert")).length,
    }));
    if (cleanupState.bodyOverflow !== "" || cleanupState.inertCount !== 0) {
      throw new Error(`modal cleanup failed: ${JSON.stringify(cleanupState)}`);
    }

    const pageErrorsBeforeRejectedRequest = pageErrors.length;
    await page.evaluate(() => {
      Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: true });
      const player = document.querySelector(".artifact-media-player");
      if (!(player instanceof HTMLElement)) throw new Error("media player is missing");
      Object.defineProperty(player, "requestFullscreen", {
        configurable: true,
        value: () => Promise.reject(new Error("fullscreen rejected by host")),
      });
      window.__wiki383UnhandledRejection = null;
    });
    await bar.getByRole("button", { name: "Enter fullscreen" }).click();
    await expandedPlayer.waitFor({ state: "visible" });
    if (await page.evaluate(() => window.__wiki383UnhandledRejection) !== null) {
      throw new Error("rejected requestFullscreen produced an unhandled rejection");
    }
    if (pageErrors.length !== pageErrorsBeforeRejectedRequest) {
      throw new Error(`rejected requestFullscreen produced page errors: ${pageErrors.slice(pageErrorsBeforeRejectedRequest).join("; ")}`);
    }
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".artifact-media-player.is-media-expanded"));
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
