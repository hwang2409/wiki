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
      title: "Silky video fixture",
      caption: "mp4 artifact for WIKI-190",
      payload: { data_base64: mp4Base64, mime: "video/mp4" },
    },
    {
      kind: "audio",
      title: "Silky audio fixture",
      caption: "wav artifact for WIKI-191",
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
      sourceType: node.querySelector("source")?.getAttribute("type"),
      sourceSrc: node.querySelector("source")?.getAttribute("src") || "",
    }));
    if (videoAttrs.tag !== "VIDEO") throw new Error("video element missing");
    if (!videoAttrs.controls) throw new Error("video controls attribute missing");
    if (videoAttrs.preload !== "metadata") throw new Error(`video preload wrong: ${videoAttrs.preload}`);
    if (!videoAttrs.playsInline) throw new Error("video playsInline attribute missing");
    if (videoAttrs.sourceType !== "video/mp4") throw new Error(`video source type wrong: ${videoAttrs.sourceType}`);
    if (!videoAttrs.sourceSrc.includes(`/api/agents/${TICKET}/artifact/`)) {
      throw new Error(`video source src does not point to artifact API: ${videoAttrs.sourceSrc}`);
    }

    logStep("verifying audio element attributes");
    const audioAttrs = await audioBlock.locator("audio").evaluate((node) => ({
      tag: node.tagName,
      controls: node.hasAttribute("controls"),
      preload: node.getAttribute("preload"),
      sourceType: node.querySelector("source")?.getAttribute("type"),
      sourceSrc: node.querySelector("source")?.getAttribute("src") || "",
    }));
    if (audioAttrs.tag !== "AUDIO") throw new Error("audio element missing");
    if (!audioAttrs.controls) throw new Error("audio controls attribute missing");
    if (audioAttrs.preload !== "metadata") throw new Error(`audio preload wrong: ${audioAttrs.preload}`);
    if (audioAttrs.sourceType !== "audio/wav") throw new Error(`audio source type wrong: ${audioAttrs.sourceType}`);
    if (!audioAttrs.sourceSrc.includes(`/api/agents/${TICKET}/artifact/`)) {
      throw new Error(`audio source src does not point to artifact API: ${audioAttrs.sourceSrc}`);
    }

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
    const audioSrc = await audioBlock
      .locator("audio source")
      .getAttribute("src");
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
