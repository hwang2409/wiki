import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const CHECKED_SCREENSHOT = "/tmp/wiki-86-multi-select-checked.png";
const ANSWERED_SCREENSHOT = "/tmp/wiki-86-answered.png";

function logStep(message) {
  console.error(`[wiki-86-playwright] ${message}`);
}

function questionToolUse(toolUseId, questions) {
  return {
    type: "assistant",
    timestamp: "2026-07-13T12:00:00Z",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: toolUseId,
          name: "AskUserQuestion",
          input: { questions },
        },
      ],
    },
  };
}

async function writeJsonl(target, rows) {
  await fs.writeFile(target, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
}

async function openTicket(page, baseUrl, ticket) {
  await page.goto(`${baseUrl}/#/agent/${ticket}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-scroll");
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-86-multi-select-");
  const mixedTranscript = path.join(fixtures.root, "mixed-questions.jsonl");
  const singleTranscript = path.join(fixtures.root, "single-question.jsonl");
  const customTranscript = path.join(fixtures.root, "custom-question.jsonl");
  const mixedQuestions = [
    {
      question: "Which frontend features do you want?",
      header: "Features",
      multiSelect: true,
      options: [
        { label: "Search" },
        { label: "Vim mode" },
        { label: "Syntax themes" },
      ],
    },
    {
      question: "Which rollout should we use?",
      header: "Rollout",
      multiSelect: false,
      options: [
        { label: "Ship now" },
        { label: "Stage first" },
      ],
    },
  ];
  await writeJsonl(mixedTranscript, [questionToolUse("toolu_wiki_86_mixed", mixedQuestions)]);
  await writeJsonl(singleTranscript, [
    questionToolUse("toolu_wiki_86_single", [
      {
        question: "Which density should we use?",
        header: "Density",
        multiSelect: false,
        options: [{ label: "Compact" }, { label: "Comfortable" }],
      },
    ]),
  ]);
  await writeJsonl(customTranscript, [
    questionToolUse("toolu_wiki_86_custom", [
      {
        question: "Which extras should we include?",
        header: "Extras",
        multiSelect: true,
        options: [{ label: "Search" }, { label: "Themes" }],
      },
    ]),
  ]);
  writeRegistry(fixtures.registryPath, [
    ["WIKI-86", mixedTranscript],
    ["WIKI-860", singleTranscript],
    ["WIKI-861", customTranscript],
  ]);

  logStep(`fixture root: ${fixtures.root}`);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1180, height: 1100 } });
  const responses = [];

  try {
    await page.route("**/api/agents/*/respond", async (route) => {
      responses.push({
        url: route.request().url(),
        body: route.request().postDataJSON(),
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ state: "working" }),
      });
    });

    logStep("asserting mixed per-question controls");
    await openTicket(page, backend.baseUrl, "WIKI-86");
    const multiQuestion = page.locator(".session-question").filter({
      hasText: "Which frontend features do you want?",
    });
    const singleQuestion = page.locator(".session-question").filter({
      hasText: "Which rollout should we use?",
    });
    await multiQuestion.waitFor({ state: "visible" });
    await singleQuestion.waitFor({ state: "visible" });
    assert.equal(await multiQuestion.locator('input[type="checkbox"]').count(), 4);
    assert.equal(await multiQuestion.locator('input[type="radio"]').count(), 0);
    assert.equal(await singleQuestion.locator('input[type="radio"]').count(), 3);
    assert.equal(await singleQuestion.locator('input[type="checkbox"]').count(), 0);
    await multiQuestion.getByText("Select at least one option (required).").waitFor();
    await multiQuestion.getByLabel("Custom answer for Which frontend features do you want?").waitFor();

    const submit = page.locator(".session-question-actions button");
    await submit.waitFor({ state: "visible" });
    assert.equal(await submit.isDisabled(), true);
    await multiQuestion.locator("label", { hasText: "Search" }).locator('input[type="checkbox"]').check();
    await multiQuestion.locator("label", { hasText: "Vim mode" }).locator('input[type="checkbox"]').check();
    assert.equal(await submit.isDisabled(), true);
    await singleQuestion.locator("label", { hasText: "Ship now" }).locator('input[type="radio"]').check();
    assert.equal(await submit.isEnabled(), true);

    logStep(`capturing checked state: ${CHECKED_SCREENSHOT}`);
    await page.locator(".session-scroll").screenshot({ path: CHECKED_SCREENSHOT });
    await submit.click();
    await page.waitForFunction(() => {
      const buttons = [...document.querySelectorAll(".session-question-actions button")];
      return buttons.some((button) => button.textContent === "Answers sent");
    });
    assert.deepEqual(responses[0].body, {
      request_id: "toolu_wiki_86_mixed",
      response: {
        answers: {
          "Which frontend features do you want?": ["Search", "Vim mode"],
          "Which rollout should we use?": "Ship now",
        },
      },
    });
    await multiQuestion.locator(".session-question-answer", { hasText: "Search, Vim mode" }).waitFor();
    await singleQuestion.locator(".session-question-answer", { hasText: "Ship now" }).waitFor();
    logStep(`capturing answered state: ${ANSWERED_SCREENSHOT}`);
    await page.locator(".session-scroll").evaluate((element) => {
      element.scrollTop = 0;
    });
    await page.locator(".session-scroll").screenshot({ path: ANSWERED_SCREENSHOT });

    logStep("asserting single-select auto-submit regression");
    await openTicket(page, backend.baseUrl, "WIKI-860");
    const densityQuestion = page.locator(".session-question").filter({
      hasText: "Which density should we use?",
    });
    await densityQuestion.waitFor({ state: "visible" });
    const singleResponse = page.waitForResponse((response) => response.url().includes("WIKI-860/respond"));
    await densityQuestion
      .locator("label", { hasText: "Comfortable" })
      .locator('input[type="radio"]')
      .check();
    await singleResponse;
    assert.deepEqual(responses[1].body, {
      request_id: "toolu_wiki_86_single",
      response: {
        answers: { "Which density should we use?": "Comfortable" },
      },
    });

    logStep("asserting multi-select Other is submitted alongside checked options");
    await openTicket(page, backend.baseUrl, "WIKI-861");
    const customQuestion = page.locator(".session-question").filter({
      hasText: "Which extras should we include?",
    });
    await customQuestion.waitFor({ state: "visible" });
    await customQuestion.locator("label", { hasText: "Search" }).locator('input[type="checkbox"]').check();
    await customQuestion.getByLabel("Other answer for Which extras should we include?").check();
    await customQuestion.getByLabel("Custom answer for Which extras should we include?").fill("Custom panels");
    const customResponse = page.waitForResponse((response) => response.url().includes("WIKI-861/respond"));
    await customQuestion.locator(".session-question-actions button").click();
    await customResponse;
    assert.deepEqual(responses[2].body, {
      request_id: "toolu_wiki_86_custom",
      response: {
        answers: { "Which extras should we include?": ["Search", "Custom panels"] },
      },
    });
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
