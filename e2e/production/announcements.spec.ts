import { test, expect, createCompletedRun } from "../support/fixtures";
import { writeFile } from "node:fs/promises";
import { tabTo } from "../support/keyboard";

for (const theme of ["light", "dark"]) {
  test(`@keyboard-stream settled replies and task progress ${theme}`, async ({
    page,
    api,
  }, info) => {
    await page.addInitScript(
      (mode) => localStorage.setItem("cosci-theme", mode),
      theme,
    );
    // Replay the real persisted offline response in two controlled segments.
    await page.addInitScript(() => {
      const state = window as typeof window & {
        q2Arm: "interview" | "qa" | null;
        q2Finish?: () => void;
        q2Reply?: string;
      };
      state.q2Arm = "interview";
      const original = window.fetch;
      window.fetch = async (...args) => {
        const response = await original(...args);
        const url = String(args[0]);
        if (
          !response.headers
            .get("content-type")
            ?.includes("text/event-stream") ||
          !state.q2Arm ||
          !(state.q2Arm === "interview"
            ? /\/interviews(?:\/[^/]+\/turns)?$/.test(url)
            : /\/messages\/ask$/.test(url))
        )
          return response;
        state.q2Arm = null;
        const body = await response.text();
        const frames = body.split("\n\n").filter(Boolean);
        const parsed = frames.map((frame) =>
          JSON.parse(frame.replace(/^data:\s*/, "")),
        );
        const chunks = parsed.filter(
          (frame) => frame.type === "chunk" && frame.content,
        );
        // Offline interviews settle with one durable interview frame rather
        // than token frames. Split that real persisted answer for this guard.
        if (!chunks.length) {
          const interview = parsed.find(
            (frame) => frame.type === "interview",
          )?.interview;
          const reply = interview?.turns
            .filter((turn: { role: string }) => turn.role === "agent")
            .at(-1)?.content;
          if (!reply)
            throw new Error("Offline response has no persisted reply");
          const middle = Math.ceil(reply.length / 2);
          frames.unshift(
            "data: " +
              JSON.stringify({ type: "chunk", content: reply.slice(middle) }),
          );
          frames.unshift(
            "data: " +
              JSON.stringify({
                type: "chunk",
                content: reply.slice(0, middle),
              }),
          );
          state.q2Reply = reply;
        } else {
          state.q2Reply = chunks.map((chunk) => chunk.content).join("");
        }
        const first = frames.findIndex((frame) => {
          const value = JSON.parse(frame.replace(/^data:\s*/, ""));
          return value.type === "chunk" && value.content;
        });
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                "data: " +
                  JSON.stringify({
                    type: "reasoning",
                    content: "Private reasoning sentinel.",
                  }) +
                  "\n\n" +
                  frames.slice(0, first + 1).join("\n\n") +
                  "\n\n",
              ),
            );
            state.q2Finish = () => {
              controller.enqueue(
                encoder.encode(frames.slice(first + 1).join("\n\n") + "\n\n"),
              );
              controller.close();
            };
          },
        });
        return new Response(stream, {
          status: response.status,
          headers: response.headers,
        });
      };
    });
    await page.goto("/");
    const composer = page.getByRole("textbox").last();
    await tabTo(page, composer);
    await page.keyboard.insertText(
      "Compare mechanisms controlling enzyme stability.",
    );
    await page.keyboard.press("Enter");
    await expect
      .poll(() =>
        page.evaluate(() =>
          Boolean(
            (window as typeof window & { q2Finish?: () => void }).q2Finish,
          ),
        ),
      )
      .toBe(true);
    const announcement = page.getByRole("status", {
      name: "Research reply",
      exact: true,
    });
    await expect(announcement).toBeEmpty();
    await announcement.evaluate((node) => {
      const state = window as typeof window & { q2Announcements: string[] };
      state.q2Announcements = [];
      new MutationObserver(() => {
        if (node.textContent?.trim())
          state.q2Announcements.push(node.textContent);
      }).observe(node, { childList: true, subtree: true, characterData: true });
    });
    await expect(
      page.locator('.reference-model-bubble[aria-hidden="true"]').last(),
    ).toContainText(/scientific|mechanisms/i);
    await page.evaluate(() =>
      (window as typeof window & { q2Finish?: () => void }).q2Finish?.(),
    );
    await expect(announcement).toContainText("Which scientific mechanisms");
    await expect(announcement).not.toContainText("Private reasoning sentinel.");
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            (window as typeof window & { q2Announcements: string[] })
              .q2Announcements,
        ),
      )
      .toEqual([
        await page.evaluate(
          () => (window as typeof window & { q2Reply: string }).q2Reply,
        ),
      ]);
    const interviewTree = info.outputPath("interview-announcement.yml");
    await writeFile(interviewTree, await page.locator("body").ariaSnapshot());
    await info.attach("interview-tree", {
      path: interviewTree,
      contentType: "text/yaml",
    });
    const interviewImage = info.outputPath("interview-announcement.png");
    await page.screenshot({ path: interviewImage });
    await info.attach("interview-announcement", {
      path: interviewImage,
      contentType: "image/png",
    });
    await page.reload();
    await expect(
      page.getByRole("button", { name: "Copy response" }).first(),
    ).toBeVisible();
    await expect(announcement).toBeEmpty();

    const id = await createCompletedRun(api, {
      research_goal: "Offline accessibility Q&A enzyme stability",
      tier: "express",
    });
    await page.route("**/api/interviews/q2-announcement", (route) =>
      route.fulfill({
        json: {
          id: "q2-announcement",
          client_id: "e2e-client",
          status: "completed",
          run_id: id,
          fields: {
            research_challenge: "Offline accessibility Q&A enzyme stability",
            focus_area: [],
            preferences: [],
            title: "Enzyme stability",
          },
          current_question: null,
          documents: [],
          created_at: 1,
          updated_at: 2,
          completed_at: 2,
          turns: [
            {
              id: 1,
              role: "agent",
              content: "Saved interview reply.",
              created_at: 2,
            },
          ],
        },
      }),
    );
    await page.goto("/chats/q2-announcement");
    await expect(
      page.getByRole("region", { name: "Started research session" }),
    ).toBeVisible();
    await expect(announcement).toBeEmpty();
    await page.evaluate(() => {
      const state = window as typeof window & {
        q2Arm: "interview" | "qa" | null;
        q2Finish?: () => void;
      };
      state.q2Arm = "qa";
      state.q2Finish = undefined;
    });
    await tabTo(page, page.getByRole("textbox").last());
    await page.keyboard.insertText(
      "What evidence supports the leading hypothesis?",
    );
    await page.keyboard.press("Enter");
    await expect
      .poll(() =>
        page.evaluate(() =>
          Boolean(
            (window as typeof window & { q2Finish?: () => void }).q2Finish,
          ),
        ),
      )
      .toBe(true);
    await expect(announcement).toBeEmpty();
    await page.evaluate(() =>
      (window as typeof window & { q2Finish?: () => void }).q2Finish?.(),
    );
    const reply = await page.evaluate(
      () => (window as typeof window & { q2Reply?: string }).q2Reply!,
    );
    await expect(announcement).toHaveText(reply);
    await expect(announcement).not.toContainText("Private reasoning sentinel.");
    const answerTree = info.outputPath("qa-announcement.yml");
    await writeFile(answerTree, await page.locator("body").ariaSnapshot());
    await info.attach("qa-tree", {
      path: answerTree,
      contentType: "text/yaml",
    });
    const answerImage = info.outputPath("qa-announcement.png");
    await page.screenshot({ path: answerImage });
    await info.attach("qa-announcement", {
      path: answerImage,
      contentType: "image/png",
    });
    await page.reload();
    await expect(
      page.getByRole("button", { name: "Copy response" }).last(),
    ).toBeVisible();
    await expect(announcement).toBeEmpty();

    // Reuse the real completed offline run, with two controlled committed
    // progress snapshots and a held nonterminal SSE event for the fast fake.
    let phase = "planning.strategy";
    let advance!: () => void;
    const nextStep = new Promise<void>((resolve) => {
      advance = resolve;
    });
    await page.route(`**/api/runs/${id}`, async (route) => {
      const response = await route.fetch();
      const run = await response.json();
      await route.fulfill({
        json: {
          ...run,
          status: "running",
          execution_progress: {
            determinate: true,
            active_task: phase,
            total_tasks: 4,
            completed_tasks: phase === "planning.strategy" ? 1 : 2,
            queued_tasks: 2,
          },
        },
      });
    });
    await page.route(`**/api/runs/${id}/events**`, async (route) => {
      await nextStep;
      await route.fulfill({
        contentType: "text/event-stream",
        body:
          "data: " +
          JSON.stringify({
            seq: 1,
            type: "scientific_task",
            payload: { activity: "reflection" },
            created_at: Date.now() / 1000,
          }) +
          "\n\n",
      });
    });
    await page.goto(`/runs/${id}/details`);
    await expect(
      page.getByRole("heading", { name: "Research in progress" }),
    ).toBeVisible();
    const progress = page.getByRole("status", {
      name: "Run execution progress",
    });
    await expect(progress).toContainText("Planning Strategy");
    await expect(progress).toHaveAttribute("aria-live", "polite");
    await expect(progress).toHaveAttribute("aria-atomic", "true");
    const focused = page.getByRole("button", { name: "Feedback", exact: true });
    await tabTo(page, focused);
    phase = "reflection";
    advance();
    await expect(progress).toContainText("Reflection");
    await expect(progress).toContainText("2 of 4 committed tasks complete");
    await expect(focused).toBeFocused();
    const progressTree = info.outputPath("progress.yml");
    await writeFile(progressTree, await page.locator("body").ariaSnapshot());
    await info.attach("progress-tree", {
      path: progressTree,
      contentType: "text/yaml",
    });
    const progressImage = info.outputPath("progress.png");
    await page.screenshot({ path: progressImage });
    await info.attach("progress", {
      path: progressImage,
      contentType: "image/png",
    });
  });
}
