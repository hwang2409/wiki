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

function buildFixtureBytes() {
  const script = `
import base64, sys
from backend.tests.test_media_scrub import _minimal_mp4, _wav_bytes
sys.stdout.write(
    base64.b64encode(_minimal_mp4(with_gps=False)).decode() + "\\n"
    + base64.b64encode(_wav_bytes()).decode() + "\\n"
)
`;
  const result = spawnSync(PYTHON, ["-c", script], {
    cwd: ROOT,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`fixture builder failed: ${result.stderr}`);
  }
  const [mp4B64, wavB64] = result.stdout.trim().split("\n");
  return {
    mp4Base64: mp4B64,
    wavBase64: wavB64,
    mp4Bytes: Buffer.from(mp4B64, "base64"),
    wavBytes: Buffer.from(wavB64, "base64"),
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
  logStep("building video + audio fixture bytes via the backend media_scrub fixtures");
  const { mp4Base64, wavBase64, mp4Bytes, wavBytes } = buildFixtureBytes();

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
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
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

    logStep("verifying served bytes match the scrubbed backend output");
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
    if (!videoBody.equals(mp4Bytes)) {
      throw new Error(
        `served video byte-mismatch: ${videoBody.length} vs source ${mp4Bytes.length}`,
      );
    }

    const servedAudio = await page.request.get(
      `${backend.baseUrl}/api/agents/${TICKET}/artifact/${results[1].artifactId}`,
    );
    if (servedAudio.status() !== 200) {
      throw new Error(`audio artifact 200 expected, got ${servedAudio.status()}`);
    }
    const audioBody = Buffer.from(await servedAudio.body());
    if (audioBody.subarray(0, 4).toString("ascii") !== "RIFF") {
      throw new Error("served audio missing RIFF header");
    }
    if (!audioBody.equals(wavBytes)) {
      throw new Error(
        `served audio byte-mismatch: ${audioBody.length} vs source ${wavBytes.length}`,
      );
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
