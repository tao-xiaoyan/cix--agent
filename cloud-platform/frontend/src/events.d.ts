/* Generated from shared/schemas/event.schema.json. Do not edit. */

export type Event =
  | PhysiologicalEvent
  | PoseEvent
  | SleepEvent
  | AudioEvent
  | AnomalyEvent
  | AgentDecisionEvent
  | CloudAnalysisEvent
  | DeviceMetricsEvent;
export type SchemaVersion = "1.0.0";
export type EventId = string;
export type DeviceId = string;
export type Timestamp = string;
export type Source = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName = string | null;
export type ModelVersion = string | null;
export type Confidence = number | null;
export type EventType = "physiological_window";
export type WindowStart = string;
export type WindowEnd = string;
export type Name = string;
export type Unit = string | null;
export type SamplingRateHz = number | null;
export type Samples = (number | null)[];
export type Quality = "valid" | "missing" | "invalid" | "unknown";
export type MissingReason = string | null;
export type Channels = Channel[];
export type Connection = "connected" | "disconnected" | "unknown";
export type Protocol = string | null;
export type SchemaVersion1 = "1.0.0";
export type EventId1 = string;
export type DeviceId1 = string;
export type Timestamp1 = string;
export type Source1 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName1 = string | null;
export type ModelVersion1 = string | null;
export type Confidence1 = number | null;
export type EventType1 = "pose_estimation";
export type CoordinateSystem = "normalized" | "pixels";
export type FrameWidth = number | null;
export type FrameHeight = number | null;
export type TrackId = string | null;
export type Name1 = string;
export type X = number;
export type Y = number;
export type Confidence2 = number | null;
export type Keypoints = Keypoint[];
export type Persons = PersonPose[];
export type Status = "available" | "no_detection" | "unavailable";
export type SchemaVersion2 = "1.0.0";
export type EventId2 = string;
export type DeviceId2 = string;
export type Timestamp2 = string;
export type Source2 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName2 = string | null;
export type ModelVersion2 = string | null;
export type Confidence3 = number | null;
export type EventType2 = "sleep_posture";
export type Label = string | null;
export type Status1 = "available" | "unknown" | "unavailable";
export type Reason = string | null;
export type SchemaVersion3 = "1.0.0";
export type EventId3 = string;
export type DeviceId3 = string;
export type Timestamp3 = string;
export type Source3 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName3 = string | null;
export type ModelVersion3 = string | null;
export type Confidence4 = number | null;
export type EventType3 = "audio_event";
export type Label1 = string | null;
export type DurationMs = number | null;
export type Status2 = "available" | "no_detection" | "unavailable";
export type Reason1 = string | null;
export type SchemaVersion4 = "1.0.0";
export type EventId4 = string;
export type DeviceId4 = string;
export type Timestamp4 = string;
export type Source4 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName4 = string | null;
export type ModelVersion4 = string | null;
export type Confidence5 = number | null;
export type EventType4 = "anomaly_event";
export type Code = string;
export type Severity = "info" | "warning" | "critical";
export type Description = string;
export type RelatedEventIds = string[];
export type SchemaVersion5 = "1.0.0";
export type EventId5 = string;
export type DeviceId5 = string;
export type Timestamp5 = string;
export type Source5 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName5 = string | null;
export type ModelVersion5 = string | null;
export type Confidence6 = number | null;
export type EventType5 = "agent_decision";
export type Decision = string;
export type Rationale = string;
export type Actions = string[];
export type RelatedEventIds1 = string[];
export type ExecutionStatus = "proposed" | "executed" | "failed" | "skipped";
export type SchemaVersion6 = "1.0.0";
export type EventId6 = string;
export type DeviceId6 = string;
export type Timestamp6 = string;
export type Source6 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName6 = string | null;
export type ModelVersion6 = string | null;
export type Confidence7 = number | null;
export type EventType6 = "cloud_analysis_result";
export type TaskId = string;
export type Summary = string;
export type Findings = string[];
export type RelatedEventIds2 = string[];
export type SchemaVersion7 = "1.0.0";
export type EventId7 = string;
export type DeviceId7 = string;
export type Timestamp7 = string;
export type Source7 = "mock" | "model" | "sensor" | "rule" | "agent" | "cloud" | "system";
export type ModelName7 = string | null;
export type ModelVersion7 = string | null;
export type Confidence8 = number | null;
export type EventType7 = "device_metrics";
export type CpuPercent = number | null;
export type MemoryUsedBytes = number | null;
export type DiskFreeBytes = number | null;
export type TemperatureCelsius = number | null;
export type PendingUploadCount = number;

