export type Activity =
  | "starting"
  | "idle"
  | "thinking"
  | "tool"
  | "streaming"
  | "waitingPermission"
  | "waitingQuestion"
  | "done"
  | "error"
  | "compacting"
  | "offline";
export type Capability = "tap" | "approvals" | "questions";
export type PaletteState =
  | "idle"
  | "starting"
  | "thinking"
  | "tool"
  | "streaming"
  | "done"
  | "compacting"
  | "offline"
  | "waitingPermission"
  | "waitingQuestion";
export type PaletteStates = Partial<Record<PaletteState, string>>;
export interface AdapterIcon {
  png?: string;
  appBundleId?: string;
}
export interface Palette {
  baseHue: number;
  attentionHue?: number;
  chroma?: number;
  lightness?: number;
  states?: PaletteStates | null;
}
export interface Registration {
  name: string;
  shortName?: string;
  palette: Palette;
  glyph?: { symbol?: string; monogram?: string };
  icon?: AdapterIcon | null;
  version?: string;
  homepage?: string;
  urlSchemes?: string[];
  capabilities?: Capability[];
  tapBehavior?: "alsoOpen" | "eventOnly";
}
export interface ApprovalRequest {
  id: string;
  toolName: string;
  commandSummary: string;
}
export interface Question {
  text: string;
  header?: string;
  multiSelect?: boolean;
  options: { label: string; description?: string }[];
}
export interface QuestionRequest {
  id: string;
  questions: Question[];
}
export interface SessionUpdate {
  title?: string;
  activity?: Activity;
  cwd?: string | null;
  /** Project group override (Orbtile 0.2.17+); trimmed 1...128 characters, null clears. */
  project?: string | null;
  model?: string | null;
  toolLabel?: string | null;
  toolKind?: string | null;
  message?: { id: string; text: string } | null;
  statusDetail?: string | null;
  subagentCount?: number;
  host?: string;
  openURL?: string | null;
  pid?: number | null;
  background?: boolean;
  liveText?: boolean;
  ttlSeconds?: number;
  approval?: ApprovalRequest | null;
  question?: QuestionRequest | null;
}
export interface SessionBatchItem extends SessionUpdate {
  id: string;
  title: string;
  activity: Activity;
}
export interface RegistrationReply {
  adapter: { id: string; palette?: Palette; resolvedBaseHue?: number };
  enabled: boolean;
  reason?: "awaiting_user" | "disabled_by_user";
  warnings?: string[];
  adapterToken?: string;
}
export interface SessionReply {
  session: { uid: string; visible: boolean };
  warnings?: string[];
}
export interface Info {
  api: 1;
  apiMinor?: number;
  server?: "orbtile";
  version?: string;
  activities?: string[];
  limits?: Record<string, number>;
  proof: string;
}
export interface BaseEvent {
  sequence: number;
  sessionId: string;
  host: string;
}
export interface TapEvent extends BaseEvent {
  type: "tap";
}
export interface ApprovalEvent extends BaseEvent {
  type: "approval";
  requestId: string;
  decision: "allow" | "deny";
  expiresAt: number;
}
export interface QuestionEvent extends BaseEvent {
  type: "question";
  requestId: string;
  picks: number[][];
  expiresAt: number;
}
export type AdapterEvent = TapEvent | ApprovalEvent | QuestionEvent;
export interface EventsReply {
  events: AdapterEvent[];
  lastSequence: number;
  acknowledged: number;
}
export class AdapterError extends Error {
  constructor(
    code: string,
    message?: string,
    options?: { status?: number; field?: string; retryAfter?: number },
  );
  code: string;
  status: number;
  field?: string;
  retryAfter?: number;
}
export class PayloadTooLargeError extends AdapterError {}
export class TransportError extends AdapterError {}
export class RegistrationLostError extends AdapterError {}
export interface ClientOptions {
  adapterId: string;
  registration: Registration;
  apiFile?: string;
  heartbeatIntervalMs?: number;
  onLeaseError?: (error: AdapterError) => void;
}
export class AdapterClient {
  constructor(options: ClientOptions);
  readonly adapterId: string;
  readonly apiFile: string;
  readonly path: string;
  registration: Registration;
  info?: Info;
  readonly cursor: number;
  readonly leaseGeneration: number;
  readonly reconciliationRequired: boolean;
  connect(): Promise<RegistrationReply>;
  register(registration?: Registration): Promise<RegistrationReply>;
  heartbeat(): Promise<void>;
  upsertSession(id: string, fields: SessionUpdate): Promise<SessionReply>;
  removeSession(id: string, host?: string): Promise<void>;
  replaceSessions(
    sessions: SessionBatchItem[],
  ): Promise<Record<string, unknown>>;
  requestApproval(
    id: string,
    request: ApprovalRequest,
    fields?: SessionUpdate,
  ): Promise<SessionReply>;
  askQuestion(
    id: string,
    request: QuestionRequest,
    fields?: SessionUpdate,
  ): Promise<SessionReply>;
  answerInUI(
    id: string,
    host: string,
    commit: (
      request: ApprovalRequest | QuestionRequest,
    ) => void | Promise<void>,
    options?: { requestId?: string },
  ): Promise<boolean>;
  pollEvents(waitSeconds?: number): Promise<EventsReply>;
  ack(sequence: number): void;
  events(options?: {
    signal?: AbortSignal;
    waitSeconds?: number;
  }): AsyncGenerator<AdapterEvent, void, unknown>;
  applyEvent(
    event: AdapterEvent,
    commit: (
      event: AdapterEvent,
      request?: ApprovalRequest | QuestionRequest,
    ) => void | Promise<void>,
  ): Promise<boolean>;
  reconcile(): void;
  close(): void;
  unregister(): Promise<void>;
}
export function validPicks(questions: Question[], picks: unknown): boolean;
