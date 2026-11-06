import {
  AdapterClient,
  type AdapterEvent,
  type SessionUpdate,
  AdapterError,
} from "../index.js";
async function typeFixture() {
  const client = new AdapterClient({
    adapterId: "dev.example.types",
    registration: {
      name: "Types",
      palette: { baseHue: 150 },
      capabilities: ["approvals", "questions"],
    },
  });
  const update: SessionUpdate = {
    title: "Types",
    activity: "thinking",
    message: null,
    project: "Customer jobs",
  };
  await client.upsertSession("run", update);
  await client.upsertSession("run", { project: null });
  await client.requestApproval("run", {
    id: "a",
    toolName: "Shell",
    commandSummary: "Check",
  });
  await client.askQuestion("run", {
    id: "q",
    questions: [{ text: "Choice", options: [{ label: "A" }, { label: "B" }] }],
  });
  for await (const event of client.events()) {
    const narrowed: AdapterEvent = event;
    await client.applyEvent(narrowed, (e) => {
      if (e.type === "question") e.picks.map((p) => p.length);
    });
  }
  const error = new AdapterError("test");
  error.code satisfies string;
  await client.answerInUI("run", "local", () => {}, { requestId: "q" });
  client.close();
}
void typeFixture;