export interface PhysiologicalEvent {
  schema_version?: SchemaVersion;
  event_id: EventId;
  device_id: DeviceId;
  timestamp: Timestamp;
  source: Source;
  model_name?: ModelName;
  model_version?: ModelVersion;
  confidence?: Confidence;
  event_type: EventType;
  payload: PhysiologicalPayload;
}
export interface PhysiologicalPayload {
  window_start: WindowStart;
  window_end: WindowEnd;
  channels?: Channels;
  connection: Connection;
  protocol?: Protocol;
}
export interface Channel {
  name: Name;
  unit?: Unit;
  sampling_rate_hz?: SamplingRateHz;
  samples?: Samples;
  quality?: Quality;
  missing_reason?: MissingReason;
}
export interface PoseEvent {
  schema_version?: SchemaVersion1;
  event_id: EventId1;
  device_id: DeviceId1;
  timestamp: Timestamp1;
  source: Source1;
  model_name?: ModelName1;
  model_version?: ModelVersion1;
  confidence?: Confidence1;
  event_type: EventType1;
  payload: PosePayload;
}
export interface PosePayload {
  coordinate_system: CoordinateSystem;
  frame_width?: FrameWidth;
  frame_height?: FrameHeight;
  persons: Persons;
  status: Status;
}
export interface PersonPose {
  track_id?: TrackId;
  keypoints: Keypoints;
}
export interface Keypoint {
  name: Name1;
  x: X;
  y: Y;
  confidence?: Confidence2;
}
export interface SleepEvent {
  schema_version?: SchemaVersion2;
  event_id: EventId2;
  device_id: DeviceId2;
  timestamp: Timestamp2;
  source: Source2;
  model_name?: ModelName2;
  model_version?: ModelVersion2;
  confidence?: Confidence3;
  event_type: EventType2;
  payload: SleepPayload;
}
export interface SleepPayload {
  label?: Label;
  status: Status1;
  reason?: Reason;
}
export interface AudioEvent {
  schema_version?: SchemaVersion3;
  event_id: EventId3;
  device_id: DeviceId3;
  timestamp: Timestamp3;
  source: Source3;
  model_name?: ModelName3;
  model_version?: ModelVersion3;
  confidence?: Confidence4;
  event_type: EventType3;
  payload: AudioPayload;
}
export interface AudioPayload {
  label?: Label1;
  duration_ms?: DurationMs;
  status: Status2;
  reason?: Reason1;
}
export interface AnomalyEvent {
  schema_version?: SchemaVersion4;
  event_id: EventId4;
  device_id: DeviceId4;
  timestamp: Timestamp4;
  source: Source4;
  model_name?: ModelName4;
  model_version?: ModelVersion4;
  confidence?: Confidence5;
  event_type: EventType4;
  payload: AnomalyPayload;
}
export interface AnomalyPayload {
  code: Code;
  severity: Severity;
  description: Description;
  related_event_ids?: RelatedEventIds;
}
export interface AgentDecisionEvent {
  schema_version?: SchemaVersion5;
  event_id: EventId5;
  device_id: DeviceId5;
  timestamp: Timestamp5;
  source: Source5;
  model_name?: ModelName5;
  model_version?: ModelVersion5;
  confidence?: Confidence6;
  event_type: EventType5;
  payload: DecisionPayload;
}
export interface DecisionPayload {
  decision: Decision;
  rationale: Rationale;
  actions?: Actions;
  related_event_ids?: RelatedEventIds1;
  execution_status?: ExecutionStatus;
}
export interface CloudAnalysisEvent {
  schema_version?: SchemaVersion6;
  event_id: EventId6;
  device_id: DeviceId6;
  timestamp: Timestamp6;
  source: Source6;
  model_name?: ModelName6;
  model_version?: ModelVersion6;
  confidence?: Confidence7;
  event_type: EventType6;
  payload: AnalysisPayload;
}
export interface AnalysisPayload {
  task_id: TaskId;
  summary: Summary;
  findings?: Findings;
  related_event_ids?: RelatedEventIds2;
}
export interface DeviceMetricsEvent {
  schema_version?: SchemaVersion7;
  event_id: EventId7;
  device_id: DeviceId7;
  timestamp: Timestamp7;
  source: Source7;
  model_name?: ModelName7;
  model_version?: ModelVersion7;
  confidence?: Confidence8;
  event_type: EventType7;
  payload: MetricsPayload;
}
export interface MetricsPayload {
  cpu_percent?: CpuPercent;
  memory_used_bytes?: MemoryUsedBytes;
  disk_free_bytes?: DiskFreeBytes;
  temperature_celsius?: TemperatureCelsius;
  pending_upload_count?: PendingUploadCount;
  sensors?: Sensors;
}
export interface Sensors {
  [k: string]: "connected" | "disconnected" | "unknown";
}
