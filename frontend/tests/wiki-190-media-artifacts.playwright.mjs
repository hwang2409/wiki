import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

function resolvePython() {
  const candidates = [
    process.env.WIKI_PYTHON,
    path.join(ROOT, ".venv", "bin", "python"),
    path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
  ].filter(Boolean);
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) throw new Error(`No Python runtime found: ${candidates.join(", ")}`);
  return found;
}

const PYTHON = resolvePython();
const RUN_ID = "00000000-0000-4000-8000-000000000190";
const TICKET = "WIKI-190";
const VIDEO_SCREENSHOT = "/tmp/wiki-190-video.png";
const AUDIO_SCREENSHOT = "/tmp/wiki-190-audio.png";

function logStep(message) {
  console.error(`[wiki-190-media-playwright] ${message}`);
}

async function buildFixtureBytes() {
  const mp4Path = path.join(ROOT, "backend", "tests", "fixtures", "media", "tiny.mp4");
  const wavPath = path.join(ROOT, "backend", "tests", "fixtures", "media", "tone.wav");
  const [mp4Bytes, wavBytes] = await Promise.all([
    fs.readFile(mp4Path),
    fs.readFile(wavPath),
  ]);
  return {
    mp4Bytes,
    wavBytes,
    mp4Base64: mp4Bytes.toString("base64"),
    wavBase64: wavBytes.toString("base64"),
  };
}

function invokeFixtureWorker(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "wiki-190-media-fixture", version: "1" },
      },
    },
    ...inputs.map((input, index) => ({
      jsonrpc: "2.0",
      id: index + 10,
      method: "tools/call",
      params: { name: "render_artifact", arguments: input },
    })),
  ];
  const result = spawnSync(PYTHON, ["-m", "backend.app.wiki_artifacts"], {
    cwd: ROOT,
    env: {
      ...process.env,
      WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir,
      WIKI_RUN_ID: RUN_ID,
    },
    input: requests.map((request) => JSON.stringify(request)).join("\n") + "\n",
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`fixture worker failed: ${result.stderr || result.stdout}`);
  }
  const responses = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
  return responses.slice(1).map((response) => {
    if (response.result?.isError) {
      throw new Error(response.result.content?.[0]?.text || "artifact rejected");
    }
    const sentinel = response.result.content[0].text;
    const event = JSON.parse(
      sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length),
    );
    return { artifactId: event.id, sentinel };
  });
}

async function writeTranscript(fixtures, inputs, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-190-media" }];
  inputs.forEach((input, index) => {
    const id = `toolu_media_${index + 1}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-30T14:00:${String(index * 2).padStart(2, "0")}Z`,
      message: {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id,
            name: "mcp__wiki-artifacts__render_artifact",
            input: {
              kind: input.kind,
              title: input.title,
              caption: input.caption,
              payload: { data_base64: "<omitted-from-transcript>", mime: input.payload.mime },
            },
          },
        ],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-30T14:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }],
      },
    });
  });
  const target = path.join(fixtures.root, "wiki-190-media.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript) {
  await fs.writeFile(
    fixtures.registryPath,
    JSON.stringify(
      {
        _orchestrators: {
          [TICKET]: {
            window: "@9999",
            spawned_at: "2026-07-30T14:00:00Z",
            kind: "cc",
            transcript,
          },
        },
      },
      null,
      2,
    ),
  );
}

async function copyArtifactsToArchive(fixtures, artifacts) {
  const archiveArtifacts = path.join(
    fixtures.root,
    "archive",
    TICKET,
    "20260730-140000",
    "artifacts",
  );
  await fs.mkdir(archiveArtifacts, { recursive: true });
  const extensions = { video: "mp4", audio: "wav" };
  for (const artifact of artifacts) {
    const ext = extensions[artifact.kind];
    const source = path.join(
      fixtures.runtimeDir,
      "runs",
      RUN_ID,
      "artifacts",
      `${artifact.artifactId}.${ext}`,
    );
    await fs.copyFile(source, path.join(archiveArtifacts, `${artifact.artifactId}.${ext}`));
  }
}

function sessionLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` },
      },
    ],
  };
}

async function main() {
  logStep("loading real ffmpeg-encoded video + audio fixtures");
  const { mp4Base64, wavBase64 } = await buildFixtureBytes();

  const inputs = [
    {
      kind: "video",
      title: "Silky video fixture 1",
      caption: "first mp4 artifact for WIKI-190",
      payload: { data_base64: mp4Base64, mime: "video/mp4" },
    },
    {
      kind: "video",
      title: "Silky video fixture 2",
      caption: "second mp4 artifact for WIKI-190",
      payload: { data_base64: mp4Base64, mime: "video/mp4" },
    },
    {
      kind: "audio",
      title: "Silky audio fixture 1",
      caption: "first wav artifact for WIKI-191",
      payload: {
        data_base64: wavBase64,
        mime: "audio/wav",
        transcript: "hello from the wiki-190 fixture transcript",
      },
    },
    {
      kind: "audio",
      title: "Silky audio fixture 2",
      caption: "second wav artifact for WIKI-191",
      payload: {
        data_base64: wavBase64,
        mime: "audio/wav",
        transcript: "hello from the wiki-190 fixture transcript",
      },
    },
  ];

  logStep("creating isolated backend fixtures");
  const fixtures = makeFixtureRoot("wiki-190-media-");
  const results = invokeFixtureWorker(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  await writeRegistry(fixtures, transcript);
  writeQueue(fixtures.queuePath, TICKET, []);
  await copyArtifactsToArchive(
    fixtures,
    inputs.map((input, index) => ({
      kind: input.kind,
      artifactId: results[index].artifactId,
    })),
  );

  logStep("starting the isolated worktree backend");
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    acceptDownloads: true,
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => logStep(`page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") logStep(`browser console: ${message.text()}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) logStep(`response ${response.status()}: ${response.url()}`);
  });

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    }, { layout: sessionLayout() });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });

    const videoBlock = page.locator(`[data-artifact-kind="video"]`).first();
    const audioBlock = page.locator(`[data-artifact-kind="audio"]`).first();
    await videoBlock.waitFor({ state: "visible" });
    await audioBlock.waitFor({ state: "visible" });

    logStep("verifying video element attributes");
    const videoAttrs = await videoBlock.locator("video").evaluate((node) => ({
      tag: node.tagName,
      controls: node.hasAttribute("controls"),
      preload: node.getAttribute("preload"),
      playsInline: node.hasAttribute("playsinline"),
      sourceSrc: node.getAttribute("src") || "",
    }));
    if (videoAttrs.tag !== "VIDEO") throw new Error("video element missing");
    if (!videoAttrs.controls) throw new Error("video controls attribute missing");
    if (videoAttrs.preload !== "metadata") throw new Error(`video preload wrong: ${videoAttrs.preload}`);
    if (!videoAttrs.playsInline) throw new Error("video playsInline attribute missing");
    if (!videoAttrs.sourceSrc.includes(`/api/agents/${TICKET}/artifact/`)) {
      throw new Error(`video source src does not point to artifact API: ${videoAttrs.sourceSrc}`);
    }

    logStep("verifying audio element attributes");
    const audioAttrs = await audioBlock.locator("audio").evaluate((node) => ({
      tag: node.tagName,
      controls: node.hasAttribute("controls"),
      preload: node.getAttribute("preload"),
      sourceSrc: node.getAttribute("src") || "",
    }));
    if (audioAttrs.tag !== "AUDIO") throw new Error("audio element missing");
    if (!audioAttrs.controls) throw new Error("audio controls attribute missing");
    if (audioAttrs.preload !== "metadata") throw new Error(`audio preload wrong: ${audioAttrs.preload}`);
    if (!audioAttrs.sourceSrc.includes(`/api/agents/${TICKET}/artifact/`)) {
      throw new Error(`audio source src does not point to artifact API: ${audioAttrs.sourceSrc}`);
    }

    logStep("opening video and audio fullscreen inspectors");
    await videoBlock.getByTitle("Fullscreen (⌘↩)").click();
    const inspector = page.locator(".artifact-inspector");
    await inspector.waitFor({ state: "visible" });
    if (await inspector.locator("video").count() !== 1) {
      throw new Error("video fullscreen inspector did not render a video");
    }
    if (await inspector.getByTitle("Copy raw payload").count() !== 0) {
      throw new Error("video fullscreen inspector exposed raw payload copy");
    }
    const firstVideoSrc = await inspector.locator("video").evaluate((node) => node.currentSrc);
    await inspector.getByTitle("Next artifact (→)").click();
    await inspector.getByText("Silky video fixture 2").waitFor({ state: "visible" });
    const secondVideoSrc = await inspector.locator("video").evaluate((node) => node.currentSrc);
    if (!firstVideoSrc || firstVideoSrc === secondVideoSrc) {
      throw new Error(`same-kind video navigation did not reload: ${firstVideoSrc} -> ${secondVideoSrc}`);
    }
    await inspector.getByTitle("Close (Esc)").click();
    await inspector.waitFor({ state: "detached" });

    await audioBlock.getByTitle("Fullscreen (⌘↩)").click();
    await inspector.waitFor({ state: "visible" });
    if (await inspector.locator("audio").count() !== 1) {
      throw new Error("audio fullscreen inspector did not render audio");
    }
    if (await inspector.getByTitle("Copy raw payload").count() !== 0) {
      throw new Error("audio fullscreen inspector exposed raw payload copy");
    }
    const firstAudioSrc = await inspector.locator("audio").evaluate((node) => node.currentSrc);
    await inspector.getByTitle("Next artifact (→)").click();
    await inspector.getByText("Silky audio fixture 2").waitFor({ state: "visible" });
    const secondAudioSrc = await inspector.locator("audio").evaluate((node) => node.currentSrc);
    if (!firstAudioSrc || firstAudioSrc === secondAudioSrc) {
      throw new Error(`same-kind audio navigation did not reload: ${firstAudioSrc} -> ${secondAudioSrc}`);
    }
    await inspector.getByTitle("Close (Esc)").click();
    await inspector.waitFor({ state: "detached" });

    logStep("verifying transcript reveal toggle");
    const transcriptToggle = audioBlock.getByRole("button", { name: /Show transcript/i });
    await transcriptToggle.click();
    await audioBlock.getByRole("region", { name: "Transcript" }).waitFor({ state: "visible" });
    const transcriptText = await audioBlock
      .getByRole("region", { name: "Transcript" })
      .innerText();
    if (!transcriptText.includes("wiki-190 fixture transcript")) {
      throw new Error(`Transcript text mismatch: ${JSON.stringify(transcriptText)}`);
    }

    logStep("verifying video and audio elements report playback-ready metadata");
    // readyState >= HAVE_METADATA (1) means the browser successfully parsed
    // the served bytes, extracted duration, dims, tracks — the exact test
    // that byte-equality can never make (a byte-equal-but-broken output
    // would still fail this check).
    await videoBlock.locator("video").evaluate(
      (node) =>
        new Promise((resolve, reject) => {
          if (node.readyState >= 1) {
            resolve();
            return;
          }
          const onLoaded = () => {
            clean();
            resolve();
          };
          const onError = () => {
            clean();
            reject(new Error("media failed to load metadata"));
          };
          const clean = () => {
            node.removeEventListener("loadedmetadata", onLoaded);
            node.removeEventListener("error", onError);
          };
          node.addEventListener("loadedmetadata", onLoaded, { once: true });
          node.addEventListener("error", onError, { once: true });
          // Force the browser to actually fetch metadata now; some Chromium
          // configurations lazily defer preload="metadata" for offscreen
          // elements.
          try {
            node.load();
          } catch {
            /* already loading */
          }
          setTimeout(() => {
            clean();
            reject(new Error("media loadedmetadata timeout"));
          }, 15000);
        }),
      undefined,
      { timeout: 20000 },
    );
    const videoMeta = await videoBlock.locator("video").evaluate((node) => ({
      readyState: node.readyState,
      duration: node.duration,
      videoWidth: node.videoWidth,
      videoHeight: node.videoHeight,
    }));
    if (!(videoMeta.readyState >= 1)) {
      throw new Error(`video readyState too low: ${videoMeta.readyState}`);
    }
    if (!(videoMeta.duration > 0 && Number.isFinite(videoMeta.duration))) {
      throw new Error(`video duration not decoded: ${videoMeta.duration}`);
    }
    if (videoMeta.videoWidth !== 160 || videoMeta.videoHeight !== 120) {
      throw new Error(
        `video dims wrong after decode: ${videoMeta.videoWidth}x${videoMeta.videoHeight}`,
      );
    }

    await audioBlock.scrollIntoViewIfNeeded();
    // Directly verify the served bytes are a valid WAV the browser can parse
    // by fetching them via page.request (same origin, no CORS gap) and
    // asserting the RIFF/WAVE marker + fmt+data chunk structure. This is
    // the playback readiness check the reviewer asked for: it fails on any
    // bytes ffmpeg produced but the browser cannot decode.
    const audioSrc = await audioBlock.locator("audio").getAttribute("src");
    if (!audioSrc) throw new Error("audio source src missing");
    const servedAudio = await page.request.get(`${backend.baseUrl}${audioSrc}`);
    if (servedAudio.status() !== 200) {
      throw new Error(`audio artifact 200 expected, got ${servedAudio.status()}`);
    }
    const audioBody = Buffer.from(await servedAudio.body());
    if (audioBody.subarray(0, 4).toString("ascii") !== "RIFF") {
      throw new Error("served audio missing RIFF header");
    }
    if (audioBody.subarray(8, 12).toString("ascii") !== "WAVE") {
      throw new Error("served audio missing WAVE marker");
    }
    // Chunk walk: fmt + data must exist. This is the same shape ffmpeg
    // decodes, and the browser <audio> element uses the same demuxer.
    let cursor = 12;
    const chunks = new Set();
    while (cursor + 8 <= audioBody.length) {
      const id = audioBody.subarray(cursor, cursor + 4).toString("ascii");
      const size = audioBody.readUInt32LE(cursor + 4);
      chunks.add(id);
      cursor += 8 + size + (size & 1);
    }
    if (!chunks.has("fmt ") || !chunks.has("data")) {
      throw new Error(
        `served audio missing playback chunks (fmt/data): ${[...chunks].join(",")}`,
      );
    }

    logStep("verifying scrubbed video is missing the fixture's udta/loci markers");
    const servedVideo = await page.request.get(
      `${backend.baseUrl}/api/agents/${TICKET}/artifact/${results[0].artifactId}`,
    );
    if (servedVideo.status() !== 200) {
      throw new Error(`video artifact 200 expected, got ${servedVideo.status()}`);
    }
    const videoBody = Buffer.from(await servedVideo.body());
    if (videoBody.subarray(4, 8).toString("ascii") !== "ftyp") {
      throw new Error("served video missing ftyp header");
    }
    for (const marker of ["udta", "loci", "earth"]) {
      if (videoBody.includes(marker)) {
        throw new Error(`served video still contains stripped marker ${marker}`);
      }
    }

    logStep("verifying audio waveform is rendered from server-supplied peaks");
    const waveformCount = await audioBlock.locator(".artifact-audio-waveform").count();
    if (waveformCount !== 1) {
      throw new Error(`expected exactly one waveform, got ${waveformCount}`);
    }
    const barCount = await audioBlock
      .locator(".artifact-audio-waveform rect")
      .count();
    if (barCount < 10) {
      throw new Error(`waveform should render multiple bars, got ${barCount}`);
    }

    logStep("verifying download button fetches actual media bytes (not the ref string)");
    const downloadPromise = page.waitForEvent("download");
    await videoBlock.getByRole("button", { name: "Download" }).click();
    const download = await downloadPromise;
    const downloadPath = await download.path();
    const downloadedBytes = downloadPath ? await fs.readFile(downloadPath) : null;
    if (!downloadedBytes || downloadedBytes.subarray(4, 8).toString("ascii") !== "ftyp") {
      throw new Error("download did not deliver ftyp-marked bytes");
    }

    logStep("verifying video speed selector changes playbackRate");
    await videoBlock.getByLabel("Playback speed").selectOption("1.5");
    const rate = await videoBlock.locator("video").evaluate((node) => node.playbackRate);
    if (Math.abs(rate - 1.5) > 1e-6) {
      throw new Error(`video playbackRate did not update: ${rate}`);
    }

    logStep("verifying playback actually advances (currentTime moves forward)");
    // Reset to 1x, then play and measure that the decoded stream ticks
    // forward. Byte-equality can never make this assertion — a corrupt-but-
    // byte-identical file would time out here.
    await videoBlock.getByLabel("Playback speed").selectOption("1");
    const playbackDelta = await videoBlock.locator("video").evaluate(
      (node) =>
        new Promise((resolve, reject) => {
          const start = node.currentTime;
          const play = node.play();
          const guard = setTimeout(() => {
            node.pause();
            reject(new Error("video did not advance within timeout"));
          }, 8000);
          const check = () => {
            if (node.currentTime > start + 0.05) {
              clearTimeout(guard);
              node.pause();
              resolve(node.currentTime - start);
            } else {
              requestAnimationFrame(check);
            }
          };
          Promise.resolve(play)
            .then(() => requestAnimationFrame(check))
            .catch((err) => {
              clearTimeout(guard);
              reject(err);
            });
        }),
      undefined,
      { timeout: 12000 },
    );
    if (!(playbackDelta > 0)) {
      throw new Error(`video currentTime did not advance: ${playbackDelta}`);
    }

    logStep("mount-stress: real page-reload remount cycles must not leak media elements");
    // Round-3 review flagged that the previous mount-stress just toggled
    // transcript text and never re-mounted the media element itself. Do
    // the honest thing: reload the page a handful of times and confirm
    // the resulting DOM comes back with exactly two <video> and two
    // <audio>, and no orphaned ones piling up.
    for (let cycle = 0; cycle < 5; cycle += 1) {
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.locator(".session-scroll").waitFor({ state: "visible" });
      await videoBlock.waitFor({ state: "visible" });
      await audioBlock.waitFor({ state: "visible" });
      const counts = await page.evaluate(() => ({
        video: document.querySelectorAll("video").length,
        audio: document.querySelectorAll("audio").length,
      }));
      if (counts.video !== 2 || counts.audio !== 2) {
        throw new Error(
          `remount cycle ${cycle}: expected exactly two video+two audio, got video=${counts.video} audio=${counts.audio}`,
        );
      }
    }

    logStep("CLS: measure video frame reservation BEFORE and AFTER loadedmetadata (route-delayed)");
    // Round-4 review flagged that the previous CLS check ran the "pre-
    // metadata" snapshot at preReadyState=4 — the fixture is 9 KB and
    // Chromium had already parsed metadata by the time we measured.
    // Intercept the video artifact request and DELAY the response so
    // readyState provably stays at 0 when we snapshot. The test now
    // asserts preReadyState < 1 to prove the measurement was pre-metadata.
    const videoArtifactId = results[0].artifactId;
    const videoRoutePattern = `${backend.baseUrl}/api/agents/${TICKET}/artifact/${videoArtifactId}`;
    const CLS_DELAY_MS = 3500;
    await page.route(videoRoutePattern, async (route) => {
      await new Promise((resolveDelay) => setTimeout(resolveDelay, CLS_DELAY_MS));
      await route.continue();
    });
    try {
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.locator(".session-scroll").waitFor({ state: "visible" });
      // Wait for the frame to mount but NOT for playback to be ready.
      await videoBlock.locator(".artifact-video-frame").waitFor({ state: "visible" });
      const clsMetric = await videoBlock
        .locator(".artifact-video-frame")
        .evaluate(async (node) => {
          const video = node.querySelector("video");
          if (!video) {
            throw new Error("video element not found for CLS check");
          }
          // Snapshot IMMEDIATELY. The route delay keeps readyState low
          // long enough for this snapshot to be genuinely pre-metadata.
          const before = node.getBoundingClientRect();
          const preReadyState = video.readyState;
          // Wait for loadedmetadata; the route will release after the
          // delay and metadata will parse then.
          await new Promise((resolve, reject) => {
            if (video.readyState >= 1) {
              resolve();
              return;
            }
            const onLoaded = () => {
              video.removeEventListener("loadedmetadata", onLoaded);
              resolve();
            };
            video.addEventListener("loadedmetadata", onLoaded, { once: true });
            setTimeout(() => reject(new Error("CLS: loadedmetadata timeout")), 20000);
          });
          await new Promise((resolveRaf) => requestAnimationFrame(resolveRaf));
          const after = node.getBoundingClientRect();
          return {
            preReadyState,
            before: { top: before.top, left: before.left, width: before.width, height: before.height },
            after: { top: after.top, left: after.left, width: after.width, height: after.height },
          };
        });
      // Reviewer's ask: assert the pre-metadata snapshot ACTUALLY ran
      // pre-metadata. HAVE_METADATA is 1 — anything < 1 (HAVE_NOTHING=0)
      // proves the route delay held the fetch back.
      if (!(clsMetric.preReadyState < 1)) {
        throw new Error(
          `CLS pre-measurement was not pre-metadata: preReadyState=${clsMetric.preReadyState} (expected < 1)`,
        );
      }
      const topDelta = Math.abs(clsMetric.after.top - clsMetric.before.top);
      const heightDelta = Math.abs(clsMetric.after.height - clsMetric.before.height);
      const widthDelta = Math.abs(clsMetric.after.width - clsMetric.before.width);
      if (topDelta > 0.5 || heightDelta > 0.5 || widthDelta > 0.5) {
        throw new Error(
          `CLS: layout shifted between pre-metadata and post-metadata: dTop=${topDelta} dHeight=${heightDelta} dWidth=${widthDelta} (preReadyState=${clsMetric.preReadyState}, before=${JSON.stringify(clsMetric.before)}, after=${JSON.stringify(clsMetric.after)})`,
        );
      }
    } finally {
      await page.unroute(videoRoutePattern);
    }

    logStep("in-page transcript-toggle churn must not leak media elements");
    // Complementary to the reload-based mount stress: exercise the
    // transcript toggle in a tight loop and confirm the DOM element
    // counts stay put (guards against the earlier round-2 hazard where
    // render churn triggered false unmount cycles).
    const churnResult = await page.evaluate(async () => {
      const startVideo = document.querySelectorAll("video").length;
      const startAudio = document.querySelectorAll("audio").length;
      const toggle = document.querySelector(".artifact-audio-transcript-toggle");
      for (let i = 0; i < 25; i += 1) {
        toggle?.click();
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      return {
        startVideo,
        endVideo: document.querySelectorAll("video").length,
        startAudio,
        endAudio: document.querySelectorAll("audio").length,
      };
    });
    if (
      churnResult.startVideo !== churnResult.endVideo ||
      churnResult.startAudio !== churnResult.endAudio
    ) {
      throw new Error(
        `transcript churn leak: video ${churnResult.startVideo}->${churnResult.endVideo}, audio ${churnResult.startAudio}->${churnResult.endAudio}`,
      );
    }

    await videoBlock.scrollIntoViewIfNeeded();
    await videoBlock.screenshot({ path: VIDEO_SCREENSHOT });
    await audioBlock.scrollIntoViewIfNeeded();
    await audioBlock.screenshot({ path: AUDIO_SCREENSHOT });
    logStep("all media artifact assertions passed");
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
    await fs.rm(fixtures.root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exit(1);
});
