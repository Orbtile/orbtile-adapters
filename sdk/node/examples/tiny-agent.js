import { AdapterClient } from "../index.js";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
const pad = new AdapterClient({
  adapterId: "dev.example.tiny-node",
  registration: {
    name: "Tiny Node agent",
    palette: { baseHue: 150, attentionHue: 330 },
    capabilities: ["tap", "questions"],
  },
  onLeaseError: (e) => console.error("Lease:", e.code),
});
const input = process.stdin.isTTY
  ? createInterface({ input: process.stdin, output: process.stdout })
  : undefined;
try {
  await pad.connect();
  await pad.upsertSession("demo", {
    title: "Tiny agent demo",
    project: "Demo jobs",
    activity: "thinking",
  });
  for (const text of [
    "Hello",
    "Hello from a tiny",
    "Hello from a tiny agent.",
  ]) {
    await pad.upsertSession("demo", {
      activity: "streaming",
      message: { id: "hello", text },
    });
    await new Promise((r) => setTimeout(r, 500));
  }
  let resolveAnswer;
  const answered = new Promise((r) => {
    resolveAnswer = r;
  });
  const question = {
    id: randomUUID(),
    questions: [
      {
        text: "Which greeting?",
        header: "Greeting",
        options: [{ label: "Hello" }, { label: "Hi" }],
      },
    ],
  };
  await pad.askQuestion("demo", question);
  input?.question(
    "Choose 0 (Hello) or 1 (Hi); your terminal stays available: ",
    async (value) => {
      if (!["0", "1"].includes(value.trim()))
        return console.log(
          "Invalid choice; leave the prompt available in your agent UI.",
        );
      try {
        await pad.answerInUI(
          "demo",
          "local",
          () => resolveAnswer([[Number(value)]]),
          { requestId: question.id },
        );
      } catch (e) {
        console.error(e.code);
      }
    },
  );
  const controller = new AbortController();
  const consume = (async () => {
    for await (const event of pad.events({ signal: controller.signal }))
      await pad.applyEvent(event, (e) => {
        if (e.type === "question") resolveAnswer(e.picks);
        else console.log("Tap:", e.sessionId);
      });
  })();
  // A lost registration needs explicit own-UI reconciliation, never an implicit answer.
  consume.catch(() => resolveAnswer(undefined));
  const picks = await answered;
  if (!picks)
    throw new Error("Event stream ended; keep the own-UI prompt available");
  controller.abort();
  console.log("Answer:", picks);
  await pad.upsertSession("demo", {
    activity: "done",
    question: null,
    message: { id: "done", text: "Thanks!" },
  });
  await consume;
} finally {
  input?.close();
  await pad.unregister().catch((e) => console.error(e.code));
}
